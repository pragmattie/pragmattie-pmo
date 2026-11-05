"""A client project's proposal and its checker (item 5.2).

Here: each checker rule, what the proposal copies from the plan, the statuses and who may move
them, the frozen versions, and the audit rows for every change and refusal. The roles matrix
(`test_authz.py`) covers every route for every role.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.proposals import HAS_FLAGS, NO_CLIENT, OWN, STALE
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    Client,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectPlan,
    ProposalEvent,
    Risk,
    User,
)
from app.proposals import PlanFacts, check
from app.security import SESSION_COOKIE

PROJECTS = "/api/v1/projects"
REASON = "Matches the plan and what the client asked for"


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


@pytest.fixture
def web(db) -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def pm(web, db, sign_in) -> User:
    return sign_in(web, db, "project_manager")


def _project(db: Session, *, client: bool = True) -> Project:
    """A project in planning with a plan: one milestone, one deliverable and two estimate lines
    (1000.00 and 500.00) with 10% contingency, so the total is 1650.00."""
    client_id = None
    if client:
        row = Client(name="Northwind Example", contact_email="buyer@northwind.example")
        db.add(row)
        db.flush()
        client_id = row.id
    project = Project(
        name="Forecast",
        client_id=client_id,
        stage="planning",
        sponsor="S",
        pm="P",
        status="on_track",
    )
    db.add(project)
    db.flush()
    plan = ProjectPlan(
        project_id=project.id, scope_in="Two warehouses", scope_out="Stores", contingency_pct=10
    )
    db.add(plan)
    db.flush()
    milestone = PlanMilestone(plan_id=plan.id, name="Pilot", target_date=date(2027, 3, 1))
    db.add(milestone)
    db.flush()
    db.add_all(
        [
            PlanDeliverable(plan_id=plan.id, name="Forecast model", milestone_id=milestone.id),
            PlanEstimateLine(
                plan_id=plan.id, description="Team", category="people", amount=Decimal("1000")
            ),
            PlanEstimateLine(
                plan_id=plan.id, description="Licence", category="software", amount=Decimal("500")
            ),
        ]
    )
    db.commit()
    return project


@pytest.fixture
def project(db, pm) -> Project:
    return _project(db)


def _url(project: Project, suffix: str = "") -> str:
    return f"{PROJECTS}/{project.id}/proposal{suffix}"


def _create(web: TestClient, project: Project) -> dict:
    response = web.post(_url(project))
    assert response.status_code == 201, response.text
    return response.json()


def _get(web: TestClient, project: Project) -> dict:
    response = web.get(_url(project))
    assert response.status_code == 200, response.text
    return response.json()


def _post(web: TestClient, project: Project, suffix: str, **body):
    return web.post(_url(project, suffix), json={"version": _get(web, project)["version"], **body})


def _rules(proposal: dict) -> list[str]:
    return [flag["rule"] for flag in proposal["flags"]]


def _audit(db: Session) -> list[AuditDecision]:
    db.expire_all()
    return list(
        db.scalars(
            select(AuditDecision)
            .where(AuditDecision.agent == "proposal")
            .order_by(AuditDecision.id)
        )
    )


def _plan(db: Session, project: Project) -> ProjectPlan:
    db.expire_all()
    return db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))


def _submitted(web, db, sign_in, project) -> dict:
    """A clean proposal submitted by the signed-in project manager; the client is then signed
    in as a sponsor."""
    _create(web, project)
    response = _post(web, project, "/submit")
    assert response.status_code == 200, response.text
    sign_in(web, db, "sponsor")
    return response.json()


def _approved(web, db, sign_in, project, pm) -> dict:
    _submitted(web, db, sign_in, project)
    response = _post(web, project, "/approve", reason=REASON)
    assert response.status_code == 200, response.text
    sign_in(web, db, "project_manager", email="pm2@pmo.example")
    return response.json()


# --- The checker, one rule at a time (pure function)


def _facts(**changes) -> PlanFacts:
    values = {
        "version": 1,
        "total": Decimal("1650.00"),
        "estimate_lines": [{"description": "Team", "category": "people", "amount": "1000.00"}],
        "milestones": [{"name": "Pilot", "target_date": "2027-03-01", "description": None}],
        "deliverables": [{"name": "Model", "description": None, "milestone": "Pilot"}],
        "shown_risks": [],
        "risk_states": {},
    }
    return PlanFacts(**{**values, **changes})


def _values(**changes) -> dict:
    facts = _facts()
    values = {
        "summary": None,
        "scope_in": None,
        "scope_out": None,
        "assumptions": None,
        "terms": None,
        "price": "1650.00",
        "estimate_lines": facts.estimate_lines,
        "milestones": facts.milestones,
        "deliverables": facts.deliverables,
        "risks": [],
    }
    return {**values, **changes}


def test_a_proposal_matching_its_plan_has_no_flags():
    text = "A pilot for $1,650.00 (team: 1000.00), finishing 1 March 2027 or 2027-03-01."
    assert check(_values(summary=text), _facts()) == []


def test_stray_money_names_the_section_and_the_figure():
    flags = check(
        _values(terms="A deposit of $400 and 1,000.00 for the team. 3 workshops, 50%."), _facts()
    )
    assert [f.as_dict() for f in flags] == [
        {"rule": "stray_money", "section": "terms", "text": "$400"}
    ]


def test_stray_date_names_the_section_and_the_date():
    flags = check(_values(summary="Starts 2027-01-15, the pilot ends March 1, 2027."), _facts())
    assert [f.as_dict() for f in flags] == [
        {"rule": "stray_date", "section": "summary", "text": "2027-01-15"}
    ]
    flags = check(_values(assumptions="Data by 3 Feb 2027"), _facts())
    assert [(f.rule, f.section, f.text) for f in flags] == [
        ("stray_date", "assumptions", "3 Feb 2027")
    ]


def test_a_date_is_not_read_as_money():
    assert check(_values(summary="Pilot ends 2027-03-01"), _facts()) == []


def test_missing_deliverable():
    facts = _facts(
        deliverables=[
            {"name": "Model", "description": None, "milestone": "Pilot"},
            {"name": "Handbook", "description": None, "milestone": "Pilot"},
        ]
    )
    flags = check(_values(), facts)
    assert [(f.rule, f.section, f.text) for f in flags] == [
        ("missing_deliverable", "deliverables", "Handbook")
    ]


def test_missing_milestone_and_a_moved_date():
    facts = _facts(milestones=[{"name": "Pilot", "target_date": "2027-04-01", "description": None}])
    flags = check(_values(), facts)
    assert [(f.rule, f.text) for f in flags] == [
        ("missing_milestone", "Pilot (2027-04-01)"),
        ("removed_milestone", "Pilot (2027-03-01)"),
    ]


def test_stale_price():
    flags = check(_values(), _facts(total=Decimal("2000.00")))
    assert [(f.rule, f.section) for f in flags] == [("stale_price", "price")]
    assert "1650.00" in flags[0].text and "2000.00" in flags[0].text


@pytest.mark.parametrize("status", ["closed", "rejected"])
def test_a_shown_risk_since_closed_or_rejected(status):
    risk = {"risk_id": 7, "title": "Late data", "response": "reduce", "response_plan": "Chase"}
    flags = check(_values(risks=[risk]), _facts(risk_states={7: (status, True)}))
    assert [f.as_dict() for f in flags] == [
        {"rule": "closed_risk", "section": "risks", "text": f"Late data ({status})"}
    ]


# --- What the proposal takes from the plan


def test_creating_copies_price_dates_deliverables_and_scope_from_the_plan(web, db, project):
    body = _create(web, project)
    assert body["status"] == "draft"
    assert body["client_name"] == "Northwind Example"
    assert body["price"] == "1650.00"
    assert [line["amount"] for line in body["estimate_lines"]] == ["1000.00", "500.00"]
    assert body["milestones"] == [
        {"name": "Pilot", "target_date": "2027-03-01", "description": None}
    ]
    assert body["deliverables"] == [
        {"name": "Forecast model", "description": None, "milestone": "Pilot"}
    ]
    assert body["scope_in"] == "Two warehouses"
    assert body["flags"] == []
    assert body["plan_changed"] is False
    [row] = _audit(db)
    assert (row.trigger, row.status, row.action_taken["action"]) == ("human", "ok", "create")
    assert web.post(_url(project)).status_code == 409


def test_the_price_and_dates_cannot_be_typed(web, project):
    proposal = _create(web, project)
    for field, value in (("price", "1.00"), ("milestones", []), ("deliverables", [])):
        response = web.patch(_url(project), json={"version": proposal["version"], field: value})
        assert response.status_code == 422, field


def test_a_project_with_no_client_has_no_proposal(web, db, pm):
    internal = _project(db, client=False)
    for response in (web.get(_url(internal)), web.post(_url(internal))):
        assert response.status_code == 404
        assert response.json()["detail"] == NO_CLIENT


def test_a_plan_change_flags_the_proposal_until_it_is_refreshed(web, db, project):
    _create(web, project)
    plan = _plan(db, project)
    db.add(PlanEstimateLine(plan_id=plan.id, description="Travel", category="other", amount=100))
    db.add(PlanDeliverable(plan_id=plan.id, name="Handbook"))
    plan.updated_at = plan.updated_at + timedelta(seconds=1)
    db.commit()

    body = _get(web, project)
    assert set(_rules(body)) == {"stale_price", "missing_deliverable"}
    assert body["plan_changed"] is True

    refreshed = _post(web, project, "/refresh").json()
    assert refreshed["flags"] == []
    assert refreshed["price"] == "1760.00"
    assert refreshed["plan_changed"] is False
    assert [a.action_taken["action"] for a in _audit(db)] == ["create", "refresh"]


def test_every_save_runs_the_checker(web, project):
    proposal = _create(web, project)
    response = web.patch(
        _url(project),
        json={"version": proposal["version"], "terms": "Pay 50% by 2027-02-01"},
    )
    assert response.status_code == 200
    assert response.json()["flags"] == [
        {"rule": "stray_date", "section": "terms", "text": "2027-02-01"}
    ]


def test_a_stale_edit_is_a_409(web, project):
    _create(web, project)
    response = web.patch(_url(project), json={"version": 99, "summary": "x"})
    assert response.status_code == 409
    assert response.json()["detail"] == STALE


# --- Risks shown to the client


def _risk(db: Session, project: Project, **values) -> Risk:
    values = {"title": "Late data", **values}
    risk = Risk(
        project_id=project.id,
        kind="threat",
        cause="The client's systems",
        effect="The pilot slips",
        category="project",
        objectives=["schedule"],
        probability=4,
        impact=5,
        score=20,
        band="High",
        response="reduce",
        response_plan="Agree a data date with the client",
        status="open",
        source="person",
        **values,
    )
    db.add(risk)
    db.commit()
    return risk


def test_only_ticked_risks_appear_and_never_their_score(web, db, project):
    _risk(db, project, show_in_proposal=True)
    _risk(db, project, title="Internal staffing")
    body = _create(web, project)
    assert body["risks"] == [
        {
            "risk_id": body["risks"][0]["risk_id"],
            "title": "Late data",
            "response": "reduce",
            "response_plan": "Agree a data date with the client",
        }
    ]
    assert _post(web, project, "/submit").status_code == 200
    versions = web.get(_url(project, "/versions")).json()["items"]
    for text in (str(body), str(versions)):
        assert "score" not in text and "band" not in text and "High" not in text
        assert "Internal staffing" not in text


def test_the_risk_tick_is_off_by_default_and_set_through_the_risk_register(web, db, project):
    created = web.post(
        f"{PROJECTS}/{project.id}/risks",
        json={
            "kind": "threat",
            "title": "Late data",
            "cause": "Systems",
            "effect": "Slip",
            "category": "project",
            "objectives": ["schedule"],
            "probability": 2,
            "impact": 2,
            "response": "reduce",
            "response_plan": "Agree a date",
        },
    ).json()
    assert created["show_in_proposal"] is False
    ticked = web.patch(
        f"{PROJECTS}/{project.id}/risks/{created['id']}",
        json={"version": created["version"], "show_in_proposal": True},
    ).json()
    assert ticked["show_in_proposal"] is True
    proposal = _create(web, project)
    assert [r["title"] for r in proposal["risks"]] == ["Late data"]


def test_closing_a_shown_risk_flags_the_proposal(web, db, project):
    risk = _risk(db, project, show_in_proposal=True)
    _create(web, project)
    risk.status = "closed"
    db.commit()
    assert _rules(_get(web, project)) == ["closed_risk"]
    assert _post(web, project, "/refresh").json()["flags"] == []


# --- Submitting


def test_submit_with_an_open_flag_is_a_409_naming_the_flags(web, db, project):
    proposal = _create(web, project)
    web.patch(_url(project), json={"version": proposal["version"], "summary": "Only $99"})
    response = _post(web, project, "/submit")
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": HAS_FLAGS,
        "flags": [{"rule": "stray_money", "section": "summary", "text": "$99"}],
    }
    assert _get(web, project)["status"] == "draft"
    refused = _audit(db)[-1]
    assert refused.status == "rejected"
    assert refused.action_taken["refusal"] == "flags_open"
    assert refused.action_taken["flags"] == response.json()["detail"]["flags"]


def test_submit_stores_a_frozen_copy(web, db, sign_in, project, pm):
    body = _submitted(web, db, sign_in, project)
    assert body["status"] == "submitted"
    assert body["latest_version"] == 1
    assert body["submitted_by"] == pm.id
    [copy] = web.get(_url(project, "/versions")).json()["items"]
    assert copy["number"] == 1
    assert copy["content"]["price"] == "1650.00"
    assert copy["content"]["client_name"] == "Northwind Example"
    assert copy["submitted_by_name"] == pm.display_name
    row = _audit(db)[-1]
    assert row.action_taken["action"] == "submit"
    assert (row.action_taken["from"], row.action_taken["to"]) == ("draft", "submitted")


# --- The sponsor's decision


def test_the_sponsor_approves_with_a_reason(web, db, sign_in, project):
    _submitted(web, db, sign_in, project)
    assert _post(web, project, "/approve", reason="Too short").status_code == 422
    response = _post(web, project, "/approve", reason=REASON)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert (body["approved_version"], body["approval_current"]) == (1, True)
    [copy] = web.get(_url(project, "/versions")).json()["items"]
    assert [(e["kind"], e["reason"]) for e in copy["events"]] == [("approve", REASON)]
    assert _audit(db)[-1].action_taken["action"] == "approve"


def test_returning_sends_it_back_to_draft(web, db, sign_in, project):
    _submitted(web, db, sign_in, project)
    body = _post(
        web, project, "/return", reason="Say more about the data the client provides"
    ).json()
    assert body["status"] == "draft"
    assert body["approved_version"] is None


@pytest.mark.parametrize("role", ["project_manager", "auditor", "ethics_reviewer", "admin"])
@pytest.mark.parametrize("decision", ["approve", "return"])
def test_only_a_sponsor_decides_and_every_refusal_is_audited(
    web, db, sign_in, project, role, decision
):
    _submitted(web, db, sign_in, project)
    sign_in(web, db, role, email=f"other.{role}@pmo.example")
    response = _post(web, project, f"/{decision}", reason=REASON)
    assert response.status_code == 403
    row = _audit(db)[-1]
    assert row.status == "rejected"
    assert row.action_taken["refusal"] == "no_permission"
    assert row.action_taken["actor_role"] == role
    assert row.action_taken["action"] == f"{decision}_refused"
    assert _get(web, project)["status"] == "submitted"


def test_a_sponsor_cannot_decide_their_own_submission(web, db, project, pm):
    # The project manager submits, then becomes a sponsor: the role is read from the database
    # on every request, so the same session now holds proposal:approve.
    _create(web, project)
    assert _post(web, project, "/submit").status_code == 200
    db.expire_all()
    db.get(User, pm.id).role = "sponsor"
    db.commit()
    response = _post(web, project, "/approve", reason=REASON)
    assert response.status_code == 403
    assert response.json()["detail"] == OWN
    assert _audit(db)[-1].action_taken["refusal"] == "own_submission"
    assert _get(web, project)["status"] == "submitted"


def test_a_decision_on_a_draft_or_a_stale_version_is_refused_and_audited(web, db, sign_in, project):
    _create(web, project)
    sign_in(web, db, "sponsor")
    response = _post(web, project, "/approve", reason=REASON)
    assert response.status_code == 409
    assert _audit(db)[-1].action_taken["refusal"] == "wrong_status"
    response = web.post(_url(project, "/approve"), json={"version": 99, "reason": REASON})
    assert response.status_code == 409
    assert _audit(db)[-1].action_taken["refusal"] == "stale"


def test_an_edit_after_approval_returns_it_to_draft(web, db, sign_in, project, pm):
    _approved(web, db, sign_in, project, pm)
    proposal = _get(web, project)
    body = web.patch(
        _url(project), json={"version": proposal["version"], "summary": "A two-warehouse pilot"}
    ).json()
    assert body["status"] == "draft"
    assert body["approved_version"] == 1
    assert body["approval_current"] is False
    row = _audit(db)[-1]
    assert (row.action_taken["from"], row.action_taken["to"]) == ("approved", "draft")
    # The next submit is a new copy, and the approval stays with the first.
    body = _post(web, project, "/submit").json()
    assert (body["latest_version"], body["approved_version"]) == (2, 1)
    assert body["approval_current"] is False


# --- Sent and the client's answer


def test_marking_sent_records_who_when_and_how_and_sends_nothing(web, db, sign_in, project, pm):
    _approved(web, db, sign_in, project, pm)
    today = date.today().isoformat()
    response = _post(
        web, project, "/sent", sent_to="Dana, Northwind buyer", sent_on=today, means="Email"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "sent"
    db.expire_all()
    event = db.scalar(select(ProposalEvent).where(ProposalEvent.kind == "sent"))
    assert (event.sent_to, event.sent_on.isoformat(), event.means) == (
        "Dana, Northwind buyer",
        today,
        "Email",
    )
    row = _audit(db)[-1]
    assert row.action_taken["action"] == "sent"
    assert row.actor_user_id == event.actor_user_id
    assert row.trigger == "human"

    response = _post(web, project, "/answer", answer="accepted", answered_on=today, note="Signed")
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    [copy] = web.get(_url(project, "/versions")).json()["items"]
    assert [e["kind"] for e in copy["events"]] == ["approve", "sent", "answer"]
    assert copy["events"][-1]["answer"] == "accepted"
    # An answered proposal can't change.
    assert (
        web.patch(
            _url(project), json={"version": _get(web, project)["version"], "summary": "x"}
        ).status_code
        == 409
    )


def test_sent_needs_an_approved_proposal_and_a_past_date(web, db, sign_in, project, pm):
    _create(web, project)
    today = date.today()
    response = _post(web, project, "/sent", sent_to="Dana", sent_on=today.isoformat(), means="Post")
    assert response.status_code == 409
    assert _audit(db)[-1].action_taken["refusal"] == "wrong_status"
    future = (today + timedelta(days=1)).isoformat()
    assert (
        _post(web, project, "/sent", sent_to="Dana", sent_on=future, means="Post").status_code
        == 422
    )


@pytest.mark.parametrize("role", ["sponsor", "auditor", "ethics_reviewer", "admin"])
def test_only_project_write_marks_sent_and_records_the_answer(web, db, sign_in, project, pm, role):
    _approved(web, db, sign_in, project, pm)
    sign_in(web, db, role, email=f"other.{role}@pmo.example")
    today = date.today().isoformat()
    response = _post(web, project, "/sent", sent_to="Dana", sent_on=today, means="Email")
    assert response.status_code == 403
    assert _audit(db)[-1].action_taken["action"] == "sent_refused"


def test_without_a_session_nothing_moves(web, db, sign_in, project, pm):
    _submitted(web, db, sign_in, project)
    web.cookies.delete(SESSION_COOKIE)
    response = web.post(_url(project, "/approve"), json={"version": 1, "reason": REASON})
    assert response.status_code == 401


def test_withdrawing(web, db, project):
    _create(web, project)
    response = _post(web, project, "/withdraw", reason="The client has paused the work")
    assert response.json()["status"] == "withdrawn"
    assert (
        _post(web, project, "/withdraw", reason="The client has paused the work").status_code == 409
    )
