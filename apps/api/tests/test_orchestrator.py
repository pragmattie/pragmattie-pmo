"""The orchestrator (item 6.2). Every test uses a fake Claude client or fake actions: none
touches the network."""

import dataclasses
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_intake import add_value_case
from test_intake_agent import FakeClaude
from test_intake_agent import _reply as intake_reply

import app.api.orchestrator as orchestrator_api
from app import orchestrator
from app import policy as policy_module
from app.agents import risk_agent
from app.autonomy import AGENT, PERSON, STEPS, WINDOW, Step
from app.config import Settings, get_settings
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    ChangeRequest,
    Client,
    InboxItem,
    OrchestratorAction,
    OrchestratorLease,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectBrief,
    ProjectPlan,
    ProjectProposal,
    ProjectRequest,
    Risk,
    User,
    ValueCase,
    utcnow,
)
from app.models.proposal import TEXT_SECTIONS
from app.orchestrator import NO_AGENT, StepAction, next_steps, run_pass

REQUEST = {
    "title": "Warehouse forecasting pilot",
    "asked_for": "A demand forecast for two warehouses",
    "problem": "Stock-outs every quarter",
    "outcome": "Fewer stock-outs",
}
RISK = {
    "kind": "threat",
    "title": "Model drift",
    "cause": "Training data ages",
    "effect": "Answers get worse",
    "category": "ai",
    "objectives": ["quality"],
    "response": "reduce",
    "response_plan": "Retrain every quarter",
    "source": "agent",
}


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
def settings(monkeypatch):
    """The settings, in `enforce` with the agents on, so a test only sets what it changes."""
    current = get_settings()
    monkeypatch.setattr(current, "orchestrator_mode", "enforce")
    monkeypatch.setattr(current, "agent_mode", "enforce")
    return current


@pytest.fixture
def rules():
    return policy_module.get_autonomy()


class Calls:
    """A fake action: keeps the subject ids it ran on, and fails while `fail` says so."""

    def __init__(self, fail: Exception | None = None):
        self.subjects: list[int] = []
        self.fail = fail

    def __call__(self, _ctx, subject_id: int):
        self.subjects.append(subject_id)
        if self.fail is not None:
            raise self.fail
        return None


@pytest.fixture
def actions(monkeypatch):
    """`actions(step=Calls(), ...)` replaces the action registry for the test."""

    def use(**fakes):
        registry = {step: StepAction(fake) for step, fake in fakes.items()}
        monkeypatch.setattr(orchestrator, "ACTIONS", registry)
        return fakes

    return use


def _with(rules, **steps):
    """`rules` with some steps replaced: `score_risk=dict(mode="window", window_minutes=15)`."""
    changed = dict(rules.steps)
    for step_id, fields in steps.items():
        changed[step_id] = dataclasses.replace(changed[step_id], **fields)
    return dataclasses.replace(rules, steps=changed)


def _user(db: Session, role: str = "project_manager", name: str = "pm") -> User:
    user = User(
        email=f"{name}@pmo.example", display_name=name, password_hash="not-a-hash", role=role
    )
    db.add(user)
    db.commit()
    return user


def _request(db: Session, user: User, status: str = "draft", uses_ai: bool = False) -> int:
    request = ProjectRequest(requested_by=user.id, status=status, uses_ai=uses_ai, **REQUEST)
    db.add(request)
    db.commit()
    return request.id


def _project(db: Session, stage: str = "planning", client: bool = False, **fields) -> Project:
    client_id = None
    if client:
        row = Client(name="Client Co")
        db.add(row)
        db.flush()
        client_id = row.id
    project = Project(
        name="Pilot",
        stage=stage,
        sponsor="S",
        pm="P",
        client_id=client_id,
        **{"status": "on_track", **fields},
    )
    db.add(project)
    db.commit()
    return project


def _complete_plan(db: Session, project: Project, owner: User) -> ProjectPlan:
    db.add(
        ProjectBrief(
            project_id=project.id,
            purpose="Why",
            objectives="What",
            decision_rights=[],
            budget_high=100,
        )
    )
    plan = ProjectPlan(
        project_id=project.id,
        scope_in="The pilot",
        contingency_pct=0,
        report_period="weekly",
        report_due_day=1,
    )
    db.add(plan)
    db.flush()
    milestone = PlanMilestone(
        plan_id=plan.id, name="Pilot", target_date=date(2027, 1, 1), owner_user_id=owner.id
    )
    db.add(milestone)
    db.add(PlanEstimateLine(plan_id=plan.id, description="Team", category="people", amount=1))
    db.flush()
    db.add(PlanDeliverable(plan_id=plan.id, name="Model", milestone_id=milestone.id))
    db.commit()
    return plan


def _risk_agent_ran(db: Session, project: Project) -> None:
    """An ok risk agent run on the project, so `propose_risks` is done."""
    db.add(
        AuditDecision(
            agent="risk_agent",
            trigger="human",
            subject_type="project",
            subject_id=str(project.id),
            status="ok",
        )
    )
    db.commit()


def _risk(db: Session, project: Project, **fields) -> Risk:
    risk = Risk(project_id=project.id, **{"status": "proposed", **RISK, **fields})
    db.add(risk)
    db.commit()
    return risk


def _proposal(db: Session, project: Project, user: User, status: str, filled: bool) -> int:
    texts = {name: "Written by a person" for name in TEXT_SECTIONS} if filled else {}
    proposal = ProjectProposal(
        project_id=project.id,
        status=status,
        created_by=user.id,
        price=Decimal("1.00"),
        estimate_lines=[],
        milestones=[],
        deliverables=[],
        risks=[],
        plan_version_seen=1,
        **texts,
    )
    db.add(proposal)
    db.commit()
    return proposal.id


def _change(db: Session, project: Project, user: User, decider: str) -> int:
    change = ChangeRequest(
        project_id=project.id,
        title="More analysis",
        reason="The client asked for more",
        changes={},
        effect={},
        decider=decider,
        measured_baseline_number=1,
        status="submitted",
        raised_by=user.id,
    )
    db.add(change)
    db.commit()
    return change.id


def _world(db: Session) -> dict[str, int]:
    """A subject in every state that has a next step."""
    pm = _user(db)
    ids = {
        "draft_value_case": _request(db, pm),
        "draft_readiness": _request(db, pm, uses_ai=True),
        "request_gate": _request(db, pm, status="submitted"),
    }
    add_value_case(ids["draft_readiness"], pm.id)
    bare = _project(db)
    ids["bare_project"] = bare.id
    ready = _project(db)
    _complete_plan(db, ready, pm)
    _risk_agent_ran(db, ready)
    ids["ready_project"] = ready.id
    delivery = _project(db, stage="delivery")
    ids["score_risk"] = _risk(db, delivery).id
    ids["open_low_risk"] = _risk(db, delivery, probability=1, impact=2, score=2, band="Low").id
    ids["accept_risk"] = _risk(
        db,
        delivery,
        status="open",
        probability=4,
        impact=4,
        score=16,
        band="High",
        escalated_to="sponsor",
        escalated_band="High",
    ).id
    for status, filled, name in (
        ("draft", False, "draft_proposal"),
        ("draft", True, "submit_proposal"),
        ("submitted", True, "approve_proposal"),
        ("approved", True, "send_proposal"),
        ("sent", True, "client_answer"),
    ):
        ids[name] = _proposal(db, _project(db, stage="delivery", client=True), pm, status, filled)
    ids["decide_change_pm"] = _change(db, delivery, pm, "project_manager")
    ids["decide_change_sponsor"] = _change(db, delivery, pm, "sponsor")
    return ids


def _found(steps, step: str, subject_id: int):
    matches = [s for s in steps if s.step == step and s.subject_id == subject_id]
    assert len(matches) == 1, (step, subject_id, steps)
    return matches[0]


def _orchestrator_rows(db: Session) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(AuditDecision.agent == "orchestrator")
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def _items(db: Session, status: str | None = None) -> list[InboxItem]:
    db.expire_all()
    stmt = select(InboxItem).order_by(InboxItem.id)
    if status is not None:
        stmt = stmt.where(InboxItem.status == status)
    return list(db.scalars(stmt))


def _actions(db: Session) -> list[OrchestratorAction]:
    db.expire_all()
    return list(db.scalars(select(OrchestratorAction).order_by(OrchestratorAction.id)))


def _pass(db, settings, now=None, rules=None, client=None):
    return run_pass(db, settings, client or FakeClaude(), policy=rules, now=now)


# --- The next-step engine


def test_every_step_id_comes_from_some_state(db, rules):
    ids = _world(db)
    steps = next_steps(db, rules)
    assert {s.step for s in steps} == set(STEPS)
    value_case = _found(steps, "draft_value_case", ids["draft_value_case"])
    assert (value_case.subject_type, value_case.mode) == ("request", AGENT)
    assert value_case.waits_on == "intake_agent"
    assert value_case.reason == "the request has no value case"
    readiness = _found(steps, "draft_readiness", ids["draft_readiness"])
    assert readiness.reason == "the request uses AI and has no readiness check"
    gate = _found(steps, "pass_gate", ids["request_gate"])
    assert (gate.subject_type, gate.mode, gate.waits_on) == ("request", PERSON, "sponsor")
    go_ahead = _found(steps, "pass_gate", ids["ready_project"])
    assert go_ahead.reason == "the 6 go-ahead checks are met"
    assert (go_ahead.subject_type, go_ahead.waits_on) == ("project", "sponsor")
    plan = _found(steps, "draft_plan", ids["bare_project"])
    assert plan.reason == "the plan is incomplete: the project has no plan"
    assert (plan.mode, plan.waits_on, plan.handoff) == (AGENT, "planner_agent", None)
    risks = _found(steps, "propose_risks", ids["bare_project"])
    assert (risks.mode, risks.waits_on) == (AGENT, "risk_agent")
    # The ready project's risks have been looked at, so it has no propose_risks step.
    assert not [
        s for s in steps if s.step == "propose_risks" and s.subject_id == ids["ready_project"]
    ]
    score = _found(steps, "score_risk", ids["score_risk"])
    assert (score.subject_type, score.mode, score.handoff) == ("risk", WINDOW, None)
    low = _found(steps, "open_low_risk", ids["open_low_risk"])
    assert low.reason == "the risk is proposed and scored Low"
    accept = _found(steps, "accept_risk", ids["accept_risk"])
    assert (accept.mode, accept.waits_on) == (PERSON, "sponsor")
    draft = _found(steps, "draft_proposal", ids["draft_proposal"])
    assert (draft.subject_type, draft.mode, draft.waits_on) == ("proposal", AGENT, "proposal_agent")
    assert _found(steps, "submit_proposal", ids["submit_proposal"]).handoff == NO_AGENT
    assert _found(steps, "approve_proposal", ids["approve_proposal"]).waits_on == "sponsor"
    send = _found(steps, "send_proposal", ids["send_proposal"])
    assert send.reason == "the proposal is approved and waiting to be sent"
    assert _found(steps, "client_answer", ids["client_answer"]).handoff == NO_AGENT
    assert _found(steps, "decide_change_pm", ids["decide_change_pm"]).waits_on == "project_manager"
    assert (
        _found(steps, "decide_change_sponsor", ids["decide_change_sponsor"]).waits_on == "sponsor"
    )
    # The same state gives the same steps, in the same order.
    assert next_steps(db, rules) == steps


def test_a_state_with_nothing_to_do(db, rules):
    assert next_steps(db, rules) == []
    pm = _user(db)
    for status in ("approved", "declined", "withdrawn"):
        _request(db, pm, status=status)
    with_value_case = _request(db, pm)
    add_value_case(with_value_case, pm.id)
    _project(db, status="cancelled")
    _project(db, stage="closed")
    delivery = _project(db, stage="delivery")
    _risk(db, delivery, status="closed")
    _risk(db, delivery, status="proposed", probability=4, impact=4, score=16, band="High")
    _proposal(db, delivery, pm, "accepted", True)
    assert next_steps(db, rules) == []


def test_unreviewed_agent_sections_hold_back_submit(db, rules):
    pm = _user(db)
    project = _project(db, stage="delivery", client=True)
    proposal_id = _proposal(db, project, pm, "draft", True)
    proposal = db.get(ProjectProposal, proposal_id)
    proposal.section_sources = {
        "summary": {"source": "agent", "reviewed_by": None, "reviewed_at": None}
    }
    db.commit()
    assert next_steps(db, rules) == []


def test_off_switches_judgement_and_agent_mode_send_steps_to_a_person(db, rules):
    pm = _user(db)
    request_id = _request(db, pm)
    delivery = _project(db, stage="delivery")
    risk = _risk(db, delivery, needs_judgement=True)
    window = _with(rules, score_risk={"mode": WINDOW})
    score = _found(next_steps(db, window), "score_risk", risk.id)
    assert score.mode == PERSON
    assert score.handoff == "the risk needs a person's judgement, so a person scores it"
    off = dataclasses.replace(rules, enabled={**rules.enabled, "intake_agent": False})
    step = _found(next_steps(db, off), "draft_value_case", request_id)
    assert (step.mode, step.waits_on) == (PERSON, "project_manager")
    assert step.handoff == "the intake_agent is switched off"
    step = _found(next_steps(db, rules, agent_mode="shadow"), "draft_value_case", request_id)
    assert (step.mode, step.handoff) == (PERSON, "the agents are off (AGENT_MODE=shadow)")


# --- Modes


def test_off_does_nothing(db, settings, monkeypatch, actions):
    monkeypatch.setattr(settings, "orchestrator_mode", "off")
    fakes = actions(draft_value_case=Calls())
    _world(db)
    report = _pass(db, settings)
    assert (report.mode, report.ran, report.outcomes) == ("off", False, [])
    assert fakes["draft_value_case"].subjects == []
    assert _orchestrator_rows(db) == [] and _items(db) == [] and _actions(db) == []
    assert db.scalar(select(OrchestratorLease)) is None


def test_shadow_records_once_and_changes_nothing(db, settings, monkeypatch, actions):
    monkeypatch.setattr(settings, "orchestrator_mode", "shadow")
    fakes = actions(draft_value_case=Calls(), propose_risks=Calls())
    ids = _world(db)
    report = _pass(db, settings)
    assert report.ran and report.agent_calls == 0
    rows = _orchestrator_rows(db)
    assert len(rows) == report.steps == len(report.outcomes)
    assert {r.status for r in rows} == {"skipped"}
    assert {r.trigger for r in rows} == {"orchestrator"}
    assert {r.actor_user_id for r in rows} == {None}
    assert {r.signals["step"] for r in rows} == set(STEPS)
    assert all(r.signals["policy_digest"] for r in rows)
    assert fakes["draft_value_case"].subjects == fakes["propose_risks"].subjects == []
    assert _items(db) == [] and _actions(db) == []
    assert db.get(ProjectRequest, ids["draft_value_case"]).status == "draft"
    # A second pass with nothing new writes nothing.
    assert _pass(db, settings).outcomes == []
    assert len(_orchestrator_rows(db)) == len(rows)
    # A changed reason is recorded again, for that step only.
    plan = ProjectPlan(project_id=ids["bare_project"], contingency_pct=0)
    db.add(plan)
    db.commit()
    report = _pass(db, settings)
    assert [(o.step, o.subject_id) for o in report.outcomes] == [
        ("draft_plan", ids["bare_project"])
    ]
    assert _orchestrator_rows(db)[-1].signals["reason"].startswith("the plan is incomplete: ")


# --- Enforce: agent steps


def test_enforce_runs_the_intake_agent_through_its_own_code_path(db, settings):
    pm = _user(db)
    request_id = _request(db, pm)
    claude = FakeClaude(intake_reply())
    report = _pass(db, settings, client=claude)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("draft_value_case", "done")]
    assert report.agent_calls == 1 and len(claude.requests) == 1
    value_case = db.scalar(select(ValueCase).where(ValueCase.request_id == request_id))
    assert (value_case.source, value_case.confirmed_by) == ("agent", None)
    run = db.scalar(select(AuditDecision).where(AuditDecision.agent == "intake_agent"))
    assert (run.trigger, run.actor_user_id, run.status) == ("orchestrator", None, "ok")
    [row] = _orchestrator_rows(db)
    assert (row.trigger, row.status, row.actor_user_id) == ("orchestrator", "ok", None)
    assert row.signals["step"] == "draft_value_case"
    assert row.signals["reason"] == "the request has no value case"
    assert row.signals["policy_digest"] == policy_module.get_autonomy().digest
    assert row.action_taken["agent_audit_id"] == run.id
    [action] = _actions(db)
    assert (action.status, action.open_key, action.attempts) == ("done", None, 0)
    # Running a pass again does no step twice, and a pass that does nothing writes nothing.
    assert _pass(db, settings, client=claude).outcomes == []
    assert len(claude.requests) == 1 and len(_orchestrator_rows(db)) == 1


def test_enforce_runs_the_risk_agent(db, settings):
    from test_risk_agent import _item
    from test_risk_agent import _reply as risk_reply

    project = _project(db, stage="planning")
    claude = FakeClaude(risk_reply(_item()))
    report = _pass(db, settings, client=claude)
    assert ("propose_risks", "done") in [(o.step, o.outcome) for o in report.outcomes]
    risks = list(db.scalars(select(Risk).where(Risk.project_id == project.id)))
    assert [(r.status, r.source) for r in risks] == [("proposed", "agent")]
    run = db.scalar(select(AuditDecision).where(AuditDecision.agent == "risk_agent"))
    assert (run.trigger, run.actor_user_id) == ("orchestrator", None)


def test_an_agent_step_already_done_but_still_open_goes_to_a_person(db, settings, actions):
    fakes = actions(draft_value_case=Calls())
    pm = _user(db)
    request_id = _request(db, pm)
    _pass(db, settings)
    assert fakes["draft_value_case"].subjects == [request_id]
    _pass(db, settings)
    assert fakes["draft_value_case"].subjects == [request_id]
    [item] = _items(db, "open")
    assert item.step == "draft_value_case" and item.who == "project_manager"
    assert item.reason.endswith(orchestrator.AGENT_ALREADY_RAN)


# --- Enforce: person steps and the inbox


def test_person_steps_get_one_inbox_item_each_and_close_when_done(db, settings, actions):
    actions()
    pm = _user(db)
    request_id = _request(db, pm, status="submitted")
    report = _pass(db, settings)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("pass_gate", "inbox")]
    [item] = _items(db)
    assert (item.step, item.subject_type, item.subject_id) == (
        "pass_gate",
        "request",
        str(request_id),
    )
    assert (item.who, item.status) == ("sponsor", "open")
    assert item.reason == "the request is waiting at the approval gate"
    assert _pass(db, settings).outcomes == []
    assert len(_items(db)) == 1
    request = db.get(ProjectRequest, request_id)
    request.status = "approved"
    db.commit()
    report = _pass(db, settings)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("pass_gate", "closed")]
    [item] = _items(db)
    assert (item.status, item.open_key) == ("done", None)
    assert item.closed_reason == orchestrator.NO_LONGER_NEEDED and item.closed_at is not None


def test_a_step_with_no_agent_yet_goes_to_a_person(db, settings, actions):
    actions()
    _project(db, stage="planning")
    _pass(db, settings)
    items = {i.step: i for i in _items(db)}
    assert (
        items["draft_plan"].reason == f"the plan is incomplete: the project has no plan; {NO_AGENT}"
    )
    assert items["propose_risks"].reason.endswith(NO_AGENT)
    assert {i.who for i in items.values()} == {"project_manager"}


def test_switching_from_enforce_to_shadow_records_each_step_once(db, settings, monkeypatch):
    # An enforce pass leaves a `skipped` row for a step waiting for its cap.
    monkeypatch.setattr(settings, "orchestrator_max_agent_calls", 0)
    request_id = _request(db, _user(db))
    _pass(db, settings)
    [wait] = _orchestrator_rows(db)
    assert (wait.status, wait.signals["orchestrator_mode"]) == ("skipped", "enforce")
    # The first shadow pass still records the step; the next one records nothing.
    monkeypatch.setattr(settings, "orchestrator_mode", "shadow")
    report = _pass(db, settings)
    assert [(o.step, o.subject_id, o.outcome) for o in report.outcomes] == [
        ("draft_value_case", request_id, "skipped")
    ]
    assert _orchestrator_rows(db)[-1].signals["orchestrator_mode"] == "shadow"
    assert _pass(db, settings).outcomes == []
    assert len(_orchestrator_rows(db)) == 2


def test_agent_mode_off_sends_agent_steps_to_a_person_without_retries(db, settings, monkeypatch):
    monkeypatch.setattr(settings, "agent_mode", "off")
    pm = _user(db)
    _request(db, pm)
    claude = FakeClaude()
    report = _pass(db, settings, client=claude)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("draft_value_case", "inbox")]
    assert claude.requests == [] and _actions(db) == []
    assert _items(db)[0].reason.endswith("the agents are off (AGENT_MODE=off)")


@pytest.mark.parametrize("unavailable", ["switched_off", "agent_mode"])
def test_a_handoff_item_is_cancelled_when_the_agent_is_available_again(
    db, settings, monkeypatch, rules, unavailable
):
    pm = _user(db)
    request_id = _request(db, pm)
    claude = FakeClaude(intake_reply())
    off_rules = rules
    if unavailable == "switched_off":
        off_rules = dataclasses.replace(rules, enabled={**rules.enabled, "intake_agent": False})
    else:
        monkeypatch.setattr(settings, "agent_mode", "off")
    report = _pass(db, settings, rules=off_rules, client=claude)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("draft_value_case", "inbox")]
    [item] = _items(db)
    assert (item.status, item.handoff) == ("open", True)
    # The agent is available again: the item is cancelled and the step runs, once.
    monkeypatch.setattr(settings, "agent_mode", "enforce")
    report = _pass(db, settings, rules=rules, client=claude)
    assert [(o.step, o.outcome) for o in report.outcomes] == [
        ("draft_value_case", "cancelled"),
        ("draft_value_case", "done"),
    ]
    [item] = _items(db)
    assert (item.status, item.open_key) == ("cancelled", None)
    assert item.closed_reason == orchestrator.AGENT_BACK and item.closed_at is not None
    cancel = [r for r in _orchestrator_rows(db) if r.action_taken["action"] == "cancel_inbox"]
    assert [(r.status, r.action_taken["inbox_item_id"]) for r in cancel] == [("ok", item.id)]
    assert db.scalar(select(ValueCase).where(ValueCase.request_id == request_id)) is not None
    assert len(claude.requests) == 1
    assert _pass(db, settings, rules=rules, client=claude).outcomes == []
    assert len(claude.requests) == 1


def test_items_opened_for_other_reasons_stay_open_when_the_agent_is_available(
    db, settings, actions, rules
):
    """A person step's item and a judgement hand-off stay open whatever the agents do."""
    actions(score_risk=Calls())
    pm = _user(db)
    _request(db, pm, status="submitted")
    _risk(db, _project(db, stage="delivery"), needs_judgement=True)
    window = _window_rules(rules)
    _pass(db, settings, rules=window)
    items = _items(db, "open")
    assert {i.step for i in items} == {"pass_gate", "score_risk"}
    assert not any(i.handoff for i in items)
    assert _pass(db, settings, rules=window).outcomes == []
    assert len(_items(db, "open")) == 2


def test_enforce_never_performs_a_person_step_whatever_the_policy_says(
    db, settings, monkeypatch, actions, rules
):
    """Even a policy built in code that gives every person step to an agent, with an action
    registered for each, never gets a gate decision, an approval, a send, an acceptance or a
    change decision out of a pass."""
    import app.api.changes as changes_api
    import app.api.gates as gates_api
    import app.api.proposals as proposals_api
    import app.api.risks as risks_api

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a pass performed a person step")

    for module, names in (
        (gates_api, ("decide_gate", "decide_project_gate", "_decide")),
        (proposals_api, ("approve_proposal", "mark_sent", "record_answer", "_decide")),
        (risks_api, ("accept_risk", "_decide_proposed")),
        (changes_api, ("decide_change",)),
    ):
        for name in names:
            # raising=True: a renamed function fails here rather than going unwatched.
            monkeypatch.setattr(module, name, forbidden, raising=True)
    person_steps = sorted(orchestrator.PERSON_ONLY)
    fakes = {step: Calls() for step in person_steps}
    monkeypatch.setattr(
        orchestrator, "ACTIONS", {step: StepAction(fake) for step, fake in fakes.items()}
    )
    greedy = _with(rules, **{step: {"mode": AGENT, "agent": "risk_agent"} for step in person_steps})
    ids = _world(db)
    report = _pass(db, settings, rules=greedy)
    assert all(fake.subjects == [] for fake in fakes.values())
    inbox = {(i.step, i.subject_id) for i in _items(db, "open")}
    for step, key in (
        ("pass_gate", "request_gate"),
        ("pass_gate", "ready_project"),
        ("approve_proposal", "approve_proposal"),
        ("send_proposal", "send_proposal"),
        ("accept_risk", "accept_risk"),
        ("decide_change_pm", "decide_change_pm"),
        ("decide_change_sponsor", "decide_change_sponsor"),
    ):
        assert (step, str(ids[key])) in inbox
    assert not [o for o in report.outcomes if o.step in person_steps and o.outcome == "done"]


# --- Enforce: windows and holds


def _window_rules(rules):
    return _with(rules, score_risk={"mode": WINDOW, "window_minutes": 15})


def test_a_window_step_waits_for_its_due_time_then_runs_once(db, settings, actions, rules):
    fakes = actions(score_risk=Calls())
    risk = _risk(db, _project(db, stage="delivery"))
    window = _window_rules(rules)
    start = utcnow()
    report = _pass(db, settings, now=start, rules=window)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("score_risk", "pending")]
    [action] = _actions(db)
    assert (action.status, action.mode, action.who) == ("pending", WINDOW, "project_manager")
    assert action.due_at == start + timedelta(minutes=15)
    [row] = _orchestrator_rows(db)
    assert row.action_taken["action"] == "window"
    _pass(db, settings, now=start + timedelta(minutes=10), rules=window)
    assert fakes["score_risk"].subjects == []
    report = _pass(db, settings, now=start + timedelta(minutes=16), rules=window)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("score_risk", "done")]
    assert fakes["score_risk"].subjects == [risk.id]
    assert _actions(db)[0].status == "done"
    _pass(db, settings, now=start + timedelta(minutes=30), rules=window)
    assert fakes["score_risk"].subjects == [risk.id]


def _hold(web: TestClient, action_id: int, reason: str = "I want to score this one myself"):
    return web.post(f"/api/v1/orchestrator/actions/{action_id}/hold", json={"reason": reason})


def _refusals(db: Session) -> list[AuditDecision]:
    return [r for r in _orchestrator_rows(db) if r.status == "rejected"]


def test_a_hold_stops_a_window_action_and_makes_an_inbox_item(
    db, web, settings, actions, rules, sign_in
):
    fakes = actions(score_risk=Calls())
    risk = _risk(db, _project(db, stage="delivery"))
    window = _window_rules(rules)
    start = utcnow()
    _pass(db, settings, now=start, rules=window)
    [action] = _actions(db)
    pm = sign_in(web, db, "project_manager")
    response = _hold(web, action.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["action"]["status"] == "held" and body["action"]["held_by"] == pm.id
    [item] = _items(db)
    assert (item.id, item.status, item.who) == (body["inbox_item_id"], "open", "project_manager")
    assert (item.step, item.subject_id, item.action_id) == ("score_risk", str(risk.id), action.id)
    assert "I want to score this one myself" in item.reason
    hold_row = _orchestrator_rows(db)[-1]
    assert (hold_row.trigger, hold_row.actor_user_id) == ("human", pm.id)
    assert hold_row.action_taken["action"] == "hold"
    _pass(db, settings, now=start + timedelta(minutes=20), rules=window)
    assert fakes["score_risk"].subjects == []
    assert len(_items(db)) == 1 and len(_actions(db)) == 1
    # Holding it again is refused: it is no longer pending.
    assert _hold(web, action.id).status_code == 409
    assert _refusals(db)[-1].action_taken["refusal"] == "not_pending"


@pytest.mark.parametrize(("role", "refusal"), [("sponsor", "wrong_role"), ("admin", "forbidden")])
def test_holding_needs_the_steps_who_role_and_refusals_are_audited(
    db, web, settings, actions, rules, sign_in, role, refusal
):
    actions(score_risk=Calls())
    risk = _risk(db, _project(db, stage="delivery"))
    _pass(db, settings, rules=_window_rules(rules))
    [action] = _actions(db)
    user = sign_in(web, db, role)
    assert _hold(web, action.id).status_code == 403
    [row] = _refusals(db)
    assert (row.subject_type, row.subject_id) == ("risk", str(risk.id))
    assert (row.actor_user_id, row.trigger) == (user.id, "human")
    assert row.action_taken["refusal"] == refusal
    assert _actions(db)[0].status == "pending" and _items(db) == []


class HoldDuringCall:
    """A fake agent action during whose run a person holds the pending action. Then, like an
    agent, it changes the subject and writes its own run row with a cost.

    A fake, not the real intake agent: SQLite's driver commits a savepoint opened outside a
    transaction when it is released, so the agent's saved draft couldn't be rolled back there.
    On MySQL the savepoint sits inside the pass's transaction and is rolled back with it."""

    def __init__(self, web: TestClient):
        self.web = web
        self.responses = []
        self.subjects: list[int] = []

    def __call__(self, ctx, subject_id: int) -> int:
        self.subjects.append(subject_id)
        with Session(engine) as other:
            [action] = other.scalars(select(OrchestratorAction))
        self.responses.append(_hold(self.web, action.id))
        ctx.db.get(ProjectRequest, subject_id).problem = "rewritten by the agent"
        run = AuditDecision(
            agent="intake_agent",
            trigger="orchestrator",
            subject_type="project_request",
            subject_id=str(subject_id),
            status="ok",
            model="claude-test",
            input_tokens=10,
            output_tokens=20,
            cost_usd=Decimal("0.25"),
            action_taken={"action": "draft", "saved": ["problem"]},
        )
        ctx.db.add(run)
        ctx.db.flush()
        return run.id


def test_a_hold_during_the_agent_call_stands_and_the_result_is_not_applied(
    db, web, settings, monkeypatch, sign_in
):
    pm = sign_in(web, db, "project_manager")
    request_id = _request(db, pm)
    fake = HoldDuringCall(web)
    monkeypatch.setattr(orchestrator, "ACTIONS", {"draft_value_case": StepAction(fake)})
    report = _pass(db, settings)
    # The hold got a clean response, not a lock wait.
    [response] = fake.responses
    assert response.status_code == 200, response.text
    assert [(o.step, o.outcome) for o in report.outcomes] == [("draft_value_case", "held")]
    [action] = _actions(db)
    assert (action.status, action.held_by, action.attempts) == ("held", pm.id, 0)
    [item] = _items(db, "open")
    assert item.action_id == action.id and not item.handoff
    # The agent's change was not applied; its run is recorded as not applied, cost and all.
    assert db.get(ProjectRequest, request_id).problem == REQUEST["problem"]
    [run] = db.scalars(select(AuditDecision).where(AuditDecision.agent == "intake_agent"))
    assert (run.status, run.trigger, run.cost_usd) == ("skipped", "orchestrator", Decimal("0.25"))
    assert (run.model, run.input_tokens, run.output_tokens) == ("claude-test", 10, 20)
    assert run.action_taken == {
        "action": "draft",
        "saved": ["problem"],
        "applied": False,
        "not_applied_reason": orchestrator.NOT_APPLIED,
    }
    assert orchestrator.spent_today(db, utcnow()) == Decimal("0.25")
    row = _orchestrator_rows(db)[-1]
    assert (row.status, row.trigger) == ("skipped", "orchestrator")
    assert row.action_taken == {
        "action": "run",
        "action_id": action.id,
        "agent_audit_id": run.id,
        "applied": False,
        "action_status": "held",
        "reason": orchestrator.NOT_APPLIED,
    }
    # The hold stands: later passes don't run the step again.
    _pass(db, settings)
    assert fake.subjects == [request_id]
    assert _actions(db)[0].status == "held" and len(_items(db, "open")) == 1


def test_a_run_that_fails_while_held_is_not_retried(db, web, settings, monkeypatch, sign_in):
    sign_in(web, db, "project_manager")
    _request(db, _user(db))
    fake = HoldDuringCall(web)

    def hold_then_fail(ctx, subject_id):
        fake(ctx, subject_id)
        raise RuntimeError("the model was down")

    monkeypatch.setattr(orchestrator, "ACTIONS", {"draft_value_case": StepAction(hold_then_fail)})
    start = utcnow()
    report = _pass(db, settings, now=start)
    assert fake.responses[0].status_code == 200
    assert [(o.step, o.outcome) for o in report.outcomes] == [("draft_value_case", "held")]
    [action] = _actions(db)
    assert (action.status, action.attempts, action.next_try_at) == ("held", 0, None)
    error = _orchestrator_rows(db)[-1]
    assert (error.status, error.action_taken["action_status"]) == ("error", "held")
    _pass(db, settings, now=start + timedelta(minutes=10))
    assert len(fake.subjects) == 1


def test_a_missing_reason_is_an_audited_refusal(db, web, settings, actions, rules, sign_in):
    actions(score_risk=Calls())
    _risk(db, _project(db, stage="delivery"))
    _pass(db, settings, rules=_window_rules(rules))
    [action] = _actions(db)
    sign_in(web, db, "project_manager")
    response = web.post(f"/api/v1/orchestrator/actions/{action.id}/hold", json={})
    assert response.status_code == 422
    assert response.json()["detail"] == orchestrator_api.SHORT_REASON
    assert _refusals(db)[-1].action_taken["refusal"] == "short_reason"
    assert _actions(db)[0].status == "pending"


def test_holding_needs_a_reason_of_ten_characters(db, web, settings, actions, rules, sign_in):
    actions(score_risk=Calls())
    _risk(db, _project(db, stage="delivery"))
    _pass(db, settings, rules=_window_rules(rules))
    [action] = _actions(db)
    sign_in(web, db, "project_manager")
    assert _hold(web, action.id, "  too short ").status_code == 422
    assert _refusals(db)[-1].action_taken["refusal"] == "short_reason"
    assert _actions(db)[0].status == "pending"
    assert _hold(web, 9999).status_code == 404


# --- Retries and caps


def test_a_failing_step_is_retried_three_times_five_minutes_apart_then_goes_to_a_person(
    db, settings, actions
):
    fakes = actions(draft_value_case=Calls(fail=RuntimeError("the model was down")))
    request_id = _request(db, _user(db))
    start = utcnow()
    report = _pass(db, settings, now=start)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("draft_value_case", "failed")]
    _pass(db, settings, now=start + timedelta(minutes=4))
    assert len(fakes["draft_value_case"].subjects) == 1
    for minutes in (5, 10, 15):
        _pass(db, settings, now=start + timedelta(minutes=minutes))
    assert fakes["draft_value_case"].subjects == [request_id] * 4
    [action] = _actions(db)
    assert (action.status, action.attempts, action.open_key) == ("cancelled", 4, None)
    assert action.last_error == "RuntimeError: the model was down"
    [item] = _items(db, "open")
    assert item.action_id == action.id
    assert item.reason.endswith("the intake_agent failed after 3 retries")
    handed = [r for r in _orchestrator_rows(db) if r.action_taken.get("action") == "inbox"]
    assert handed[-1].action_taken["reason"] == item.reason
    errors = [r for r in _orchestrator_rows(db) if r.status == "error"]
    assert len(errors) == 4 and errors[0].error == "RuntimeError: the model was down"
    _pass(db, settings, now=start + timedelta(minutes=30))
    assert len(fakes["draft_value_case"].subjects) == 4


def test_an_agent_error_keeps_the_agents_own_error_row(db, settings):
    _request(db, _user(db))
    claude = FakeClaude(error=RuntimeError("timeout"))
    _pass(db, settings, client=claude)
    run = db.scalar(select(AuditDecision).where(AuditDecision.agent == "intake_agent"))
    assert (run.status, run.trigger) == ("error", "orchestrator")
    [row] = [r for r in _orchestrator_rows(db) if r.status == "error"]
    assert row.action_taken["agent_audit_id"] == run.id


def test_one_failing_subject_does_not_stop_the_pass(db, settings, actions):
    pm = _user(db)
    first, second = _request(db, pm), _request(db, pm)

    def flaky(_ctx, subject_id):
        if subject_id == first:
            raise ValueError("bad data")

    actions(draft_value_case=flaky)
    report = _pass(db, settings)
    assert [(o.subject_id, o.outcome) for o in report.outcomes] == [
        (first, "failed"),
        (second, "done"),
    ]


def test_the_per_pass_cap(db, settings, monkeypatch, actions):
    monkeypatch.setattr(settings, "orchestrator_max_agent_calls", 2)
    fakes = actions(draft_value_case=Calls())
    pm = _user(db)
    ids = [_request(db, pm) for _ in range(3)]
    report = _pass(db, settings)
    assert report.agent_calls == 2
    assert fakes["draft_value_case"].subjects == ids[:2]
    waiting = [o for o in report.outcomes if o.outcome == "waiting"]
    assert [o.subject_id for o in waiting] == [ids[2]]
    pending = [a for a in _actions(db) if a.status == "pending"]
    assert [a.wait_reason for a in pending] == [orchestrator.PASS_CAP.format(n=2)]
    _pass(db, settings)
    assert fakes["draft_value_case"].subjects == ids


def test_the_daily_cap_counts_todays_orchestrator_runs(db, settings, monkeypatch, actions):
    monkeypatch.setattr(settings, "orchestrator_daily_cap_usd", Decimal("1.00"))
    fakes = actions(draft_value_case=Calls())
    pm = _user(db)
    request_id = _request(db, pm)
    # A person's run doesn't count; the orchestrator's do.
    costs = (("human", "9.00"), ("orchestrator", "0.60"), ("orchestrator", "0.40"))
    for attempt, (trigger, cost) in enumerate(costs, start=1):
        db.add(
            AuditDecision(
                agent="intake_agent",
                trigger=trigger,
                subject_type="project_request",
                subject_id=str(request_id),
                attempt=attempt,
                status="ok",
                cost_usd=Decimal(cost),
            )
        )
    db.commit()
    now = utcnow()
    assert orchestrator.spent_today(db, now) == Decimal("1.00")
    report = _pass(db, settings, now=now)
    assert fakes["draft_value_case"].subjects == []
    assert [o.outcome for o in report.outcomes] == ["waiting"]
    day_rows = [r for r in _orchestrator_rows(db) if r.subject_type == "orchestrator_day"]
    assert len(day_rows) == 1 and day_rows[0].action_taken["action"] == "daily_cap"
    _pass(db, settings, now=now)
    assert len([r for r in _orchestrator_rows(db) if r.subject_type == "orchestrator_day"]) == 1
    _pass(db, settings, now=now + timedelta(days=1))
    assert fakes["draft_value_case"].subjects == [request_id]


def _cap_rows(db: Session) -> list[AuditDecision]:
    return [r for r in _orchestrator_rows(db) if (r.action_taken or {}).get("action") == "wait"]


def test_the_per_pass_cap_writes_one_row_when_a_step_first_waits(
    db, settings, monkeypatch, actions
):
    monkeypatch.setattr(settings, "orchestrator_max_agent_calls", 1)
    actions(draft_value_case=Calls())
    pm = _user(db)
    ids = [_request(db, pm) for _ in range(3)]
    _pass(db, settings)
    reason = orchestrator.PASS_CAP.format(n=1)
    rows = _cap_rows(db)
    assert [int(r.subject_id) for r in rows] == ids[1:]
    for row in rows:
        assert row.trigger == "orchestrator" and row.status == "skipped"
        assert row.action_taken["reason"] == reason
        assert row.signals["step"] == "draft_value_case" and row.signals["policy_digest"]
    # The next pass runs one more and defers the last for the same reason: no new row.
    _pass(db, settings)
    assert len(_cap_rows(db)) == 2


def test_the_daily_cap_writes_one_row_when_a_step_first_waits(db, settings, monkeypatch, actions):
    monkeypatch.setattr(settings, "orchestrator_daily_cap_usd", Decimal("1.00"))
    actions(draft_value_case=Calls())
    request_id = _request(db, _user(db))
    db.add(
        AuditDecision(
            agent="intake_agent",
            trigger="orchestrator",
            subject_type="project_request",
            subject_id=str(request_id),
            attempt=1,
            status="ok",
            cost_usd=Decimal("1.00"),
        )
    )
    db.commit()
    now = utcnow()
    _pass(db, settings, now=now)
    rows = _cap_rows(db)
    assert len(rows) == 1
    assert rows[0].subject_id == str(request_id) and rows[0].status == "skipped"
    assert rows[0].trigger == "orchestrator"
    assert rows[0].action_taken["reason"] == orchestrator.DAY_CAP.format(cap="1.00")
    assert rows[0].signals["step"] == "draft_value_case" and rows[0].signals["policy_digest"]
    before = len(_orchestrator_rows(db))
    _pass(db, settings, now=now)
    assert len(_orchestrator_rows(db)) == before


def test_the_loop_survives_a_failing_pass(monkeypatch, capsys):
    calls = []

    def fake_run_pass(_db, settings, _client):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database unreachable")
        if len(calls) == 2:
            return orchestrator.PassReport(mode=settings.orchestrator_mode, ran=True)
        raise KeyboardInterrupt

    sleeps = []
    monkeypatch.setattr(orchestrator, "run_pass", fake_run_pass)
    monkeypatch.setattr(orchestrator.risk_agent, "AnthropicClient", lambda _s: None)
    monkeypatch.setattr(orchestrator.time, "sleep", sleeps.append)
    assert orchestrator.main(["run"]) == 0
    assert len(calls) == 3 and len(sleeps) == 2
    out = capsys.readouterr().out
    assert "ran True" in out and "orchestrator: stopped" in out


# --- Overlap and the route


def test_a_pass_gives_up_while_another_holds_the_lease(db, settings, actions):
    fakes = actions(draft_value_case=Calls())
    _request(db, _user(db))
    db.add(
        OrchestratorLease(name="pass", holder="other", expires_at=utcnow() + timedelta(minutes=5))
    )
    db.commit()
    report = _pass(db, settings)
    assert report.busy and not report.ran and fakes["draft_value_case"].subjects == []
    lease = db.get(OrchestratorLease, "pass")
    lease.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert _pass(db, settings).ran
    assert len(fakes["draft_value_case"].subjects) == 1


def _set_lease(holder: str | None, expires_in: timedelta) -> None:
    """Change the lease from another session: `holder` None keeps this pass's."""
    with Session(engine) as other:
        lease = other.get(OrchestratorLease, "pass")
        lease.holder = holder or lease.holder
        lease.expires_at = utcnow() + expires_in
        other.commit()


def test_a_long_pass_renews_its_lease_before_each_agent_call(db, settings, actions):
    pm = _user(db)
    first, second = _request(db, pm), _request(db, pm)
    calls: list[int] = []

    def run(_ctx, subject_id: int):
        calls.append(subject_id)
        if len(calls) == 1:  # a call so long that the lease runs out, with no other pass
            _set_lease(None, -timedelta(seconds=1))

    actions(draft_value_case=run)
    report = _pass(db, settings)
    assert calls == [first, second]
    assert [o.outcome for o in report.outcomes] == ["done", "done"]


def test_a_pass_whose_lease_ran_out_does_no_step_twice(db, settings, monkeypatch, actions):
    pm = _user(db)
    first, second = _request(db, pm), _request(db, pm)
    calls: list[int] = []
    actions(draft_value_case=lambda _ctx, subject_id: calls.append(subject_id))
    # The lease runs out just before the second step's agent call, and a second pass takes it.
    spent_today = orchestrator.spent_today
    checks = []

    def spent_then_lose(db_, now):
        checks.append(now)
        if len(checks) == 2:
            _set_lease("other", timedelta(minutes=5))
        return spent_today(db_, now)

    monkeypatch.setattr(orchestrator, "spent_today", spent_then_lose)
    report = _pass(db, settings)
    assert calls == [first]
    assert [(o.subject_id, o.outcome, o.detail) for o in report.outcomes][1:] == [
        (second, "skipped", orchestrator.LEASE_LOST_BEFORE)
    ]
    row = _orchestrator_rows(db)[-1]
    assert (row.status, row.action_taken["reason"]) == ("skipped", orchestrator.LEASE_LOST_BEFORE)
    # The pass that took over does the second step, once.
    monkeypatch.setattr(orchestrator, "spent_today", spent_today)
    _set_lease("other", -timedelta(seconds=1))
    _pass(db, settings)
    _pass(db, settings)
    assert calls == [first, second]


def test_a_result_is_not_applied_when_the_lease_is_lost_during_the_call(db, settings, actions):
    pm = _user(db)
    first, second = _request(db, pm), _request(db, pm)
    calls: list[int] = []

    def run(_ctx, subject_id: int):
        calls.append(subject_id)
        if len(calls) == 1:
            _set_lease("other", timedelta(minutes=5))

    actions(draft_value_case=run)
    report = _pass(db, settings)
    # The result is not applied, the action stays pending, and the pass stops there.
    assert calls == [first]
    assert [(o.subject_id, o.outcome, o.detail) for o in report.outcomes] == [
        (first, "skipped", orchestrator.LEASE_LOST)
    ]
    [action] = _actions(db)
    assert (action.status, action.attempts) == ("pending", 0)
    row = _orchestrator_rows(db)[-1]
    assert (row.status, row.action_taken["applied"]) == ("skipped", False)
    assert row.action_taken["reason"] == orchestrator.LEASE_LOST
    # The pass that took over runs each step once.
    _set_lease("other", -timedelta(seconds=1))
    _pass(db, settings)
    _pass(db, settings)
    assert calls == [first, first, second]
    assert {a.status for a in _actions(db)} == {"done"}


def test_the_per_pass_cap_is_at_most_20():
    with pytest.raises(ValidationError):
        Settings(orchestrator_max_agent_calls=21)
    assert Settings(orchestrator_max_agent_calls=20).orchestrator_max_agent_calls == 20


def test_admin_runs_a_pass_on_demand(db, web, settings, sign_in):
    claude = FakeClaude(intake_reply())
    app.dependency_overrides[orchestrator_api.get_claude_client] = lambda: claude
    try:
        sign_in(web, db, "admin")
        pm = _user(db)
        _request(db, pm)
        response = web.post("/api/v1/orchestrator/run")
    finally:
        app.dependency_overrides.pop(orchestrator_api.get_claude_client, None)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["mode"], body["ran"], body["agent_calls"]) == ("enforce", True, 1)
    assert body["outcomes"][0]["outcome"] == "done"


def test_the_route_runs_nothing_when_the_orchestrator_is_off(db, web, sign_in):
    sign_in(web, db, "admin")
    response = web.post("/api/v1/orchestrator/run")
    assert response.status_code == 200
    assert response.json()["ran"] is False


def test_the_agents_record_a_human_trigger_by_default(db):
    """The agents' `trigger` parameter defaults to a person's run, as the buttons use it."""
    import inspect

    from app.agents import intake_agent, proposal_agent

    for module in (intake_agent, proposal_agent, risk_agent):
        assert inspect.signature(module.run).parameters["trigger"].default == "human"


def test_no_floor_step_has_an_action():
    assert not orchestrator.PERSON_ONLY & orchestrator.ACTIONS.keys()
    assert {"decide_change_pm", "decide_change_sponsor"} <= orchestrator.PERSON_ONLY
    assert all(isinstance(s, Step) for s in policy_module.get_autonomy().steps.values())
