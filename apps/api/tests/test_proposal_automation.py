"""Proposal automation and the simulated client (item 6.8). Every test uses a fake Claude client:
none touches the network."""

import dataclasses
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_orchestrator import (
    _complete_plan,
    _items,
    _orchestrator_rows,
    _pass,
    _project,
    _sent,
    _user,
    _with,
)
from test_proposal_agent import FakeClaude, _output, _reply

from app import orchestrator, simulated_client
from app import policy as policy_module
from app import proposals as proposal_rules
from app.autonomy import AGENT, PERSON, WINDOW
from app.config import get_settings
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    Client,
    Project,
    ProjectPlan,
    ProjectProposal,
    ProposalEvent,
    ProposalVersion,
    User,
    utcnow,
)
from app.models.planning import ProjectAssumption
from app.models.portfolio import CANCELLED
from app.models.proposal import TEXT_SECTIONS
from app.orchestrator import next_steps
from app.schemas.portfolio import ClientOut


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
    """The settings, in `enforce` with the agents on."""
    current = get_settings()
    monkeypatch.setattr(current, "orchestrator_mode", "enforce")
    monkeypatch.setattr(current, "agent_mode", "enforce")
    return current


@pytest.fixture
def rules():
    return policy_module.get_autonomy()


DEMO = "buyer@client.example"
REAL = "buyer@client.test"


def _plan(db: Session, project: Project) -> ProjectPlan:
    return db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))


def _ready(db: Session, email: str | None = DEMO, stage: str = "planning"):
    """A client project with a complete plan, and its (new) project manager."""
    pm = _user(db, name=f"pm{db.scalar(select(func.count(User.id))) + 1}")
    project = _project(db, stage=stage, client=True, client_email=email)
    _complete_plan(db, project, pm)
    return project, pm


def _proposal(db: Session, project: Project, pm: User, *, reviewed: bool = True, **texts):
    """A proposal made from the plan (so the checker has no flags), each section written by a
    person, or an unreviewed agent draft when `reviewed` is false."""
    proposal = proposal_rules.new_proposal(db, _plan(db, project), pm.id)
    source = {"source": "person", "reviewed_by": pm.id, "reviewed_at": "2026-11-09T10:00:00"}
    if not reviewed:
        source = {"source": "agent", "reviewed_by": None, "reviewed_at": None}
    sources = {}
    for name in TEXT_SECTIONS:
        setattr(proposal, name, texts.get(name, f"The {name} written for the client"))
        if getattr(proposal, name):
            sources[name] = source
    proposal.section_sources = sources
    db.commit()
    return proposal


def _steps(db, rules, now=None):
    return [(s.step, s.subject_type, s.subject_id) for s in next_steps(db, rules, now=now)]


def _proposal_rows(db: Session, proposal_id: int) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(
        AuditDecision.agent == "proposal",
        AuditDecision.subject_type == "proposal",
        AuditDecision.subject_id == str(proposal_id),
    )
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def _fresh(db: Session, proposal_id: int) -> ProjectProposal:
    db.expire_all()
    return db.get(ProjectProposal, proposal_id)


def _no_risk_steps(db: Session, project: Project) -> None:
    """Mark the risk agent as having run, so its step doesn't crowd the pass."""
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


# --- The simulated flag


def test_a_client_is_simulated_exactly_when_its_email_is_a_demo_address(db):
    client = Client(name="Demo", contact_email=DEMO)
    db.add(client)
    db.commit()
    assert client.simulated
    client.contact_email = REAL
    db.commit()
    assert not client.simulated
    client.contact_email = " Buyer@Client.EXAMPLE "
    assert client.simulated
    client.contact_email = None
    assert not client.simulated
    assert not Client(name="No email").simulated
    # The API shows the flag and never takes it.
    assert "simulated" in ClientOut.model_fields


def test_every_seeded_client_is_simulated(db):
    from app.seed import CLIENTS, seed

    seed(db)
    db.commit()
    seeded = list(db.scalars(select(Client).where(Client.seed_key.is_not(None))))
    assert len(seeded) == len(CLIENTS) and all(c.simulated for c in seeded)


# --- Drafting


def test_drafting_waits_for_a_complete_plan(db, rules):
    pm = _user(db, name="planner")
    project = _project(db, client=True, client_email=DEMO)
    assert ("draft_proposal", "project", project.id) not in _steps(db, rules)
    db.add(ProjectPlan(project_id=project.id, scope_in="The pilot", contingency_pct=0))
    db.commit()
    assert ("draft_proposal", "project", project.id) not in _steps(db, rules)
    # Still incomplete (no milestones, deliverables or estimate): replace it with a complete one.
    db.delete(_plan(db, project))
    db.commit()
    _complete_plan(db, project, pm)
    [step] = [s for s in next_steps(db, rules) if s.step == "draft_proposal"]
    assert (step.subject_type, step.subject_id, step.mode) == ("project", project.id, AGENT)
    assert step.reason == orchestrator.NEEDS_PROPOSAL
    # A project with no client has no proposal, so nothing to draft.
    internal = _project(db)
    _complete_plan(db, internal, pm)
    assert ("draft_proposal", "project", internal.id) not in _steps(db, rules)


def test_a_real_clients_project_is_drafted_too(db, rules):
    project, _ = _ready(db, email=REAL)
    assert ("draft_proposal", "project", project.id) in _steps(db, rules)


def test_the_orchestrator_creates_and_drafts_the_proposal(db, settings, rules):
    project, _ = _ready(db)
    _no_risk_steps(db, project)
    # The plan has no scope out, so the agent writes it: every section is then written.
    claude = FakeClaude(_reply(_output(scope_out="Other warehouses")))
    report = _pass(db, settings, rules=rules, client=claude)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("draft_proposal", "done")]
    proposal = db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == project.id))
    assert proposal.status == "draft" and proposal.created_by is None
    assert len(claude.requests) == 1
    assert proposal_rules.unreviewed_sections(proposal)
    create, *_ = _proposal_rows(db, proposal.id)
    assert create.action_taken["action"] == "create" and create.action_taken["by"] == "orchestrator"
    assert (create.trigger, create.actor_user_id) == ("orchestrator", None)
    run = db.scalar(select(AuditDecision).where(AuditDecision.agent == "proposal_agent"))
    assert (run.trigger, run.actor_user_id, run.status) == ("orchestrator", None, "ok")
    [done] = [r for r in _orchestrator_rows(db) if r.action_taken.get("action") == "run"]
    assert done.action_taken["agent_audit_id"] == run.id
    # Written sections wait for a person's review: nothing more to draft or submit.
    assert _pass(db, settings, rules=rules, client=claude).outcomes == []
    assert len(claude.requests) == 1


def test_the_agent_fills_only_empty_sections(db, settings, rules):
    project, pm = _ready(db, stage="delivery")
    proposal = _proposal(db, project, pm, reviewed=False, terms="", assumptions="")
    before = {name: getattr(proposal, name) for name in TEXT_SECTIONS}
    [step] = next_steps(db, rules)
    assert (step.step, step.subject_type) == ("draft_proposal", "proposal")
    claude = FakeClaude(_reply())
    _pass(db, settings, rules=rules, client=claude)
    proposal = _fresh(db, proposal.id)
    for name in ("summary", "scope_in", "scope_out"):
        # Written but not reviewed: never drafted again.
        assert getattr(proposal, name) == before[name]
    assert proposal.terms and proposal.assumptions
    run = db.scalar(select(AuditDecision).where(AuditDecision.agent == "proposal_agent"))
    assert set(run.action_taken["replaced"]) == {"terms", "assumptions"}


def test_an_existing_draft_waits_for_a_complete_plan(db, rules):
    project, pm = _ready(db, stage="delivery")
    proposal = _proposal(db, project, pm, terms="")
    plan = _plan(db, project)
    plan.scope_in = None
    db.commit()
    assert [s for s in next_steps(db, rules) if s.subject_id == proposal.id] == []


def test_an_agent_that_leaves_a_section_empty_fails_and_goes_to_the_project_manager(
    db, settings, rules
):
    project, _ = _ready(db)
    _no_risk_steps(db, project)
    # The agent leaves every section empty (null), on every try.
    claude = FakeClaude(_reply({name: None for name in TEXT_SECTIONS}))
    start = utcnow()
    report = _pass(db, settings, rules=rules, client=claude, now=start)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("draft_proposal", "failed")]
    proposal = db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == project.id))
    # Not again before the retry is due, although the step is now the proposal's.
    _pass(db, settings, rules=rules, client=claude, now=start + timedelta(minutes=4))
    assert len(claude.requests) == 1
    for minutes in (5, 10, 15, 20, 30, 60):
        _pass(db, settings, rules=rules, client=claude, now=start + timedelta(minutes=minutes))
    # The orchestrator's usual rule: the first try and its retries, then a person.
    assert len(claude.requests) == 1 + orchestrator.MAX_RETRIES
    [item] = _items(db, "open")
    assert (item.step, item.subject_type, item.subject_id) == (
        "draft_proposal",
        "proposal",
        str(proposal.id),
    )
    assert item.who == "project_manager"
    assert item.reason.endswith("the proposal_agent failed after 3 retries")
    errors = [r for r in _orchestrator_rows(db) if r.status == "error"]
    assert len(errors) == 1 + orchestrator.MAX_RETRIES
    assert errors[-1].error.startswith("the proposal agent left these sections empty: ")
    assert _fresh(db, proposal.id).status == "draft"


def _assume(db: Session, project: Project, owner: User, text: str) -> None:
    db.add(
        ProjectAssumption(
            project_id=project.id,
            text=text,
            owner_user_id=owner.id,
            confirm_by=date(2027, 1, 1),
        )
    )
    db.commit()


def test_a_project_with_no_assumptions_gets_the_none_recorded_sentence(db, settings, rules):
    project, _ = _ready(db)
    _no_risk_steps(db, project)
    # No assumptions and no scope out: the agent leaves both null, as the real model did.
    claude = FakeClaude(_reply(_output(scope_in="The pilot", assumptions=None)))
    report = _pass(db, settings, rules=rules, client=claude)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("draft_proposal", "done")]
    proposal = db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == project.id))
    assert proposal.assumptions == "No assumptions were recorded for this project."
    assert proposal.scope_out == "Nothing was recorded as out of scope for this project."
    # An agent draft like any other: a person reviews it before submit.
    assert proposal.section_sources["assumptions"]["source"] == "agent"
    assert proposal.section_sources["assumptions"]["reviewed_by"] is None
    assert "assumptions" in proposal_rules.unreviewed_sections(proposal)
    run = db.scalar(select(AuditDecision).where(AuditDecision.agent == "proposal_agent"))
    assert set(run.output["nothing_recorded"]) == {"assumptions", "scope_out"}
    assert "assumptions" in run.action_taken["replaced"]


def test_a_project_with_assumptions_whose_draft_comes_back_empty_still_fails(db, settings, rules):
    project, pm = _ready(db)
    _no_risk_steps(db, project)
    _assume(db, project, pm, "The client's data is available")
    claude = FakeClaude(_reply(_output(scope_in="The pilot", assumptions=None)))
    report = _pass(db, settings, rules=rules, client=claude)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("draft_proposal", "failed")]
    proposal = db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == project.id))
    assert not proposal.assumptions
    errors = [r for r in _orchestrator_rows(db) if r.status == "error"]
    assert errors[-1].error == orchestrator.LEFT_EMPTY.format(sections="assumptions")


# --- Submitting


def test_submitting_waits_for_every_section_to_be_reviewed(db, rules):
    project, pm = _ready(db, stage="delivery")
    proposal = _proposal(db, project, pm, reviewed=False)
    assert next_steps(db, rules) == []
    proposal.section_sources = {
        name: {"source": "agent", "reviewed_by": pm.id, "reviewed_at": "2026-11-09T10:00:00"}
        for name in TEXT_SECTIONS
    }
    db.commit()
    [step] = next_steps(db, rules)
    assert (step.step, step.mode, step.subject_id) == ("submit_proposal", WINDOW, proposal.id)


def test_open_flags_send_submitting_to_the_project_manager(db, settings, rules):
    project, pm = _ready(db, stage="delivery")
    proposal = _proposal(db, project, pm, summary="We charge $99,000.00 for this.")
    [step] = next_steps(db, rules)
    assert (step.step, step.mode, step.waits_on) == ("submit_proposal", PERSON, "project_manager")
    assert step.handoff == orchestrator.FLAGS_OPEN.format(n=1)
    _pass(db, settings, rules=rules)
    [item] = _items(db, "open")
    assert (item.step, item.who) == ("submit_proposal", "project_manager")
    # Once a person fixes the text, the item gives way and the window opens.
    proposal = _fresh(db, proposal.id)
    proposal.summary = "We build the model."
    db.commit()
    report = _pass(db, settings, rules=rules)
    assert [o.outcome for o in report.outcomes] == ["cancelled", "pending"]
    assert _fresh(db, proposal.id).status == "draft"


def test_the_window_submits_once_with_no_person(db, settings, rules):
    project, pm = _ready(db, stage="delivery")
    proposal = _proposal(db, project, pm)
    start = utcnow()
    _pass(db, settings, now=start, rules=rules)
    assert _fresh(db, proposal.id).status == "draft"
    report = _pass(db, settings, now=start + timedelta(minutes=16), rules=rules)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("submit_proposal", "done")]
    proposal = _fresh(db, proposal.id)
    assert proposal.status == "submitted"
    copy = db.scalar(select(ProposalVersion).where(ProposalVersion.proposal_id == proposal.id))
    assert copy.submitted_by is None
    [row] = [r for r in _proposal_rows(db, proposal.id) if r.action_taken["action"] == "submit"]
    assert (row.trigger, row.actor_user_id) == ("orchestrator", None)
    assert row.action_taken["window_minutes"] == 15
    # Approving is the sponsor's: the next pass opens an inbox item and approves nothing.
    _pass(db, settings, now=start + timedelta(minutes=20), rules=rules)
    assert [(i.step, i.who) for i in _items(db, "open")] == [("approve_proposal", "sponsor")]
    assert _fresh(db, proposal.id).status == "submitted"


def test_the_window_checks_again_before_submitting(db, settings, rules):
    project, pm = _ready(db, stage="delivery")
    proposal = _proposal(db, project, pm)
    start = utcnow()
    _pass(db, settings, now=start, rules=rules)
    # The plan changes while the window waits: the checker now has a flag. The action runs as
    # planned (as if the pass had found the step before the change) and must not submit.
    ctx = orchestrator.Context(db, settings, FakeClaude(), rules, "", "proposal", start)
    proposal = _fresh(db, proposal.id)
    proposal.terms = "Paid $5,000.00 up front."
    db.commit()
    row_id = orchestrator._submit_proposal(ctx, proposal.id)
    db.commit()
    row = db.get(AuditDecision, row_id)
    assert row.status == "skipped" and row.action_taken["refusal"] == "flags_open"
    assert _fresh(db, proposal.id).status == "draft"


def test_a_hold_sends_submitting_to_the_project_managers_inbox(
    db,
    web,
    settings,
    rules,
    sign_in,
):
    project, pm = _ready(db, stage="delivery")
    proposal = _proposal(db, project, pm)
    start = utcnow()
    _pass(db, settings, now=start, rules=rules)
    [action] = orchestrator_actions(db)
    sign_in(web, db, "project_manager", email="other.pm@pmo.example")
    reply = web.post(
        f"/api/v1/orchestrator/actions/{action.id}/hold",
        json={"reason": "I want to read it once more"},
    )
    assert reply.status_code == 200, reply.text
    _pass(db, settings, now=start + timedelta(minutes=30), rules=rules)
    assert _fresh(db, proposal.id).status == "draft"
    [item] = _items(db, "open")
    assert (item.step, item.who) == ("submit_proposal", "project_manager")


def orchestrator_actions(db: Session):
    from app.models import OrchestratorAction

    db.expire_all()
    return list(db.scalars(select(OrchestratorAction).order_by(OrchestratorAction.id)))


# --- Approving and sending stay with people


def test_no_automated_path_approves_or_marks_a_proposal_sent(db, settings, rules):
    project, pm = _ready(db, stage="delivery")
    proposal = _proposal(db, project, pm)
    proposal.status = "submitted"
    other, _ = _ready(db, stage="delivery")
    approved = _proposal(db, other, pm)
    approved.status = "approved"
    db.commit()
    # Even a policy that gives every proposal step to an agent leaves these with people.
    greedy = _with(
        rules,
        approve_proposal={"mode": AGENT, "agent": "proposal_agent"},
        send_proposal={"mode": AGENT, "agent": "proposal_agent"},
    )
    for policy in (rules, greedy):
        _pass(db, settings, rules=policy, now=utcnow() + timedelta(days=1))
    assert _fresh(db, proposal.id).status == "submitted"
    assert _fresh(db, approved.id).status == "approved"
    assert {(i.step, i.who) for i in _items(db, "open")} == {
        ("approve_proposal", "sponsor"),
        ("send_proposal", "project_manager"),
    }
    events = db.scalars(select(ProposalEvent.kind)).all()
    assert "approve" not in events and "sent" not in events


# --- The simulated client


def _sent_proposal(db: Session, email: str | None = DEMO, minutes_ago: int = 11):
    project, pm = _ready(db, email=email, stage="delivery")
    proposal = _proposal(db, project, pm)
    proposal.status = "sent"
    db.commit()
    _sent(db, proposal.id, pm, utcnow() - timedelta(minutes=minutes_ago))
    return project, proposal


def test_a_simulated_client_answers_only_after_the_delay(db, settings, rules):
    _, proposal = _sent_proposal(db, minutes_ago=5)
    assert next_steps(db, rules) == []
    assert _pass(db, settings, rules=rules).outcomes == []
    later = utcnow() + timedelta(minutes=6)
    [step] = next_steps(db, rules, now=later)
    assert (step.step, step.mode, step.waits_on) == ("client_answer", AGENT, "simulated_client")
    report = _pass(db, settings, rules=rules, now=later)
    assert [(o.step, o.outcome) for o in report.outcomes] == [("client_answer", "done")]
    assert _fresh(db, proposal.id).status == "accepted"
    event = db.scalar(select(ProposalEvent).where(ProposalEvent.kind == "answer"))
    assert (event.source, event.actor_user_id, event.answer) == ("simulated", None, "accepted")
    assert event.note.startswith("Simulated client")
    [row] = [r for r in _proposal_rows(db, proposal.id) if r.action_taken["action"] == "answer"]
    assert (row.trigger, row.actor_user_id) == ("orchestrator", None)
    assert row.action_taken["source"] == "simulated"
    assert row.action_taken["by"] == "simulated_client"
    # It answers at most once.
    assert _pass(db, settings, rules=rules, now=later + timedelta(hours=1)).outcomes == []
    answers = db.scalars(select(ProposalEvent).where(ProposalEvent.kind == "answer")).all()
    assert len(answers) == 1


def test_the_delay_comes_from_the_settings(db, settings, rules, monkeypatch):
    monkeypatch.setattr(settings, "simulated_client_delay_minutes", 60)
    _sent_proposal(db, minutes_ago=30)
    assert next_steps(db, rules) == []
    assert len(next_steps(db, rules, now=utcnow() + timedelta(minutes=31))) == 1


def test_the_policy_can_make_the_simulated_client_decline(db, settings, rules):
    _, proposal = _sent_proposal(db)
    declining = dataclasses.replace(rules, simulated_answer="declined")
    _pass(db, settings, rules=declining)
    assert _fresh(db, proposal.id).status == "declined"


def test_a_real_client_is_never_answered_by_the_simulation(db, settings, rules):
    _, proposal = _sent_proposal(db, email=REAL, minutes_ago=60 * 24)
    [step] = next_steps(db, rules)
    assert (step.step, step.mode, step.waits_on) == ("client_answer", PERSON, "project_manager")
    assert step.handoff == orchestrator.REAL_CLIENT
    _pass(db, settings, rules=rules)
    assert _fresh(db, proposal.id).status == "sent"
    [item] = _items(db, "open")
    assert (item.step, item.who) == ("client_answer", "project_manager")
    with pytest.raises(simulated_client.Skipped, match="not simulated"):
        simulated_client.answer_now(
            db, proposal.id, answer="accepted", delay_minutes=0, now=utcnow()
        )


def test_a_client_whose_email_becomes_real_stops_being_simulated(db, settings, rules):
    project, proposal = _sent_proposal(db)
    client = db.get(Client, project.client_id)
    client.contact_email = REAL
    db.commit()
    assert not client.simulated
    _pass(db, settings, rules=rules)
    assert _fresh(db, proposal.id).status == "sent"
    assert db.scalar(select(ProposalEvent).where(ProposalEvent.kind == "answer")) is None


def test_the_answer_checks_again_when_it_is_due(db, settings, rules):
    project, proposal = _sent_proposal(db)
    ctx = orchestrator.Context(db, settings, FakeClaude(), rules, "", "proposal", utcnow())
    # The client's email changed after the step was found.
    db.get(Client, project.client_id).contact_email = REAL
    db.commit()
    row = db.get(AuditDecision, orchestrator._client_answer(ctx, proposal.id))
    assert row.status == "skipped"
    assert row.action_taken["reason"] == simulated_client.NOT_SIMULATED
    db.get(Client, project.client_id).contact_email = DEMO
    proposal = _fresh(db, proposal.id)
    proposal.status = "withdrawn"
    db.commit()
    row = db.get(AuditDecision, orchestrator._client_answer(ctx, proposal.id))
    assert row.action_taken["reason"] == simulated_client.NOT_SENT
    assert _fresh(db, proposal.id).status == "withdrawn"


def test_a_cancelled_project_is_not_answered_by_the_simulation(db, settings, rules):
    project, proposal = _sent_proposal(db)
    ctx = orchestrator.Context(db, settings, FakeClaude(), rules, "", "proposal", utcnow())
    # The project was cancelled after the step was found.
    project.status = CANCELLED
    db.commit()
    row = db.get(AuditDecision, orchestrator._client_answer(ctx, proposal.id))
    assert row.status == "skipped"
    assert row.action_taken["reason"] == simulated_client.NOT_LIVE
    assert _fresh(db, proposal.id).status == "sent"
    assert db.scalar(select(ProposalEvent).where(ProposalEvent.kind == "answer")) is None
    with pytest.raises(simulated_client.Skipped, match="cancelled"):
        simulated_client.answer_now(
            db, proposal.id, answer="accepted", delay_minutes=0, now=utcnow()
        )


def test_only_a_simulated_answer_may_have_no_person(db):
    _, proposal = _sent_proposal(db)
    with pytest.raises(ValueError, match="simulated client"):
        proposal_rules.answer(
            db,
            proposal,
            answer="accepted",
            answered_on=utcnow().date(),
            note=None,
            actor_user_id=None,
            source="person",
            trigger="human",
        )
    db.rollback()
    # The database refuses it too.
    db.add(ProposalEvent(proposal_id=proposal.id, kind="withdraw", actor_user_id=None))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_persons_answer_records_the_person(db, web, sign_in):
    project, proposal = _sent_proposal(db, email=REAL)
    user = sign_in(web, db, "project_manager", email="answering.pm@pmo.example")
    reply = web.post(
        f"/api/v1/projects/{project.id}/proposal/answer",
        json={"version": proposal.version, "answer": "declined", "answered_on": "2026-11-09"},
    )
    assert reply.status_code == 200, reply.text
    event = db.scalar(select(ProposalEvent).where(ProposalEvent.kind == "answer"))
    assert (event.source, event.actor_user_id) == ("person", user.id)
    every = db.scalars(select(ProposalEvent)).all()
    assert all(e.actor_user_id is not None and e.source == "person" for e in every)


def test_the_proposal_page_says_simulated_client(db, web, settings, rules, sign_in):
    project, proposal = _sent_proposal(db)
    _pass(db, settings, rules=rules)
    sign_in(web, db, "auditor")
    page = web.get(f"/api/v1/projects/{project.id}/proposal").json()
    assert page["client_simulated"] is True and page["status"] == "accepted"
    versions = web.get(f"/api/v1/projects/{project.id}/proposal/versions").json()
    # The proposal was put in `sent` by hand here, so its events have no version: check the rows.
    assert versions["items"] == []
    events = db.scalars(select(ProposalEvent).order_by(ProposalEvent.id)).all()
    assert [(e.kind, e.source) for e in events] == [("sent", "person"), ("answer", "simulated")]


# --- Shadow mode


def test_shadow_saves_nothing_for_all_three_steps(db, settings, rules, monkeypatch):
    monkeypatch.setattr(settings, "orchestrator_mode", "shadow")
    new, _ = _ready(db)
    _no_risk_steps(db, new)
    to_submit, pm = _ready(db, stage="delivery")
    submit = _proposal(db, to_submit, pm)
    _, answer = _sent_proposal(db)
    claude = FakeClaude(_reply())
    report = _pass(db, settings, rules=rules, client=claude, now=utcnow() + timedelta(days=1))
    assert {o.step for o in report.outcomes} == {
        "draft_proposal",
        "submit_proposal",
        "client_answer",
    }
    assert claude.requests == []
    assert db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == new.id)) is None
    assert _fresh(db, submit.id).status == "draft"
    assert _fresh(db, answer.id).status == "sent"
    assert db.scalar(select(ProposalVersion)) is None
    assert db.scalar(select(ProposalEvent).where(ProposalEvent.kind == "answer")) is None
    assert {r.status for r in _orchestrator_rows(db)} == {"skipped"}
