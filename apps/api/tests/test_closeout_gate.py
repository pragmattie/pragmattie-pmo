"""The close-out gate (item 7.7): each code check refuses by name, a pass sets the project to
closed and freezes its closure report in the same transaction, accepted risks don't block it,
every refusal is audited, and the orchestrator drafts the closure report once and asks the
sponsor for the gate."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_closure import CLOSURE, LESSONS, REPORT, TODAY, closing_project, create, refusals
from test_closure_agent import FakeClaude

from app import delivery, orchestrator
from app import policy as policy_module
from app.api.gates import CLOSE_OUT_UNMET, NOT_IN_CLOSURE
from app.config import get_settings
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    ClosureReport,
    GateDecision,
    Lesson,
    Project,
    Risk,
    User,
    ValueCaseBenefit,
)

REASON = "The record is complete and the client has signed off"
TICKS = [{"id": "record_complete", "met": True, "note": "Read it end to end"}]
CHECK_IDS = [
    "closure_report_reviewed",
    "benefits_measured_or_owned",
    "lesson_recorded",
    "no_open_risks",
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
    monkeypatch.setattr(delivery, "today", lambda: TODAY)


@pytest.fixture
def pm(db, gates_policy, sign_in) -> tuple[TestClient, User]:
    web = TestClient(app, base_url="http://localhost")
    return web, sign_in(web, db, "project_manager")


@pytest.fixture
def sponsor(db, sign_in, pm) -> tuple[TestClient, User]:
    web = TestClient(app, base_url="http://localhost")
    return web, sign_in(web, db, "sponsor")


def _url(pid: int) -> str:
    return f"/api/v1/projects/{pid}/close-out-gate"


def _decide(web: TestClient, pid: int, outcome: str = "pass"):
    version = web.get(_url(pid)).json()["project_version"]
    body = {"version": version, "outcome": outcome, "reason": REASON, "criteria": TICKS}
    return web.post(_url(pid), json=body)


def _risk(db: Session, pid: int, status: str, title: str = "Data feed fails") -> Risk:
    risk = Risk(
        project_id=pid,
        kind="threat",
        title=title,
        cause="c",
        effect="e",
        category="project",
        objectives=["schedule"],
        response="reduce",
        response_plan="p",
        status=status,
        source="person",
    )
    db.add(risk)
    db.commit()
    return risk


def _complete(db: Session, web: TestClient, owner: User) -> int:
    """A project in closure whose every close-out check is met."""
    pid = closing_project(db, owner)
    assert create(web, pid).status_code == 201
    bid = db.scalar(select(ValueCaseBenefit.id))
    measured = web.put(
        REPORT.format(pid=pid) + f"/benefits/{bid}",
        json={"version": 0, "measure_owner_id": owner.id, "measure_by": "2027-09-01"},
    )
    assert measured.status_code == 200, measured.text
    lesson = {
        "category": "scope",
        "what_happened": "The regions weren't agreed at the start.",
        "do_differently": "Agree them before the go-ahead.",
    }
    assert web.post(LESSONS.format(pid=pid), json=lesson).status_code == 201
    return pid


def _unmet(response) -> dict[str, str]:
    return {c["id"]: c["detail"] for c in response.json()["detail"]["unmet"]}


def test_the_gate_shows_its_criteria(db, pm):
    web, user = pm
    pid = _complete(db, web, user)
    body = web.get(_url(pid)).json()
    assert body["gate"] | {"deciders": None} == {
        "id": "close_out",
        "name": "Close-out gate",
        "from_stage": "closure",
        "to_stage": "closed",
        "applies_to": "project",
        "deciders": None,
    }
    assert [c["id"] for c in body["criteria"] if c["kind"] == "check"] == CHECK_IDS
    assert body["checks_met"] is True


def test_each_check_refuses_by_name_until_met(db, pm, sponsor):
    web, user = pm
    sponsor_web, _ = sponsor
    pid = closing_project(db, user)
    risk = _risk(db, pid, "open")
    refused = _decide(sponsor_web, pid)
    assert refused.status_code == 409
    assert refused.json()["detail"]["message"] == CLOSE_OUT_UNMET
    unmet = _unmet(refused)
    assert set(unmet) == set(CHECK_IDS)
    assert unmet["closure_report_reviewed"] == "this project has no closure report yet"
    assert "Days to plan" in unmet["benefits_measured_or_owned"]
    assert unmet["no_open_risks"] == f"still open: risk {risk.id} 'Data feed fails' (open)"

    # An unreviewed agent section blocks, naming it.
    create(web, pid)
    report = db.scalar(select(ClosureReport).where(ClosureReport.project_id == pid))
    report.sections = {**report.sections, "remaining": "Agent text."}
    report.section_sources = {
        "remaining": {"source": "agent", "reviewed_by": None, "reviewed_at": None}
    }
    db.commit()
    assert _unmet(_decide(sponsor_web, pid))["closure_report_reviewed"] == (
        "agent draft not reviewed by a person: remaining"
    )
    # An agent's suggested lesson doesn't count until a person records it.
    db.add(
        Lesson(
            project_id=pid,
            category="ai",
            what_happened="w",
            do_again="d",
            source="agent",
            status="suggested",
        )
    )
    db.commit()
    assert "lesson_recorded" in _unmet(_decide(sponsor_web, pid))
    assert set(refusals(db, "gate")) == {"unmet_criteria"}
    assert db.get(Project, pid).stage == "closure"


def test_an_accepted_risk_does_not_block_but_a_proposed_one_does(db, pm, sponsor):
    web, user = pm
    pid = _complete(db, web, user)
    _risk(db, pid, "accepted", "Key analyst leaves")
    _risk(db, pid, "closed", "Licence lapses")
    proposed = _risk(db, pid, "proposed", "Model drifts")
    unmet = _unmet(_decide(sponsor[0], pid))
    assert unmet == {"no_open_risks": f"still open: risk {proposed.id} 'Model drifts' (proposed)"}


def test_passing_closes_the_project_and_freezes_the_report(db, pm, sponsor):
    web, user = pm
    sponsor_web, sponsor_user = sponsor
    pid = _complete(db, web, user)
    passed = _decide(sponsor_web, pid)
    assert passed.status_code == 200, passed.text
    assert passed.json()["project_stage"] == "closed"
    db.expire_all()
    assert db.get(Project, pid).stage == "closed"
    report = db.scalar(select(ClosureReport).where(ClosureReport.project_id == pid))
    assert report.frozen_at is not None
    decision = db.scalar(select(GateDecision).where(GateDecision.gate_id == "close_out"))
    assert decision.outcome == "pass" and decision.decided_by == sponsor_user.id
    rows = db.scalars(select(AuditDecision).where(AuditDecision.agent == "closure")).all()
    frozen = next((r for r in rows if r.action_taken["action"] == "frozen"), None)
    assert frozen is not None and frozen.action_taken["decision_id"] == decision.id
    gate_row = db.scalar(
        select(AuditDecision).where(AuditDecision.agent == "gate", AuditDecision.status == "ok")
    )
    assert gate_row.action_taken["closure_report_id"] == report.id
    # The closed project shows the frozen report, and nothing on it changes again.
    shown = web.get(CLOSURE.format(pid=pid)).json()
    assert shown["report"]["frozen_at"] is not None
    edit = web.patch(
        REPORT.format(pid=pid),
        json={"version": report.version, "sections": {"remaining": "Late edit"}},
    )
    assert edit.status_code == 409
    assert _decide(sponsor_web, pid).status_code == 409


def test_only_a_project_in_closure_is_decided_and_never_by_its_manager(db, pm, sponsor, sign_in):
    web, user = pm
    pid = closing_project(db, user, stage="delivery")
    refused = _decide(sponsor[0], pid)
    assert refused.status_code == 409 and refused.json()["detail"] == NOT_IN_CLOSURE
    db.execute(update(Project).where(Project.id == pid).values(stage="closure"))
    db.commit()
    assert _decide(web, pid).status_code == 403  # a project manager can't decide a gate
    assert refusals(db, "gate") == ["not_in_closure", "no_permission"]


def test_the_orchestrator_drafts_the_closure_report_once_and_asks_for_the_gate(db, pm, monkeypatch):
    web, user = pm
    pid = closing_project(db, user)
    policy = policy_module.get_autonomy()
    steps = [s for s in orchestrator.next_steps(db, policy) if s.subject_id == pid]
    assert [s.step for s in steps] == ["draft_closure_report"]
    assert steps[0].mode == "agent"

    monkeypatch.setattr(get_settings(), "agent_mode", "enforce")
    monkeypatch.setattr(get_settings(), "orchestrator_mode", "enforce")
    fake = FakeClaude()
    report = orchestrator.run_pass(db, get_settings(), fake)
    assert len(fake.requests) == 1, report
    db.expire_all()
    made = db.scalar(select(ClosureReport).where(ClosureReport.project_id == pid))
    assert made is not None and made.created_by is None
    assert all(made.section_sources[n]["source"] == "agent" for n in made.sections)
    # Once drafted it isn't drafted again, and the gate isn't asked for while sections wait.
    steps = [s for s in orchestrator.next_steps(db, policy) if s.subject_id == pid]
    assert [s.step for s in steps] == []

    # A person reviews the sections, measures the benefit and records a lesson: the sponsor is
    # asked for the close-out gate.
    for name in ("delivered", "value_case", "remaining"):
        db.expire_all()
        version = db.get(ClosureReport, made.id).version
        url = REPORT.format(pid=pid) + f"/sections/{name}/confirm"
        assert web.post(url, json={"version": version}).status_code == 200
    bid = db.scalar(select(ValueCaseBenefit.id))
    web.put(
        REPORT.format(pid=pid) + f"/benefits/{bid}",
        json={"version": 0, "value_now": "three days", "value_now_on": "2027-05-30"},
    )
    db.execute(update(Lesson).where(Lesson.project_id == pid).values(status="recorded"))
    db.commit()
    db.expire_all()
    steps = [s for s in orchestrator.next_steps(db, policy) if s.subject_id == pid]
    assert [(s.step, s.gate, s.waits_on) for s in steps] == [("pass_gate", "close_out", "sponsor")]


def test_no_close_out_steps_for_a_closed_project(db, pm):
    web, user = pm
    pid = closing_project(db, user, stage="closed")
    policy = policy_module.get_autonomy()
    assert [s for s in orchestrator.next_steps(db, policy) if s.subject_id == pid] == []
    assert orchestrator.subject_steps(db, policy, "project", pid) == []


def test_closure_steps_are_the_same_through_subject_steps(db, pm):
    web, user = pm
    pid = closing_project(db, user)
    policy = policy_module.get_autonomy()
    every = [s for s in orchestrator.next_steps(db, policy) if s.subject_id == pid]
    assert every and orchestrator.subject_steps(db, policy, "project", pid) == every
