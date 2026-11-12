"""Delivery progress (item 7.2): days late against the baseline target in working days, deliverable
status without `accepted`, append-only spend with guarded corrections, the plan and baseline left
alone, and every write and refused attempt audited."""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app import delivery as rules
from app.baselines import freeze
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    GateDecision,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectBaseline,
    ProjectPlan,
    ProjectSpend,
    User,
)
from app.models.audit import AppendOnlyError

# The baseline target: a Monday.
TARGET = date(2027, 3, 1)
TODAY = date(2027, 6, 1)


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
def pm(db, sign_in) -> tuple[TestClient, User]:
    web = TestClient(app, base_url="http://localhost")
    return web, sign_in(web, db, "project_manager")


def _project(db: Session, owner: User, *, stage: str = "delivery", status: str = "on_track"):
    """A project managed by `owner` with one milestone on `TARGET`, one deliverable and one line
    of 1000.00, and baseline 1 frozen from that plan. Returns the project, milestone and
    deliverable ids."""
    project = Project(
        name="Forecast",
        stage=stage,
        sponsor="S",
        pm="P",
        pm_user_id=owner.id,
        status=status,
    )
    db.add(project)
    db.flush()
    plan = ProjectPlan(project_id=project.id, scope_in="The pilot", contingency_pct=0)
    db.add(plan)
    db.flush()
    milestone = PlanMilestone(plan_id=plan.id, name="Pilot live", target_date=TARGET)
    db.add(milestone)
    db.flush()
    deliverable = PlanDeliverable(plan_id=plan.id, name="Model", milestone_id=milestone.id)
    db.add_all(
        [
            deliverable,
            PlanEstimateLine(
                plan_id=plan.id, description="Analysts", category="people", amount="1000.00"
            ),
        ]
    )
    decision = GateDecision(
        gate_id="go_ahead",
        subject_type="project",
        subject_id=project.id,
        outcome="pass",
        decided_by=owner.id,
        reason="The plan is complete and inside the budget",
        criteria=[],
        policy_version="test",
    )
    db.add(decision)
    db.flush()
    freeze(db, plan, decision.id)
    db.commit()
    return project.id, milestone.id, deliverable.id


@pytest.fixture
def ids(db, pm):
    return _project(db, pm[1])


def _url(pid: int, suffix: str = "") -> str:
    return f"/api/v1/projects/{pid}/delivery{suffix}"


def _reach(web: TestClient, pid: int, mid: int, on: date | None, version: int = 1, **extra):
    body = {"version": version, "reached_on": on.isoformat() if on else None, **extra}
    return web.put(_url(pid, f"/milestones/{mid}"), json=body)


def _status(web: TestClient, pid: int, did: int, status: str, version: int = 1):
    return web.put(_url(pid, f"/deliverables/{did}"), json={"version": version, "status": status})


def _spend(web: TestClient, pid: int, amount: str, supersedes_id: int | None = None):
    body = {"amount": amount, "as_of": "2027-05-31", "note": "Finance report, May"}
    if supersedes_id is not None:
        body["supersedes_id"] = supersedes_id
    return web.post(_url(pid, "/spend"), json=body)


def _rows(db: Session, **filters) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(AuditDecision.agent == "delivery")
    for name, value in filters.items():
        stmt = stmt.where(getattr(AuditDecision, name) == value)
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def _refusals(db: Session) -> list[str]:
    return [r.action_taken["refusal"] for r in _rows(db, status="rejected")]


def _plan_state(db: Session, pid: int) -> dict:
    db.expire_all()
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == pid))
    milestones = list(db.scalars(select(PlanMilestone).where(PlanMilestone.plan_id == plan.id)))
    deliverables = list(
        db.scalars(select(PlanDeliverable).where(PlanDeliverable.plan_id == plan.id))
    )
    baselines = list(db.scalars(select(ProjectBaseline).where(ProjectBaseline.project_id == pid)))
    project = db.get(Project, pid)
    return {
        "project_version": project.version,
        "plan_version": plan.version,
        "milestones": [(m.target_date, m.version, m.updated_at) for m in milestones],
        "deliverables": [(d.name, d.version, d.updated_at) for d in deliverables],
        "baselines": [(b.number, b.plan, b.total) for b in baselines],
    }


# --- Days late


def test_working_days_late_early_and_on_time():
    assert rules.days_late(TARGET, date(2027, 2, 26), TODAY) == -1  # the Friday before: early
    assert rules.days_late(TARGET, TARGET, TODAY) == 0  # on time
    assert rules.days_late(TARGET, date(2027, 3, 8), TODAY) == 5  # a week later: 5 working days
    # Not reached: counts to today, and never early before it is reached.
    assert rules.days_late(TARGET, None, date(2027, 3, 3)) == 2
    assert rules.days_late(TARGET, None, date(2027, 2, 1)) == 0
    assert rules.days_late(None, date(2027, 3, 3), TODAY) is None


@pytest.mark.parametrize(
    ("reached", "late"),
    [(date(2027, 2, 26), -1), (TARGET, 0), (date(2027, 3, 3), 2), (date(2027, 3, 6), 4)],
    ids=["early", "on_time", "late", "late_on_a_saturday"],
)
def test_marking_a_milestone_reached_measures_days_late(db, pm, ids, reached, late):
    pid, mid, _ = ids
    response = _reach(pm[0], pid, mid, reached, reached_note="Signed off by the client")
    assert response.status_code == 200, response.text
    milestone = response.json()["milestones"][0]
    assert milestone == {
        "id": mid,
        "name": "Pilot live",
        "target_date": TARGET.isoformat(),
        "plan_date": TARGET.isoformat(),
        "reached_on": reached.isoformat(),
        "reached_note": "Signed off by the client",
        "days_late": late,
        "version": 2,
    }


def test_days_late_use_the_baseline_target_after_the_plan_date_moved(db, pm, ids):
    pid, mid, _ = ids
    db.execute(
        update(PlanMilestone.__table__)
        .where(PlanMilestone.__table__.c.id == mid)
        .values(target_date=date(2027, 3, 15))
    )
    db.commit()
    milestone = _reach(pm[0], pid, mid, date(2027, 3, 8)).json()["milestones"][0]
    assert milestone["plan_date"] == "2027-03-15"
    assert milestone["target_date"] == TARGET.isoformat()
    assert milestone["days_late"] == 5  # late against the baseline, though early on the plan


def test_an_unreached_milestone_counts_to_today(db, pm, ids):
    pid, _, _ = ids
    body = pm[0].get(_url(pid)).json()
    assert body["today"] == TODAY.isoformat()
    # 1 March to 1 June 2027: 13 weeks and a day, so 66 working days.
    assert body["milestones"][0]["days_late"] == 66
    assert body["milestones"][0]["reached_on"] is None


def test_a_milestone_without_a_baseline_target_has_no_days_late(db, pm, ids):
    pid, _, _ = ids
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == pid))
    db.add(PlanMilestone(plan_id=plan.id, name="Handover", target_date=date(2027, 4, 1)))
    db.commit()
    later = pm[0].get(_url(pid)).json()["milestones"][1]
    assert later["name"] == "Handover"
    assert later["target_date"] is None and later["days_late"] is None


def test_clearing_a_reached_milestone(db, pm, ids):
    pid, mid, _ = ids
    _reach(pm[0], pid, mid, TARGET, reached_note="Done")
    response = _reach(pm[0], pid, mid, None, version=2)
    assert response.status_code == 200, response.text
    milestone = response.json()["milestones"][0]
    assert milestone["reached_on"] is None and milestone["reached_note"] is None
    assert milestone["version"] == 3
    actions = [r.action_taken["action"] for r in _rows(db, status="ok")]
    assert actions == ["milestone_reached", "milestone_reached_cleared"]


def test_a_note_without_a_date_is_refused(db, pm, ids):
    pid, mid, _ = ids
    assert _reach(pm[0], pid, mid, None, reached_note="Done").status_code == 422


def test_a_reached_date_after_today_is_refused_and_audited(db, pm, ids):
    pid, mid, _ = ids
    assert _reach(pm[0], pid, mid, date(2027, 6, 2)).status_code == 422
    assert _refusals(db) == ["future_date"]


# --- Deliverable status


@pytest.mark.parametrize("status", ["in_progress", "ready_for_acceptance", "not_started"])
def test_the_project_manager_moves_a_deliverable_between_the_first_three(db, pm, ids, status):
    pid, _, did = ids
    response = _status(pm[0], pid, did, status)
    assert response.status_code == 200, response.text
    assert response.json()["deliverables"] == [
        {"id": did, "name": "Model", "milestone_id": ids[1], "status": status, "version": 2}
    ]


def test_no_route_sets_a_deliverable_to_accepted(db, pm, ids):
    pid, _, did = ids
    response = _status(pm[0], pid, did, "accepted")
    assert response.status_code == 422
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "not_started"
    assert _refusals(db) == ["accepted_not_allowed"]
    # No other route here takes a status at all.
    body = {"version": 1, "reached_on": None, "status": "accepted"}
    assert pm[0].put(_url(pid, f"/milestones/{ids[1]}"), json=body).status_code == 422
    spend = {"amount": "1.00", "as_of": "2027-05-31", "note": "x", "status": "accepted"}
    assert pm[0].post(_url(pid, "/spend"), json=spend).status_code == 422
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "not_started"


def test_an_accepted_deliverable_is_not_moved_back(db, pm, ids):
    pid, _, did = ids
    db.execute(
        update(PlanDeliverable.__table__)
        .where(PlanDeliverable.__table__.c.id == did)
        .values(status="accepted")
    )
    db.commit()
    assert _status(pm[0], pid, did, "in_progress").status_code == 409
    assert _refusals(db) == ["already_accepted"]


# --- Rules: stage, cancelled, stale


@pytest.mark.parametrize(
    ("stage", "status", "refusal"),
    [
        ("planning", "on_track", "not_in_delivery"),
        ("closure", "on_track", "not_in_delivery"),
        ("delivery", "cancelled", "cancelled"),
    ],
)
def test_every_write_needs_a_project_in_delivery_not_cancelled(db, pm, stage, status, refusal):
    pid, mid, did = _project(db, pm[1], stage=stage, status=status)
    web = pm[0]
    assert _reach(web, pid, mid, TARGET).status_code == 409
    assert _status(web, pid, did, "in_progress").status_code == 409
    assert _spend(web, pid, "10.00").status_code == 409
    assert _refusals(db) == [refusal] * 3
    assert all(r.subject_type == "project" for r in _rows(db))


def test_a_stale_version_is_409_and_audited(db, pm, ids):
    pid, mid, did = ids
    assert _reach(pm[0], pid, mid, TARGET).status_code == 200
    assert _reach(pm[0], pid, mid, None, version=1).status_code == 409
    assert _status(pm[0], pid, did, "in_progress").status_code == 200
    assert _status(pm[0], pid, did, "ready_for_acceptance", version=1).status_code == 409
    assert _refusals(db) == ["stale_version", "stale_version"]


def test_an_item_of_another_project_is_404(db, pm, ids):
    pid, _, _ = ids
    _, other_mid, other_did = _project(db, pm[1])
    assert _reach(pm[0], pid, other_mid, TARGET).status_code == 404
    assert _status(pm[0], pid, other_did, "in_progress").status_code == 404


def test_a_role_without_project_write_is_refused_and_audited(db, sign_in, ids):
    pid, mid, did = ids
    web = TestClient(app, base_url="http://localhost")
    auditor = sign_in(web, db, "auditor")
    assert web.get(_url(pid)).status_code == 200
    assert _reach(web, pid, mid, TARGET).status_code == 403
    assert _status(web, pid, did, "in_progress").status_code == 403
    assert _spend(web, pid, "1.00").status_code == 403
    rows = _rows(db, status="rejected")
    assert [r.action_taken["refusal"] for r in rows] == ["no_permission"] * 3
    assert [r.subject_type for r in rows] == ["plan_milestone", "plan_deliverable", "project"]
    assert {r.actor_user_id for r in rows} == {auditor.id}


# --- Spend


def test_the_newest_entry_is_the_current_figure_and_a_correction_names_it(db, pm, ids):
    pid, _, _ = ids
    first = _spend(pm[0], pid, "250.00")
    assert first.status_code == 201, first.text
    first_id = first.json()["current_spend"]["id"]
    second = _spend(pm[0], pid, "400.5").json()
    assert second["current_spend"]["amount"] == "400.50"
    correction = _spend(pm[0], pid, "390.50", supersedes_id=second["current_spend"]["id"])
    assert correction.status_code == 201, correction.text
    body = correction.json()
    assert body["current_spend"]["amount"] == "390.50"
    assert body["current_spend"]["supersedes_id"] == second["current_spend"]["id"]
    assert body["current_spend"]["created_by_name"] == pm[1].display_name
    assert body["baseline_total"] == "1000.00"
    assert [e["amount"] for e in body["spend"]["items"]] == ["390.50", "400.50", "250.00"]
    assert body["spend"]["total"] == 3
    # Every entry is still there as it was written.
    db.expire_all()
    assert db.get(ProjectSpend, first_id).amount == Decimal("250.00")
    actions = [r.action_taken["action"] for r in _rows(db, status="ok")]
    assert actions == ["spend_recorded", "spend_recorded", "spend_corrected"]


def test_a_correction_of_an_older_entry_is_stale(db, pm, ids):
    pid, _, _ = ids
    first = _spend(pm[0], pid, "250.00").json()["current_spend"]["id"]
    _spend(pm[0], pid, "300.00")
    response = _spend(pm[0], pid, "200.00", supersedes_id=first)
    assert response.status_code == 409
    assert _refusals(db) == ["stale_spend"]
    db.expire_all()
    assert db.scalar(select(ProjectSpend).order_by(ProjectSpend.id.desc())).amount == 300


def test_a_correction_with_no_entry_yet_is_stale(db, pm, ids):
    pid, _, _ = ids
    assert _spend(pm[0], pid, "200.00", supersedes_id=99).status_code == 409
    assert _refusals(db) == ["stale_spend"]


def test_a_new_entry_needs_no_version_and_leaves_the_project_version(db, pm, ids):
    pid, _, _ = ids
    version = db.get(Project, pid).version
    assert _spend(pm[0], pid, "1.00").status_code == 201
    assert _spend(pm[0], pid, "2.00").status_code == 201
    db.expire_all()
    assert db.get(Project, pid).version == version


@pytest.mark.parametrize("amount", ["-1.00", "1.001", "abc"])
def test_a_bad_amount_is_422(db, pm, ids, amount):
    assert _spend(pm[0], ids[0], amount).status_code == 422


def _entry(db: Session, pid: int, owner: User) -> ProjectSpend:
    entry = ProjectSpend(
        project_id=pid, amount=Decimal("10.00"), as_of=TODAY, note="Ledger", created_by=owner.id
    )
    db.add(entry)
    db.commit()
    return entry


def test_spend_entries_refuse_an_update(db, pm, ids):
    entry = _entry(db, ids[0], pm[1])
    entry.amount = Decimal("20.00")
    with pytest.raises(AppendOnlyError):
        db.commit()
    db.rollback()


def test_spend_entries_refuse_a_delete(db, pm, ids):
    entry = _entry(db, ids[0], pm[1])
    db.delete(entry)
    with pytest.raises(AppendOnlyError):
        db.commit()
    db.rollback()


@pytest.mark.parametrize(
    "statement",
    [update(ProjectSpend).values(amount=0), delete(ProjectSpend)],
    ids=["update", "delete"],
)
def test_spend_entries_refuse_bulk_changes(db, pm, ids, statement):
    _entry(db, ids[0], pm[1])
    with pytest.raises(AppendOnlyError):
        db.execute(statement)
    db.rollback()
    assert db.scalar(select(ProjectSpend.amount)) == Decimal("10.00")


# --- The plan and baseline stay as they were; every write is audited


def test_no_write_changes_the_plan_or_the_baseline(db, pm, ids):
    pid, mid, did = ids
    before = _plan_state(db, pid)
    assert _reach(pm[0], pid, mid, TARGET).status_code == 200
    assert _plan_state(db, pid) == before
    assert _status(pm[0], pid, did, "in_progress").status_code == 200
    assert _plan_state(db, pid) == before
    assert _spend(pm[0], pid, "5.00").status_code == 201
    assert _plan_state(db, pid) == before


def test_each_write_is_one_audit_row_for_the_person(db, pm, ids):
    pid, mid, did = ids
    _reach(pm[0], pid, mid, TARGET)
    _status(pm[0], pid, did, "in_progress")
    _spend(pm[0], pid, "5.00")
    rows = _rows(db)
    assert [(r.subject_type, r.status, r.trigger) for r in rows] == [
        ("plan_milestone", "ok", "human"),
        ("plan_deliverable", "ok", "human"),
        ("project_spend", "ok", "human"),
    ]
    assert {r.actor_user_id for r in rows} == {pm[1].id}
    assert rows[0].signals["reached_on"] is None
    assert rows[0].output["reached_on"] == TARGET.isoformat()
    assert rows[1].signals["status"] == "not_started"
    assert rows[1].output["status"] == "in_progress"
    assert rows[2].output == {
        "amount": "5.00",
        "as_of": "2027-05-31",
        "note": "Finance report, May",
        "supersedes_id": None,
    }


def test_reading_progress_of_an_unknown_project_is_404(db, pm):
    assert pm[0].get(_url(999)).status_code == 404
