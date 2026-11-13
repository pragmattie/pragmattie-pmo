"""The acceptance gate (item 7.6): each code check refuses by name, a pass moves the project to
closure, nobody decides the gate of a project they manage, every refusal is audited, and the
orchestrator asks for the gate (and each deliverable's answer) in the right person's Inbox."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_acceptance import make_project, offer

from app import delivery as rules
from app import orchestrator
from app import policy as policy_module
from app.api.gates import ACCEPTANCE_UNMET, NOT_IN_DELIVERY, OWN_PROJECT
from app.changes import waiting_changes
from app.config import get_settings
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    ChangeRequest,
    DeliverableAcceptance,
    GateDecision,
    InboxItem,
    PlanDeliverable,
    PlanMilestone,
    Project,
    ProjectPlan,
    Risk,
    User,
)

TODAY = date(2027, 6, 1)
REASON = "Everything agreed is accepted and every milestone is reached"
TICKS = [
    {"id": "scope_met", "met": True, "note": "Matches the scope in baseline 1"},
    {"id": "client_in_good_standing", "met": True, "note": "Invoices paid, no complaints"},
]
CHECK_IDS = [
    "deliverables_accepted",
    "milestones_reached",
    "no_waiting_changes",
    "escalations_settled",
]


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(rules, "today", lambda: TODAY)


@pytest.fixture
def pm(db, gates_policy, sign_in) -> tuple[TestClient, User]:
    web = TestClient(app, base_url="http://localhost")
    return web, sign_in(web, db, "project_manager")


@pytest.fixture
def sponsor(db, sign_in, pm) -> tuple[TestClient, User]:
    web = TestClient(app, base_url="http://localhost")
    return web, sign_in(web, db, "sponsor")


def _ready(db: Session, owner: User, **kwargs) -> int:
    """A project in delivery whose every acceptance check is met: its one deliverable accepted
    and its one milestone reached."""
    pid, did = make_project(db, owner, status="accepted", **kwargs)
    plan_id = db.scalar(select(ProjectPlan.id).where(ProjectPlan.project_id == pid))
    db.execute(
        update(PlanMilestone)
        .where(PlanMilestone.plan_id == plan_id)
        .values(reached_on=date(2027, 3, 1))
    )
    db.commit()
    return pid


def _url(pid: int) -> str:
    return f"/api/v1/projects/{pid}/acceptance-gate"


def _decide(web: TestClient, pid: int, outcome: str = "pass", version: int | None = None):
    if version is None:
        version = web.get(_url(pid)).json()["project_version"]
    body = {"version": version, "outcome": outcome, "reason": REASON, "criteria": TICKS}
    return web.post(_url(pid), json=body)


def _refusals(db: Session) -> list[str]:
    db.expire_all()
    stmt = select(AuditDecision).where(
        AuditDecision.agent == "gate", AuditDecision.status == "rejected"
    )
    return [r.action_taken["refusal"] for r in db.scalars(stmt.order_by(AuditDecision.id))]


def _change(db: Session, pid: int, user: User, status: str) -> ChangeRequest:
    change = ChangeRequest(
        project_id=pid,
        title="More analysis",
        reason="The client asked for more",
        changes={},
        effect={},
        decider="sponsor",
        measured_baseline_number=1,
        status=status,
        raised_by=user.id,
    )
    db.add(change)
    db.commit()
    return change


# --- The gate as shown


def test_the_gate_shows_its_criteria_and_the_go_ahead_route_is_unchanged(db, pm):
    web, user = pm
    pid = _ready(db, user)
    shown = web.get(_url(pid))
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert body["gate"] | {"deciders": None} == {
        "id": "acceptance",
        "name": "Acceptance gate",
        "from_stage": "delivery",
        "to_stage": "closure",
        "applies_to": "project",
        "deciders": None,
    }
    assert [c["id"] for c in body["criteria"] if c["kind"] == "check"] == CHECK_IDS
    assert body["checks_met"] is True
    assert body["baseline"]["number"] == 1
    # The go-ahead route keeps showing the go-ahead gate, for a project in delivery too.
    assert web.get(f"/api/v1/projects/{pid}/gate").json()["gate"]["id"] == "go_ahead"


# --- Each code check refuses by name


def _break(db: Session, pid: int, user: User, check: str) -> None:
    plan_id = db.scalar(select(ProjectPlan.id).where(ProjectPlan.project_id == pid))
    if check == "deliverables_accepted":
        db.execute(
            update(PlanDeliverable)
            .where(PlanDeliverable.plan_id == plan_id)
            .values(status="ready_for_acceptance")
        )
    elif check == "milestones_reached":
        db.execute(
            update(PlanMilestone).where(PlanMilestone.plan_id == plan_id).values(reached_on=None)
        )
    elif check == "no_waiting_changes":
        _change(db, pid, user, "submitted")
    elif check == "escalations_settled":
        db.add(
            Risk(
                project_id=pid,
                kind="threat",
                title="Key analyst leaves",
                cause="A tight market",
                effect="Handover slips",
                category="project",
                objectives=["schedule"],
                response="reduce",
                response_plan="Pair every analyst",
                source="person",
                status="open",
                probability=4,
                impact=4,
                score=16,
                band="High",
                escalated_to="sponsor",
                escalated_band="High",
            )
        )
    db.commit()


@pytest.mark.parametrize("check", CHECK_IDS)
def test_the_gate_refuses_while_a_check_fails_naming_it(db, pm, sponsor, check):
    _, user = pm
    pid = _ready(db, user)
    _break(db, pid, user, check)
    refused = _decide(sponsor[0], pid)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["message"] == ACCEPTANCE_UNMET
    assert [u["id"] for u in detail["unmet"]] == [check]
    assert _refusals(db) == ["unmet_criteria"]
    db.expire_all()
    assert db.get(Project, pid).stage == "delivery"


def test_the_checks_name_what_is_missing(db, pm):
    web, user = pm
    pid, _ = make_project(db, user)
    criteria = {c["id"]: c for c in web.get(_url(pid)).json()["criteria"]}
    assert criteria["deliverables_accepted"]["detail"] == (
        "not accepted yet: 'Model' (ready for acceptance)"
    )
    assert criteria["milestones_reached"]["detail"] == "not reached yet: 'Pilot live'"


def test_a_draft_change_does_not_wait_and_a_submitted_one_does(db, pm):
    _, user = pm
    pid = _ready(db, user)
    _change(db, pid, user, "draft")
    _change(db, pid, user, "approved")
    project = db.get(Project, pid)
    assert waiting_changes(db, pid) == []
    assert orchestrator.PROJECT_CHECKS["no_waiting_changes"](db, project).met
    submitted = _change(db, pid, user, "submitted")
    assert [c.id for c in waiting_changes(db, pid)] == [submitted.id]
    assert not orchestrator.PROJECT_CHECKS["no_waiting_changes"](db, project).met


def test_a_baseline_deliverable_no_longer_in_the_plan_is_not_accepted(db, pm):
    _, user = pm
    pid = _ready(db, user)
    db.add(
        PlanDeliverable(
            plan_id=db.scalar(select(ProjectPlan.id).where(ProjectPlan.project_id == pid)),
            name="Added after the baseline",
            status="in_progress",
        )
    )
    db.commit()
    # Only the latest baseline's deliverables count: one added since isn't in it.
    project = db.get(Project, pid)
    assert orchestrator.PROJECT_CHECKS["deliverables_accepted"](db, project).met


# --- Deciding


def test_a_pass_moves_the_project_to_closure_and_is_recorded(db, pm, sponsor):
    _, user = pm
    pid = _ready(db, user)
    passed = _decide(sponsor[0], pid)
    assert passed.status_code == 200, passed.text
    assert passed.json()["project_stage"] == "closure"
    assert passed.json()["latest_decision"]["gate_id"] == "acceptance"
    db.expire_all()
    assert db.get(Project, pid).stage == "closure"
    decision = db.scalar(select(GateDecision).where(GateDecision.gate_id == "acceptance"))
    assert (decision.outcome, decision.decided_by) == ("pass", sponsor[1].id)
    audit = db.scalar(
        select(AuditDecision).where(AuditDecision.agent == "gate", AuditDecision.status == "ok")
    )
    assert audit.action_taken["gate"] == "acceptance"
    assert audit.output["stage"] == "closure"
    # Decided once: a project in closure is no longer at the gate.
    again = _decide(sponsor[0], pid)
    assert again.status_code == 409
    assert again.json()["detail"] == NOT_IN_DELIVERY


def test_return_keeps_delivery_and_decline_cancels(db, pm, sponsor):
    _, user = pm
    pid = _ready(db, user)
    assert _decide(sponsor[0], pid, "return").status_code == 200
    db.expire_all()
    assert db.get(Project, pid).stage == "delivery"
    assert _decide(sponsor[0], pid, "decline").status_code == 200
    db.expire_all()
    project = db.get(Project, pid)
    assert (project.stage, project.status) == ("delivery", "cancelled")


def test_nobody_decides_the_gate_of_a_project_they_manage(db, sponsor):
    web, user = sponsor
    pid = _ready(db, user)  # the sponsor manages this one
    refused = _decide(web, pid)
    assert refused.status_code == 403
    assert refused.json()["detail"] == OWN_PROJECT
    assert _refusals(db) == ["own_project"]


def test_a_role_without_gate_decide_is_refused_and_audited(db, pm):
    web, user = pm
    pid = _ready(db, user)
    assert _decide(web, pid).status_code == 403
    assert _refusals(db) == ["no_permission"]


def test_a_project_in_planning_is_not_at_the_acceptance_gate(db, pm, sponsor):
    _, user = pm
    pid = _ready(db, user)
    db.execute(update(Project).where(Project.id == pid).values(stage="planning"))
    db.commit()
    refused = _decide(sponsor[0], pid)
    assert refused.status_code == 409
    assert _refusals(db) == ["not_in_delivery"]


# --- The orchestrator and the Inbox


@pytest.fixture
def settings(monkeypatch):
    current = get_settings()
    monkeypatch.setattr(current, "orchestrator_mode", "enforce")
    monkeypatch.setattr(current, "agent_mode", "enforce")
    monkeypatch.setattr(current, "simulated_client_delay_minutes", 0)
    return current


def _steps(db: Session, pid: int):
    policy = policy_module.get_autonomy()
    return orchestrator.subject_steps(db, policy, "project", pid)


def test_the_orchestrator_asks_for_the_gate_once_every_check_is_met(db, pm):
    _, user = pm
    pid = _ready(db, user)
    gate = [s for s in _steps(db, pid) if s.step == "pass_gate"]
    assert len(gate) == 1
    assert (gate[0].gate, gate[0].waits_on, gate[0].mode) == ("acceptance", "sponsor", "person")
    assert gate[0].reason == "the 4 acceptance checks are met"
    _break(db, pid, user, "milestones_reached")
    assert not [s for s in _steps(db, pid) if s.step == "pass_gate"]


def test_the_sponsor_sees_the_acceptance_gate_in_the_inbox(db, pm, sponsor, settings):
    _, user = pm
    sponsor_web, sponsor_user = sponsor
    pid = _ready(db, user, sponsor_id=sponsor_user.id)
    orchestrator.run_pass(db, settings, None)
    item = db.scalar(select(InboxItem).where(InboxItem.step == "pass_gate"))
    assert (item.subject_type, item.subject_id, item.who) == ("project", str(pid), "sponsor")
    inbox = sponsor_web.get("/api/v1/inbox")
    assert inbox.status_code == 200, inbox.text
    entry = next(e for e in inbox.json()["items"] if e["step"] == "pass_gate")
    assert entry["gate_id"] == "acceptance"
    assert [f["key"] for f in entry["facts"]] == CHECK_IDS


def test_a_real_clients_offer_waits_for_the_project_manager(db, pm, settings):
    web, user = pm
    pid, did = make_project(db, user)
    offer(web, pid, did)
    orchestrator.run_pass(db, settings, None)
    item = db.scalar(select(InboxItem).where(InboxItem.step == "client_acceptance"))
    assert (item.subject_type, item.subject_id, item.who) == (
        "deliverable",
        str(did),
        "project_manager",
    )
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "ready_for_acceptance"
    entry = next(
        e for e in web.get("/api/v1/inbox").json()["items"] if e["step"] == "client_acceptance"
    )
    assert entry["subject_title"] == "Deliverable 'Model'"
    assert entry["project_id"] == pid
    assert {f["key"]: f["value"] for f in entry["facts"]}["offered_to"] == "Dana Reyes, Harbour"


def test_with_no_client_the_offer_waits_for_the_sponsor(db, pm, sponsor, settings):
    web, user = pm
    pid, did = make_project(db, user, client=None, sponsor_id=sponsor[1].id)
    offer(web, pid, did, offered_to="The sponsor")
    orchestrator.run_pass(db, settings, None)
    item = db.scalar(select(InboxItem).where(InboxItem.step == "client_acceptance"))
    assert item.who == "sponsor"
    entry = next(
        e
        for e in sponsor[0].get("/api/v1/inbox").json()["items"]
        if e["step"] == "client_acceptance"
    )
    assert entry["recommendation"].startswith("Accept it if it meets what was agreed")


def test_the_simulated_client_answers_in_a_pass_and_says_so(db, pm, settings):
    web, user = pm
    pid, did = make_project(db, user, client="simulated")
    offer(web, pid, did)
    orchestrator.run_pass(db, settings, None)
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "accepted"
    answer = db.scalars(
        select(DeliverableAcceptance).order_by(DeliverableAcceptance.id.desc())
    ).first()
    assert (answer.kind, answer.source, answer.recorded_by) == ("answer", "simulated", None)
    shown = web.get(f"/api/v1/projects/{pid}/delivery").json()
    assert shown["deliverables"][0]["acceptance"][-1]["source"] == "simulated"
    assert db.scalar(select(InboxItem).where(InboxItem.step == "client_acceptance")) is None


def test_the_simulated_client_uses_the_configured_answer(db, pm, settings, monkeypatch):
    import dataclasses

    web, user = pm
    pid, did = make_project(db, user, client="simulated")
    offer(web, pid, did)
    policy = dataclasses.replace(policy_module.get_autonomy(), acceptance_answer="not_accepted")
    orchestrator.run_pass(db, settings, None, policy=policy)
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "in_progress"
    shown = web.get(f"/api/v1/projects/{pid}/delivery").json()["deliverables"][0]
    assert "simulated" in shown["acceptance"][-1]["missing"].lower()


def test_the_simulated_client_waits_for_its_delay(db, pm, settings, monkeypatch):
    web, user = pm
    monkeypatch.setattr(settings, "simulated_client_delay_minutes", 60)
    pid, did = make_project(db, user, client="simulated")
    offer(web, pid, did)
    offered_at = db.scalar(select(DeliverableAcceptance.created_at))
    orchestrator.run_pass(db, settings, None, now=offered_at + timedelta(minutes=30))
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "ready_for_acceptance"
    orchestrator.run_pass(db, settings, None, now=offered_at + timedelta(minutes=61))
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "accepted"
