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

from app.api.proposals import (
    EXISTS,
    FINAL,
    HAS_FLAGS,
    NO_CLIENT,
    OWN,
    SENT_HAS_FLAGS,
    STALE,
)
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
    ProjectProposal,
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


@pytest.mark.parametrize(
    "written", ["$1,650.00", "$1,650", "$1650", "1,650.00", "1650.00", "1,650", "£1,650", "€1650"]
)
def test_the_total_matches_with_or_without_sign_separators_or_cents(written):
    assert check(_values(summary=f"The price is {written}."), _facts()) == []


@pytest.mark.parametrize("written", ["$1,650.01", "$1,649", "1,600.00", "$16.50"])
def test_an_amount_matches_only_exactly_at_2_decimal_places(written):
    flags = check(_values(summary=f"The price is {written}."), _facts())
    assert [f.as_dict() for f in flags] == [
        {"rule": "stray_money", "section": "summary", "text": written}
    ]


@pytest.mark.parametrize("written", ["$40k", "40k", "$1.65k", "£1.2m", "$2bn", "$2 million"])
def test_a_shorthand_money_figure_is_always_flagged(written):
    flags = check(_values(terms=f"A budget of {written} for the work."), _facts())
    assert [f.as_dict() for f in flags] == [
        {"rule": "stray_money", "section": "terms", "text": written}
    ]


@pytest.mark.parametrize("text", ["3 workshops over 12 weeks", "50% on signing", "3 months", "5m"])
def test_numbers_that_are_not_money_are_not_flagged(text):
    assert check(_values(terms=text), _facts()) == []


@pytest.mark.parametrize(
    "written",
    ["2027-01-15", "15 January 2027", "January 15, 2027", "15th Jan 2027", "Jan 15 2027"],
)
def test_each_date_form_is_read(written):
    flags = check(_values(summary=f"Kick-off on {written}."), _facts())
    assert [(f.rule, f.text) for f in flags] == [("stray_date", written)]


@pytest.mark.parametrize("written", ["2027-03-01", "1 March 2027", "March 1, 2027"])
def test_each_date_form_matches_a_milestone_date(written):
    assert check(_values(summary=f"The pilot ends {written}."), _facts()) == []


@pytest.mark.parametrize(
    "written", ["15/01/2027", "01/15/2027", "January 2027", "15 January", "mid-January", "Q1 2027"]
)
def test_other_date_forms_are_not_read(written):
    assert check(_values(summary=f"Kick-off in {written}."), _facts()) == []


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


LINE = {"description": "Team", "category": "people", "amount": "1000.00"}


@pytest.mark.parametrize(
    "change", [{"description": "Analysts"}, {"category": "other"}, {"amount": "900.00"}]
)
def test_an_estimate_line_changed_in_the_plan_is_flagged_even_with_the_same_total(change):
    facts = _facts(estimate_lines=[{**LINE, **change}])
    flags = check(_values(), facts)
    assert [(f.rule, f.section) for f in flags] == [("changed_estimate_line", "price")]
    assert flags[0].text.startswith("Estimate line 1: Team (people) 1000.00 is now")


def test_an_estimate_line_added_or_dropped_in_the_plan_is_flagged():
    extra = {"description": "Travel", "category": "other", "amount": "100.00"}
    flags = check(_values(), _facts(estimate_lines=[LINE, extra]))
    assert [(f.rule, f.section, f.text) for f in flags] == [
        (
            "missing_estimate_line",
            "price",
            "Estimate line 2: Travel (other) 100.00 is in the plan, not the proposal",
        )
    ]
    flags = check(_values(estimate_lines=[LINE, extra]), _facts())
    assert [(f.rule, f.text) for f in flags] == [
        ("removed_estimate_line", "Estimate line 2: Travel (other) 100.00 is no longer in the plan")
    ]


def test_stray_money_reads_the_plans_figures_not_the_proposals_copy():
    # The plan's line is now 900.00: the text may quote it, and the copy's 1000.00 is stray.
    facts = _facts(estimate_lines=[{**LINE, "amount": "900.00"}])
    flags = check(_values(summary="The team costs $900.00, not $1,000.00."), facts)
    assert [(f.rule, f.text) for f in flags if f.rule == "stray_money"] == [
        ("stray_money", "$1,000.00")
    ]
    assert [f.rule for f in flags] == ["stray_money", "changed_estimate_line"]
    # The same holds for the total: the plan's new total may be quoted, the copy's may not.
    facts = _facts(total=Decimal("2000.00"))
    flags = check(_values(summary="In all $2,000.00, once $1,650.00."), facts)
    assert [(f.rule, f.text) for f in flags] == [
        ("stray_money", "$1,650.00"),
        ("stale_price", flags[1].text),
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


def test_removed_deliverable():
    facts = _facts(deliverables=[])
    flags = check(_values(), facts)
    assert [f.as_dict() for f in flags] == [
        {"rule": "removed_deliverable", "section": "deliverables", "text": "Model"}
    ]


def test_changed_deliverable_description():
    facts = _facts(deliverables=[{"name": "Model", "description": "Weekly", "milestone": "Pilot"}])
    flags = check(_values(), facts)
    assert [(f.rule, f.section) for f in flags] == [("changed_deliverable", "deliverables")]
    assert "Model" in flags[0].text


def test_changed_milestone_description():
    facts = _facts(
        milestones=[{"name": "Pilot", "target_date": "2027-03-01", "description": "Two sites"}]
    )
    flags = check(_values(), facts)
    assert [(f.rule, f.section) for f in flags] == [("changed_milestone", "milestones")]
    assert "Pilot (2027-03-01)" in flags[0].text


RISK = {"risk_id": 7, "title": "Late data", "response": "reduce", "response_plan": "Chase"}


def test_a_shown_risk_no_longer_ticked():
    flags = check(_values(risks=[RISK]), _facts(risk_states={7: ("open", False)}))
    assert [f.as_dict() for f in flags] == [
        {"rule": "risk_changed", "section": "risks", "text": "Late data (no longer ticked to show)"}
    ]


def test_a_ticked_risk_not_yet_shown():
    flags = check(_values(risks=[]), _facts(shown_risks=[RISK], risk_states={7: ("open", True)}))
    assert [f.as_dict() for f in flags] == [
        {
            "rule": "risk_changed",
            "section": "risks",
            "text": "Late data (ticked to show, not yet shown)",
        }
    ]


@pytest.mark.parametrize(
    "change",
    [{"title": "Data arrives late"}, {"response": "avoid"}, {"response_plan": "Escalate"}],
)
def test_a_shown_risk_whose_title_or_response_changed(change):
    facts = _facts(shown_risks=[{**RISK, **change}], risk_states={7: ("open", True)})
    flags = check(_values(risks=[RISK]), facts)
    assert [(f.rule, f.section) for f in flags] == [("risk_changed", "risks")]
    assert "title or response changed" in flags[0].text


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
    assert set(_rules(body)) == {"stale_price", "missing_estimate_line", "missing_deliverable"}
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


def test_an_estimate_line_change_with_the_same_total_flags_until_refreshed(web, db, project):
    proposal = _create(web, project)
    plan = _plan(db, project)
    first, second = db.scalars(
        select(PlanEstimateLine)
        .where(PlanEstimateLine.plan_id == plan.id)
        .order_by(PlanEstimateLine.id)
    ).all()[:2]
    # Move 100.00 from one line to the other: the total doesn't change.
    first.amount, second.amount = first.amount - 100, second.amount + 100
    db.commit()
    body = _get(web, project)
    assert body["price"] == proposal["price"]
    assert _rules(body) == ["changed_estimate_line", "changed_estimate_line"]
    assert _post(web, project, "/submit").status_code == 409
    assert _post(web, project, "/refresh").json()["flags"] == []


def test_a_description_change_in_the_plan_flags_until_refreshed(web, db, project):
    _create(web, project)
    plan = _plan(db, project)
    db.scalar(
        select(PlanDeliverable).where(PlanDeliverable.plan_id == plan.id)
    ).description = "Weekly demand forecast"
    db.scalar(
        select(PlanMilestone).where(PlanMilestone.plan_id == plan.id)
    ).description = "Two warehouses live"
    db.commit()
    assert _rules(_get(web, project)) == ["changed_deliverable", "changed_milestone"]
    assert _post(web, project, "/refresh").json()["flags"] == []


@pytest.mark.parametrize(
    "change",
    [{"title": "Data arrives late"}, {"response": "avoid"}, {"response_plan": "Escalate early"}],
)
def test_a_shown_risk_edited_in_the_register_flags_until_refreshed(web, db, project, change):
    risk = _risk(db, project, show_in_proposal=True)
    _create(web, project)
    for name, value in change.items():
        setattr(risk, name, value)
    db.commit()
    assert _rules(_get(web, project)) == ["risk_changed"]
    assert _post(web, project, "/refresh").json()["flags"] == []


def _refusal(db: Session, action: str, refusal: str) -> AuditDecision:
    row = _audit(db)[-1]
    assert row.status == "rejected"
    assert row.action_taken["action"] == f"{action}_refused"
    assert row.action_taken["refusal"] == refusal
    return row


@pytest.mark.parametrize("edit", ["update", "refresh"])
def test_a_stale_edit_is_a_409_audited(web, db, project, edit):
    _create(web, project)
    if edit == "update":
        response = web.patch(_url(project), json={"version": 99, "summary": "x"})
    else:
        response = web.post(_url(project, "/refresh"), json={"version": 99})
    assert response.status_code == 409
    assert response.json()["detail"] == STALE
    _refusal(db, edit, "stale")
    assert _get(web, project)["summary"] is None


@pytest.mark.parametrize("final", ["sent", "accepted", "declined", "withdrawn"])
@pytest.mark.parametrize("edit", ["update", "refresh"])
def test_an_edit_to_a_sent_answered_or_withdrawn_proposal_is_a_409_audited(
    web, db, sign_in, project, pm, final, edit
):
    if final == "withdrawn":
        _create(web, project)
        assert _post(web, project, "/withdraw", reason=WITHDRAW_REASON).status_code == 200
    else:
        _to_status(web, db, sign_in, project, pm, final)
    before = len(_audit(db))
    version = _get(web, project)["version"]
    if edit == "update":
        response = web.patch(_url(project), json={"version": version, "summary": "x"})
    else:
        response = web.post(_url(project, "/refresh"), json={"version": version})
    assert response.status_code == 409
    assert response.json()["detail"] == FINAL
    assert len(_audit(db)) == before + 1
    assert _refusal(db, edit, "wrong_status").action_taken["status"] == final
    assert _get(web, project)["status"] == final


def test_a_second_create_is_a_409_audited(web, db, project):
    proposal = _create(web, project)
    response = web.post(_url(project))
    assert response.status_code == 409
    assert response.json()["detail"] == EXISTS
    row = _refusal(db, "create", "exists")
    assert (row.subject_type, row.subject_id) == ("proposal", str(proposal["id"]))


@pytest.mark.parametrize("role", ["sponsor", "auditor", "ethics_reviewer", "admin"])
def test_a_create_without_project_write_is_a_403_audited_against_the_project(
    web, db, sign_in, project, role
):
    sign_in(web, db, role, email=f"other.{role}@pmo.example")
    assert web.post(_url(project)).status_code == 403
    row = _refusal(db, "create", "no_permission")
    assert (row.subject_type, row.subject_id) == ("project", str(project.id))
    assert row.action_taken["actor_role"] == role


@pytest.mark.parametrize("role", ["sponsor", "auditor", "ethics_reviewer", "admin"])
@pytest.mark.parametrize("edit", ["update", "refresh"])
def test_an_edit_without_project_write_is_a_403_audited(web, db, sign_in, project, role, edit):
    proposal = _create(web, project)
    sign_in(web, db, role, email=f"other.{role}@pmo.example")
    body = {"version": proposal["version"], "summary": "x"}
    if edit == "update":
        response = web.patch(_url(project), json=body)
    else:
        response = web.post(_url(project, "/refresh"), json={"version": proposal["version"]})
    assert response.status_code == 403
    row = _refusal(db, edit, "no_permission")
    assert (row.subject_type, row.subject_id) == ("proposal", str(proposal["id"]))
    assert row.action_taken["actor_role"] == role
    assert _get(web, project)["summary"] is None


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
    assert set(versions[0]["content"]["risks"][0]) == {
        "risk_id",
        "title",
        "response",
        "response_plan",
    }
    for text in (str(body), str(versions)):
        for hidden in ("score", "band", "High", "probability", "impact"):
            assert hidden not in text
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


def test_sent_with_an_open_flag_is_a_409_naming_the_flags(web, db, sign_in, project, pm):
    _approved(web, db, sign_in, project, pm)
    plan = _plan(db, project)
    db.add(PlanDeliverable(plan_id=plan.id, name="Handbook"))
    db.commit()
    response = _post(web, project, "/sent", **SENT)
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": SENT_HAS_FLAGS,
        "flags": [{"rule": "missing_deliverable", "section": "deliverables", "text": "Handbook"}],
    }
    assert _get(web, project)["status"] == "approved"
    refused = _refusal(db, "sent", "flags_open")
    assert refused.action_taken["flags"] == response.json()["detail"]["flags"]
    db.expire_all()
    assert db.scalar(select(ProposalEvent).where(ProposalEvent.kind == "sent")) is None


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


WITHDRAW_REASON = "The client has paused the work"
TODAY = date.today().isoformat()
SENT = {"sent_to": "Dana", "sent_on": TODAY, "means": "Email"}
ANSWER = {"answer": "accepted", "answered_on": TODAY, "note": "Signed"}


def _to_status(web, db, sign_in, project, pm, wanted: str) -> None:
    """Take a fresh proposal to `wanted`, ending signed in as a project manager who didn't
    submit it."""
    if wanted == "draft":
        _create(web, project)
        return
    if wanted == "submitted":
        _submitted(web, db, sign_in, project)
        sign_in(web, db, "project_manager", email="pm2@pmo.example")
        return
    _approved(web, db, sign_in, project, pm)
    if wanted in ("sent", "accepted", "declined"):
        assert _post(web, project, "/sent", **SENT).status_code == 200
    if wanted in ("accepted", "declined"):
        response = _post(web, project, "/answer", **{**ANSWER, "answer": wanted})
        assert response.status_code == 200


@pytest.mark.parametrize("start", ["draft", "submitted", "approved", "sent"])
def test_withdrawing_from_each_open_status(web, db, sign_in, project, pm, start):
    _to_status(web, db, sign_in, project, pm, start)
    before = len(_audit(db))
    response = _post(web, project, "/withdraw", reason=WITHDRAW_REASON)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "withdrawn"
    [row] = _audit(db)[before:]
    assert (row.status, row.trigger) == ("ok", "human")
    assert row.action_taken["action"] == "withdraw"
    assert (row.action_taken["from"], row.action_taken["to"]) == (start, "withdrawn")
    db.expire_all()
    event = db.scalar(select(ProposalEvent).where(ProposalEvent.kind == "withdraw"))
    assert event.reason == WITHDRAW_REASON


@pytest.mark.parametrize("final", ["accepted", "declined", "withdrawn"])
def test_an_answered_or_withdrawn_proposal_cannot_be_withdrawn(
    web, db, sign_in, project, pm, final
):
    if final == "withdrawn":
        _create(web, project)
        assert _post(web, project, "/withdraw", reason=WITHDRAW_REASON).status_code == 200
    else:
        _to_status(web, db, sign_in, project, pm, final)
    response = _post(web, project, "/withdraw", reason=WITHDRAW_REASON)
    assert response.status_code == 409
    row = _audit(db)[-1]
    assert (row.status, row.action_taken["action"]) == ("rejected", "withdraw_refused")
    assert row.action_taken["refusal"] == "wrong_status"
    assert _get(web, project)["status"] == final


def test_withdrawing_needs_a_reason(web, project):
    _create(web, project)
    for body in ({}, {"reason": ""}, {"reason": "Paused"}):
        assert _post(web, project, "/withdraw", **body).status_code == 422
    assert _get(web, project)["status"] == "draft"


@pytest.mark.parametrize(
    ("start", "step"),
    [
        ("draft", "sent"),
        ("submitted", "sent"),
        ("sent", "sent"),
        ("accepted", "sent"),
        ("draft", "answer"),
        ("submitted", "answer"),
        ("approved", "answer"),
        ("declined", "answer"),
    ],
)
def test_the_steps_go_in_order(web, db, sign_in, project, pm, start, step):
    _to_status(web, db, sign_in, project, pm, start)
    response = _post(web, project, f"/{step}", **(SENT if step == "sent" else ANSWER))
    assert response.status_code == 409
    row = _audit(db)[-1]
    assert (row.status, row.action_taken["action"]) == ("rejected", f"{step}_refused")
    assert row.action_taken["refusal"] == "wrong_status"
    assert row.action_taken["status"] == start
    assert _get(web, project)["status"] == start


def test_the_read_returns_the_current_flags_without_a_save(web, db, project):
    # Text that reached the database without a save through the API still shows its flags.
    _create(web, project)
    db.expire_all()
    proposal = db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == project.id))
    proposal.terms = "A deposit of $40k by 2027-02-01"
    db.commit()
    assert _get(web, project)["flags"] == [
        {"rule": "stray_money", "section": "terms", "text": "$40k"},
        {"rule": "stray_date", "section": "terms", "text": "2027-02-01"},
    ]
