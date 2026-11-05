"""The go-ahead gate (item 5.4): its checks, a sponsor's decision, the baseline it freezes, and
every refusal audited."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_intake import _create, add_readiness

from app.api.gates import (
    GO_AHEAD_UNAVAILABLE,
    NOT_IN_PLANNING,
    NOT_SAVED,
    OWN_PROJECT,
    PROJECT_CANCELLED,
    PROJECT_STALE,
    PROJECT_UNMET,
)
from app.api.plan import CANCELLED_PROJECT as PLAN_CANCELLED
from app.api.plan import NOT_PLANNING
from app.api.proposals import CANCELLED_PROJECT as PROPOSAL_CANCELLED
from app.authz import FORBIDDEN
from app.db import engine
from app.gates import NO_TOP_BUDGET
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    Client,
    GateDecision,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectBaseline,
    ProjectBrief,
    ProjectPlan,
    ProjectProposal,
    ProjectRequest,
    Risk,
    User,
)
from app.proposals import plan_facts, refresh_from_plan

REASON = "The plan is complete and inside the budget"
TICKS = [
    {"id": "team_and_dates_realistic", "met": True, "note": "Owners agreed every date"},
    {"id": "risks_acceptable", "met": True, "note": "Nothing open we can't carry"},
]
CHECK_IDS = [
    "plan_complete",
    "estimate_within_budget",
    "proposal_accepted",
    "no_unreviewed_risks",
    "escalations_settled",
    "readiness_not_red",
]


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


@pytest.fixture
def web(db, gates_policy) -> TestClient:
    """The project manager's client."""
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def pm(web, db, sign_in) -> User:
    return sign_in(web, db, "project_manager")


def _client(db, sign_in, role: str, email: str | None = None) -> tuple[TestClient, User]:
    other = TestClient(app, base_url="http://localhost")
    return other, sign_in(other, db, role, email)


@pytest.fixture
def sponsor(db, sign_in, pm) -> TestClient:
    return _client(db, sign_in, "sponsor")[0]


def _project(db: Session, pm: User, *, client: bool = False, budget_high="1000.00") -> int:
    """A project in planning managed by `pm`, ready to pass: a complete plan of 900.00 (two lines
    and 10% contingency) within the brief's top budget, and no risks. With `client`, it has a
    client whose proposal the client accepted."""
    client_id = None
    if client:
        row = Client(name="Quayside Example")
        db.add(row)
        db.flush()
        client_id = row.id
    project = Project(
        name="Forecast",
        client_id=client_id,
        stage="planning",
        sponsor="S",
        pm="P",
        pm_user_id=pm.id,
        status="on_track",
    )
    db.add(project)
    db.flush()
    db.add(
        ProjectBrief(
            project_id=project.id,
            purpose="Fewer stock-outs",
            objectives="Forecast weekly demand",
            decision_rights=[],
            budget_high=budget_high,
        )
    )
    plan = ProjectPlan(
        project_id=project.id,
        scope_in="Weekly forecast for the pilot stores",
        scope_out="Other stores",
        contingency_pct=10,
        report_period="weekly",
        report_due_day=5,
    )
    db.add(plan)
    db.flush()
    milestone = PlanMilestone(
        plan_id=plan.id, name="Pilot live", target_date=date(2027, 3, 1), owner_user_id=pm.id
    )
    db.add(milestone)
    db.flush()
    db.add_all(
        [
            PlanDeliverable(plan_id=plan.id, name="Forecast model", milestone_id=milestone.id),
            PlanEstimateLine(
                plan_id=plan.id, description="Analysts", category="people", amount="700.00"
            ),
            PlanEstimateLine(
                plan_id=plan.id, description="Licence", category="software", amount="118.18"
            ),
        ]
    )
    if client:
        db.flush()
        proposal = ProjectProposal(project_id=project.id, status="accepted", created_by=pm.id)
        refresh_from_plan(proposal, plan_facts(db, plan))
        db.add(proposal)
    db.commit()
    return project.id


def _url(pid: int) -> str:
    return f"/api/v1/projects/{pid}/gate"


def _decide(client: TestClient, pid: int, outcome: str = "pass", **changes):
    body = {"version": 1, "outcome": outcome, "reason": REASON, "criteria": TICKS}
    body.update(changes)
    return client.post(_url(pid), json=body)


def _criteria(web: TestClient, pid: int) -> dict[str, tuple[bool | None, str | None]]:
    body = web.get(_url(pid)).json()
    return {c["id"]: (c["met"], c.get("detail")) for c in body["criteria"]}


def _check(web: TestClient, pid: int, check: str) -> tuple[bool | None, str | None]:
    return _criteria(web, pid)[check]


def _gate_rows(db: Session, pid: int) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(
        AuditDecision.agent == "gate",
        AuditDecision.subject_type == "project",
        AuditDecision.subject_id == str(pid),
    )
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def _decisions(db: Session) -> list[GateDecision]:
    db.expire_all()
    return list(db.scalars(select(GateDecision).order_by(GateDecision.id)))


def _baselines(db: Session) -> list[ProjectBaseline]:
    db.expire_all()
    return list(db.scalars(select(ProjectBaseline).order_by(ProjectBaseline.id)))


def _state(db: Session, pid: int) -> tuple[str, str, int]:
    db.expire_all()
    project = db.get(Project, pid)
    return project.stage, project.status, project.version


def _unmet_ids(response) -> list[str]:
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["message"] == PROJECT_UNMET
    return [c["id"] for c in response.json()["detail"]["unmet"]]


def _plan(db: Session, pid: int) -> ProjectPlan:
    return db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == pid))


def _add_risk(db: Session, pid: int, **changes) -> Risk:
    fields = {
        "project_id": pid,
        "kind": "threat",
        "title": "Data arrives late",
        "cause": "The client's export is manual",
        "effect": "The pilot slips",
        "category": "project",
        "objectives": ["schedule"],
        "response": "reduce",
        "response_plan": "Automate the export",
        "status": "open",
        "source": "person",
        **changes,
    }
    risk = Risk(**fields)
    db.add(risk)
    db.commit()
    return risk


# --- Reading the gate


def test_get_shows_the_gate_and_each_criterion(web, db, pm):
    pid = _project(db, pm)
    body = web.get(_url(pid)).json()
    assert body["gate"] == {
        "id": "go_ahead",
        "name": "Go-ahead gate",
        "from_stage": "planning",
        "to_stage": "delivery",
        "applies_to": "project",
        "deciders": ["sponsor"],
    }
    assert (body["project_stage"], body["project_status"], body["project_version"]) == (
        "planning",
        "on_track",
        1,
    )
    assert body["pm_user_id"] == pm.id
    assert body["checks_met"] is True
    assert body["latest_decision"] is None
    assert body["baseline"] is None
    kinds = {c["id"]: (c["kind"], c["met"]) for c in body["criteria"]}
    assert kinds == {
        **{check: ("check", True) for check in CHECK_IDS},
        "team_and_dates_realistic": ("person", None),
        "risks_acceptable": ("person", None),
    }


@pytest.mark.parametrize("role", ["sponsor", "project_manager", "auditor", "ethics_reviewer"])
def test_every_reader_sees_the_gate(web, db, sign_in, pm, role):
    pid = _project(db, pm)
    reader, _ = _client(db, sign_in, role, f"reader.{role}@pmo.example")
    assert reader.get(_url(pid)).status_code == 200


def test_an_unknown_project_is_404(web, pm, sponsor):
    assert web.get(_url(9999)).status_code == 404
    assert _decide(sponsor, 9999).status_code == 404


# --- Each automatic check, met and unmet


def test_plan_complete(web, db, pm):
    pid = _project(db, pm)
    assert _check(web, pid, "plan_complete") == (True, "nothing is missing")
    _plan(db, pid).report_period = None
    _plan(db, pid).report_due_day = None
    db.commit()
    assert _check(web, pid, "plan_complete") == (False, "missing: No reporting plan")


def test_plan_complete_is_unmet_without_a_plan(web, db, pm):
    pid = _project(db, pm)
    plan = _plan(db, pid)
    for model in (PlanDeliverable, PlanEstimateLine, PlanMilestone):
        for row in db.scalars(select(model).where(model.plan_id == plan.id)):
            db.delete(row)
    db.flush()
    db.delete(plan)
    db.commit()
    assert _check(web, pid, "plan_complete") == (False, "this project has no plan")
    assert _check(web, pid, "estimate_within_budget") == (False, "this project has no plan")


def test_estimate_within_budget(web, db, pm):
    # 818.18 plus 10% is 899.998, rounded half-up to 900.00.
    pid = _project(db, pm, budget_high="900.00")
    assert _check(web, pid, "estimate_within_budget") == (
        True,
        "the total 900.00 is within the top budget 900.00",
    )
    other = _project(db, pm, budget_high="899.99")
    assert _check(web, other, "estimate_within_budget") == (
        False,
        "the total 900.00 is above the top budget 899.99",
    )


def test_a_brief_with_no_top_budget_is_unmet(web, db, pm):
    pid = _project(db, pm, budget_high=None)
    assert _check(web, pid, "estimate_within_budget") == (False, NO_TOP_BUDGET)
    assert NO_TOP_BUDGET == "The brief has no top budget"


def test_a_project_with_no_brief_has_no_top_budget(web, db, pm):
    pid = _project(db, pm)
    db.delete(db.scalar(select(ProjectBrief).where(ProjectBrief.project_id == pid)))
    db.commit()
    assert _check(web, pid, "estimate_within_budget") == (False, NO_TOP_BUDGET)


def test_proposal_accepted_is_met_for_a_project_with_no_client(web, db, pm):
    pid = _project(db, pm)
    assert _check(web, pid, "proposal_accepted") == (True, "not needed: this project has no client")


def test_proposal_accepted_for_a_client_project(web, db, pm):
    pid = _project(db, pm, client=True)
    assert _check(web, pid, "proposal_accepted") == (
        True,
        "the client accepted the latest proposal",
    )
    proposal = db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == pid))
    proposal.status = "sent"
    db.commit()
    assert _check(web, pid, "proposal_accepted") == (
        False,
        "the latest proposal is sent, not accepted",
    )


def test_a_client_project_with_no_proposal_is_unmet(web, db, pm):
    pid = _project(db, pm, client=True)
    db.delete(db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == pid)))
    db.commit()
    assert _check(web, pid, "proposal_accepted") == (False, "this project has no proposal yet")


def test_no_unreviewed_risks(web, db, pm):
    pid = _project(db, pm)
    _add_risk(db, pid, status="open")
    _add_risk(db, pid, status="rejected", title="Rejected one")
    assert _check(web, pid, "no_unreviewed_risks") == (True, "no risk is still proposed")
    _add_risk(db, pid, status="proposed", title="Model drifts")
    assert _check(web, pid, "no_unreviewed_risks") == (False, "still proposed: 'Model drifts'")


def test_escalations_settled(web, db, pm):
    pid = _project(db, pm)
    # Settled escalations, and one on a risk that is no longer open, don't wait on anyone.
    _add_risk(db, pid, status="open")
    _add_risk(db, pid, status="accepted", escalated_to="sponsor", escalated_band="High")
    assert _check(web, pid, "escalations_settled") == (
        True,
        "no open risk is waiting on an escalation owner",
    )
    _add_risk(db, pid, title="Budget overrun", escalated_to="sponsor", escalated_band="High")
    assert _check(web, pid, "escalations_settled") == (
        False,
        "waiting on an escalation owner: 'Budget overrun' (with sponsor)",
    )


def test_readiness_not_red_for_a_project_with_no_request(web, db, pm):
    pid = _project(db, pm)
    assert _check(web, pid, "readiness_not_red") == (
        True,
        "not needed: this project didn't come from a request",
    )


def _from_request(web: TestClient, db: Session, pm: User, pid: int, uses_ai: bool, **ratings):
    rid = _create(web, uses_ai=uses_ai).json()["id"]
    if uses_ai:
        add_readiness(rid, pm.id, pm.id, **ratings)
    db.expire_all()
    db.get(ProjectRequest, rid).project_id = pid
    db.commit()


def test_readiness_not_red_reads_the_projects_request(web, db, pm):
    plain = _project(db, pm)
    _from_request(web, db, pm, plain, uses_ai=False)
    assert _check(web, plain, "readiness_not_red") == (
        True,
        "not needed: this request doesn't use AI",
    )
    green = _project(db, pm)
    _from_request(web, db, pm, green, uses_ai=True)
    assert _check(web, green, "readiness_not_red") == (True, "overall yellow")
    red = _project(db, pm)
    _from_request(web, db, pm, red, uses_ai=True, data_rating="red")
    assert _check(web, red, "readiness_not_red") == (False, "rated red: data")


# --- Passing


def test_a_sponsor_passes_and_the_plan_becomes_baseline_1(web, db, pm, sponsor):
    pid = _project(db, pm, client=True)
    response = _decide(sponsor, pid)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["project_stage"], body["project_status"], body["project_version"]) == (
        "delivery",
        "on_track",
        2,
    )
    assert body["latest_decision"]["outcome"] == "pass"
    assert body["latest_decision"]["reason"] == REASON
    assert _state(db, pid) == ("delivery", "on_track", 2)

    [decision] = _decisions(db)
    assert (decision.gate_id, decision.subject_type, decision.subject_id) == (
        "go_ahead",
        "project",
        pid,
    )
    [baseline] = _baselines(db)
    assert (baseline.project_id, baseline.number, baseline.gate_decision_id) == (
        pid,
        1,
        decision.id,
    )
    plan = baseline.plan
    assert plan["scope_in"] == "Weekly forecast for the pilot stores"
    assert plan["scope_out"] == "Other stores"
    assert [m["name"] for m in plan["milestones"]] == ["Pilot live"]
    assert plan["milestones"][0]["target_date"] == "2027-03-01"
    assert [(d["name"], d["milestone_name"]) for d in plan["deliverables"]] == [
        ("Forecast model", "Pilot live")
    ]
    assert [line["amount"] for line in plan["estimate_lines"]] == ["700.00", "118.18"]
    assert (plan["contingency_pct"], plan["lines_total"], plan["contingency_amount"]) == (
        10,
        "818.18",
        "81.82",
    )
    assert plan["total"] == "900.00"
    assert str(baseline.total) == "900.00"

    [row] = _gate_rows(db, pid)
    assert (row.status, row.trigger, row.version) == ("ok", "human", 2)
    assert row.action_taken["action"] == "gate_pass"
    assert row.action_taken["decision_id"] == decision.id
    assert row.action_taken["baseline_id"] == baseline.id
    assert row.signals["stage"] == "planning"
    assert row.output["stage"] == "delivery"

    shown = body["baseline"]
    assert (shown["id"], shown["number"], shown["total"]) == (baseline.id, 1, "900.00")
    assert shown["plan"]["total"] == "900.00"
    assert web.get(_url(pid)).json()["baseline"] == shown


def test_the_baseline_does_not_follow_later_plan_changes(web, db, pm, sponsor):
    pid = _project(db, pm)
    assert _decide(sponsor, pid).status_code == 200
    # Straight into the database: the routes refuse plan writes after planning.
    _plan(db, pid).scope_in = "Something else"
    db.commit()
    [baseline] = _baselines(db)
    assert baseline.plan["scope_in"] == "Weekly forecast for the pilot stores"


def test_a_failure_while_passing_leaves_no_baseline_and_no_decision(
    web, db, pm, sponsor, monkeypatch
):
    pid = _project(db, pm)

    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr("app.api.gates.freeze", broken)
    response = _decide(sponsor, pid)
    assert response.status_code == 500
    assert response.json()["detail"] == NOT_SAVED
    assert _state(db, pid) == ("planning", "on_track", 1)
    assert _decisions(db) == []
    assert _baselines(db) == []
    assert _gate_rows(db, pid) == []


def test_pass_with_an_unmet_check_is_a_409_naming_it(web, db, pm, sponsor):
    pid = _project(db, pm, budget_high="10.00")
    _add_risk(db, pid, status="proposed")
    response = _decide(sponsor, pid)
    assert _unmet_ids(response) == ["estimate_within_budget", "no_unreviewed_risks"]
    unmet = response.json()["detail"]["unmet"][0]
    assert unmet["text"] == "The plan's total is at or below the brief's top budget"
    assert unmet["detail"] == "the total 900.00 is above the top budget 10.00"
    assert _state(db, pid) == ("planning", "on_track", 1)
    assert _decisions(db) == [] and _baselines(db) == []
    [row] = _gate_rows(db, pid)
    assert (row.status, row.version) == ("rejected", 1)
    assert row.action_taken["refusal"] == "unmet_criteria"
    assert [c["id"] for c in row.action_taken["unmet"]] == [
        "estimate_within_budget",
        "no_unreviewed_risks",
    ]


@pytest.mark.parametrize(
    ("ticks", "unmet"),
    [
        ([], ["team_and_dates_realistic", "risks_acceptable"]),
        ([TICKS[0]], ["risks_acceptable"]),
        ([TICKS[0], {"id": "risks_acceptable", "met": False}], ["risks_acceptable"]),
    ],
)
def test_an_unticked_person_criterion_blocks_pass(web, db, pm, sponsor, ticks, unmet):
    pid = _project(db, pm)
    assert _unmet_ids(_decide(sponsor, pid, criteria=ticks)) == unmet
    assert _state(db, pid) == ("planning", "on_track", 1)


def test_a_pass_names_no_project_manager(web, db, pm, sponsor):
    pid = _project(db, pm)
    assert _decide(sponsor, pid, project_manager_id=pm.id).status_code == 422


# --- Return and decline


def test_return_keeps_the_project_in_planning_with_the_reason(web, db, pm, sponsor):
    pid = _project(db, pm, budget_high="10.00")
    reason = "The estimate is over the budget; trim the licence costs"
    response = _decide(sponsor, pid, "return", reason=reason, criteria=[])
    assert response.status_code == 200, response.text
    assert response.json()["latest_decision"]["outcome"] == "return"
    assert response.json()["latest_decision"]["reason"] == reason
    assert _state(db, pid) == ("planning", "on_track", 1)
    assert _baselines(db) == []
    # The plan can still change, and the project can be decided again.
    edit = web.patch(f"/api/v1/projects/{pid}/plan", json={"version": 1, "contingency_pct": 0})
    assert edit.status_code == 200, edit.text
    db.scalar(select(ProjectBrief).where(ProjectBrief.project_id == pid)).budget_high = 1000
    db.commit()
    assert _decide(sponsor, pid).status_code == 200
    assert [b.number for b in _baselines(db)] == [1]


def test_decline_cancels_the_project_and_keeps_its_stage(web, db, pm, sponsor):
    pid = _project(db, pm)
    reason = "The client has stopped the programme this year"
    response = _decide(sponsor, pid, "decline", reason=reason, criteria=[])
    assert response.status_code == 200, response.text
    assert response.json()["project_status"] == "cancelled"
    assert response.json()["latest_decision"]["reason"] == reason
    assert _state(db, pid) == ("planning", "cancelled", 2)
    assert _baselines(db) == []
    [row] = _gate_rows(db, pid)
    assert row.action_taken["action"] == "gate_decline"
    assert row.output["status"] == "cancelled"


def _assert_one_refusal(db: Session, pid: int, refusal: str, outcome: str, before) -> None:
    """Exactly one more `gate_refused` row naming the refusal; the project unchanged."""
    row = _gate_rows(db, pid)[-1]
    assert row.status == "rejected"
    assert row.action_taken["action"] == "gate_refused"
    assert row.action_taken["refusal"] == refusal
    assert row.action_taken["outcome"] == outcome
    assert _state(db, pid) == before


@pytest.mark.parametrize("outcome", ["pass", "return", "decline"])
def test_a_cancelled_project_takes_no_further_decision(web, db, pm, sponsor, outcome):
    pid = _project(db, pm)
    assert _decide(sponsor, pid, "decline", criteria=[]).status_code == 200
    before = _state(db, pid)
    response = _decide(sponsor, pid, outcome, version=2)
    assert response.status_code == 409
    assert response.json()["detail"] == PROJECT_CANCELLED
    _assert_one_refusal(db, pid, "cancelled", outcome, before)
    assert len(_decisions(db)) == 1


@pytest.mark.parametrize("outcome", ["pass", "return", "decline"])
def test_the_gate_is_decided_once(web, db, pm, sponsor, outcome):
    pid = _project(db, pm)
    assert _decide(sponsor, pid).status_code == 200
    before = _state(db, pid)
    response = _decide(sponsor, pid, outcome, version=2)
    assert response.status_code == 409
    assert response.json()["detail"] == NOT_IN_PLANNING
    _assert_one_refusal(db, pid, "not_in_planning", outcome, before)
    assert [b.number for b in _baselines(db)] == [1]


@pytest.mark.parametrize("outcome", ["pass", "return", "decline"])
def test_a_stale_version_is_a_409(web, db, pm, sponsor, outcome):
    pid = _project(db, pm)
    response = _decide(sponsor, pid, outcome, version=7)
    assert response.status_code == 409
    assert response.json()["detail"] == PROJECT_STALE
    _assert_one_refusal(db, pid, "stale_version", outcome, ("planning", "on_track", 1))


# --- Plan and proposal writes after the gate


def test_plan_writes_are_refused_once_the_project_is_in_delivery(web, db, pm, sponsor):
    pid = _project(db, pm)
    assert _decide(sponsor, pid).status_code == 200
    response = web.patch(f"/api/v1/projects/{pid}/plan", json={"version": 1, "contingency_pct": 5})
    assert response.status_code == 409
    assert response.json()["detail"] == NOT_PLANNING
    assert web.get(f"/api/v1/projects/{pid}/plan").json()["editable"] is False


def test_a_cancelled_project_takes_no_plan_writes(web, db, pm, sponsor):
    pid = _project(db, pm)
    assert _decide(sponsor, pid, "decline", criteria=[]).status_code == 200
    plan = f"/api/v1/projects/{pid}/plan"
    for response in (
        web.patch(plan, json={"version": 1, "contingency_pct": 5}),
        web.post(f"{plan}/deliverables", json={"version": 1, "name": "Another"}),
    ):
        assert response.status_code == 409
        assert response.json()["detail"] == PLAN_CANCELLED
    assert web.get(plan).json()["editable"] is False


def _proposal_refusals(db: Session, pid: int) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(
        AuditDecision.agent == "proposal", AuditDecision.status == "rejected"
    )
    return [r for r in db.scalars(stmt.order_by(AuditDecision.id)) if r.action_taken]


def test_a_cancelled_project_takes_no_proposal_writes(web, db, pm, sponsor):
    pid = _project(db, pm, client=True)
    proposal = db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == pid))
    proposal.status = "draft"
    db.commit()
    assert _decide(sponsor, pid, "decline", criteria=[]).status_code == 200
    url = f"/api/v1/projects/{pid}/proposal"
    for response in (
        web.patch(url, json={"version": 1, "summary": "A pilot"}),
        web.post(f"{url}/submit", json={"version": 1}),
        web.post(f"{url}/refresh", json={"version": 1}),
    ):
        assert response.status_code == 409
        assert response.json()["detail"] == PROPOSAL_CANCELLED
    rows = _proposal_refusals(db, pid)
    assert [r.action_taken["refusal"] for r in rows] == ["project_cancelled"] * 3


def test_a_cancelled_project_cannot_start_a_proposal(web, db, pm, sponsor):
    pid = _project(db, pm, client=True)
    db.delete(db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == pid)))
    db.commit()
    assert _decide(sponsor, pid, "decline", criteria=[]).status_code == 200
    response = web.post(f"/api/v1/projects/{pid}/proposal")
    assert response.status_code == 409
    assert response.json()["detail"] == PROPOSAL_CANCELLED
    [row] = _proposal_refusals(db, pid)
    assert (row.subject_type, row.subject_id) == ("project", str(pid))
    assert row.action_taken["refusal"] == "project_cancelled"


# --- Who decides


@pytest.mark.parametrize("role", ["project_manager", "auditor", "admin"])
def test_roles_without_gate_decide_are_refused_and_audited(web, db, sign_in, pm, role):
    pid = _project(db, pm)
    other, user = _client(db, sign_in, role, f"other.{role}@pmo.example")
    response = _decide(other, pid)
    assert response.status_code == 403
    assert response.json()["detail"] == FORBIDDEN
    [row] = _gate_rows(db, pid)
    assert (row.status, row.actor_user_id, row.version) == ("rejected", user.id, 1)
    assert row.action_taken["refusal"] == "no_permission"
    assert row.action_taken["actor_role"] == role
    assert _state(db, pid) == ("planning", "on_track", 1)
    assert _decisions(db) == []


def test_an_ethics_reviewer_is_refused_by_the_gates_role_list(web, db, sign_in, pm):
    pid = _project(db, pm)
    reviewer, user = _client(db, sign_in, "ethics_reviewer")
    response = _decide(reviewer, pid)
    assert response.status_code == 403
    assert response.json()["detail"] == "the go-ahead gate is decided only by: sponsor"
    [row] = _gate_rows(db, pid)
    assert (row.status, row.actor_user_id) == ("rejected", user.id)
    assert row.action_taken["refusal"] == "role_not_a_decider"
    assert _decisions(db) == []


def test_a_sponsor_who_manages_the_project_is_refused(web, db, sign_in, pm):
    sponsor_client, sponsor = _client(db, sign_in, "sponsor")
    pid = _project(db, sponsor)
    response = _decide(sponsor_client, pid)
    assert response.status_code == 403
    assert response.json()["detail"] == OWN_PROJECT
    [row] = _gate_rows(db, pid)
    assert row.action_taken["refusal"] == "own_project"
    assert _state(db, pid) == ("planning", "on_track", 1)
    assert _decisions(db) == []


# --- The baseline can't change


def test_no_route_changes_or_deletes_a_baseline():
    writes = [
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", ())
        if "baseline" in route.path and method in {"POST", "PUT", "PATCH", "DELETE"}
    ]
    assert writes == []


# --- Before the gates file is applied


@pytest.fixture
def applied_policies(monkeypatch):
    """The live gates file, which has no go-ahead gate until a person applies the draft."""
    from policy_files import APPLIED_POLICIES

    from app.config import get_settings
    from app.policy import get_policies

    monkeypatch.setattr(get_settings(), "policies_dir", APPLIED_POLICIES)
    get_policies.cache_clear()
    yield
    get_policies.cache_clear()


def test_the_routes_answer_503_until_the_go_ahead_gate_is_applied(
    web, db, pm, sponsor, applied_policies
):
    from app.policy import get_policies

    if "go_ahead" in get_policies().gates.gates:
        pytest.skip("the go-ahead gate has been applied")
    pid = _project(db, pm)
    for response in (web.get(_url(pid)), _decide(sponsor, pid)):
        assert response.status_code == 503
        assert response.json()["detail"] == GO_AHEAD_UNAVAILABLE
    assert _state(db, pid) == ("planning", "on_track", 1)
