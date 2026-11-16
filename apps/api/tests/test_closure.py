"""The closure report (item 7.7): figures worked out by code with sources that match the
baselines (a project with two approved changes), benefits measured at close by a person, lessons,
versions on every write, one audit row per write and per refusal, and a closed project's record
refusing every write."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from test_acceptance import make_project

from app import delivery
from app.baselines import freeze
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    ChangeRequest,
    ClosureBenefit,
    Lesson,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectBaseline,
    ProjectPlan,
    ProjectRequest,
    ProjectSpend,
    Risk,
    User,
    ValueCase,
    ValueCaseBenefit,
)

TODAY = date(2027, 6, 1)
REPORT = "/api/v1/projects/{pid}/closure-report"
CLOSURE = "/api/v1/projects/{pid}/closure"
LESSONS = "/api/v1/projects/{pid}/lessons"


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
def web(db) -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def pm(web, db, sign_in) -> User:
    return sign_in(web, db, "project_manager")


def approve_change(db: Session, pid: int, user: User, amount: str, title: str) -> ChangeRequest:
    """An approved change that adds `amount` to the plan and makes the next baseline."""
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == pid))
    change = ChangeRequest(
        project_id=pid,
        title=title,
        reason="The client asked for it",
        changes={},
        effect={"cost_difference": amount},
        decider="sponsor",
        measured_baseline_number=1,
        status="approved",
        raised_by=user.id,
        decided_by=user.id,
        decision_reason="Worth it",
        decided_at=datetime(2027, 4, 1),
    )
    db.add(change)
    if Decimal(amount) >= 0:
        db.add(
            PlanEstimateLine(plan_id=plan.id, description=title, category="people", amount=amount)
        )
    else:  # a change that takes cost off lowers the first line
        line = db.scalar(select(PlanEstimateLine).where(PlanEstimateLine.plan_id == plan.id))
        line.amount = Decimal(line.amount) + Decimal(amount)
    db.flush()
    freeze(db, plan, change_request_id=change.id)
    db.commit()
    return change


def closing_project(
    db: Session,
    owner: User,
    *,
    uses_ai: bool = False,
    benefits: int = 1,
    sponsor_id: int | None = None,
    stage: str = "closure",
) -> int:
    """A project in closure (or `stage`): its deliverable accepted, its milestone reached a day
    after its baseline 1 target, a spend entry, and a value case with `benefits` benefits on the
    request it came from."""
    pid, _ = make_project(db, owner, status="accepted", sponsor_id=sponsor_id)
    plan_id = db.scalar(select(ProjectPlan.id).where(ProjectPlan.project_id == pid))
    db.execute(
        update(PlanMilestone)
        .where(PlanMilestone.plan_id == plan_id)
        .values(reached_on=date(2027, 3, 2))
    )
    db.execute(update(Project).where(Project.id == pid).values(stage=stage))
    request = ProjectRequest(
        title="Forecasts",
        asked_for="A forecast",
        problem="Guesswork",
        outcome="Better plans",
        requested_by=owner.id,
        status="approved",
        uses_ai=uses_ai,
        project_id=pid,
    )
    db.add(request)
    db.flush()
    case = ValueCase(request_id=request.id, strategic_fit="Core", source="person")
    db.add(case)
    db.flush()
    for n in range(benefits):
        db.add(
            ValueCaseBenefit(
                value_case_id=case.id,
                position=n,
                description=f"Faster planning {n}",
                measure="Days to plan",
                current_value="ten days",
                target="two days",
                target_date=date(2027, 9, 1),
            )
        )
    db.add(
        ProjectSpend(
            project_id=pid,
            amount="1400.00",
            as_of=date(2027, 5, 31),
            note="Final",
            created_by=owner.id,
        )
    )
    db.commit()
    return pid


def create(web: TestClient, pid: int):
    return web.post(REPORT.format(pid=pid))


def report_version(web: TestClient, pid: int) -> int:
    return web.get(CLOSURE.format(pid=pid)).json()["report"]["version"]


def refusals(db: Session, agent: str = "closure") -> list[str]:
    db.expire_all()
    stmt = select(AuditDecision).where(
        AuditDecision.agent == agent, AuditDecision.status == "rejected"
    )
    return [r.action_taken["refusal"] for r in db.scalars(stmt.order_by(AuditDecision.id))]


def _figures(body: dict) -> dict[str, dict]:
    return {f["key"]: f for f in body["report"]["figures"]}


# --- Figures


def test_the_figures_match_the_baselines_for_a_project_with_two_approved_changes(db, web, pm):
    pid = closing_project(db, pm)
    first = approve_change(db, pid, pm, "300.00", "Extra region")
    second = approve_change(db, pid, pm, "-100.00", "Drop a dashboard")
    made = create(web, pid)
    assert made.status_code == 201, made.text
    figures = _figures(made.json())
    baselines = {
        b.number: b
        for b in db.scalars(select(ProjectBaseline).where(ProjectBaseline.project_id == pid))
    }
    assert set(baselines) == {1, 2, 3}

    assert figures["first_baseline_total"]["value"] == "1000.00"
    assert figures["first_baseline_total"]["sources"] == [
        {"table": "project_baselines", "id": baselines[1].id, "field": "total"}
    ]
    assert figures["final_baseline_number"]["value"] == 3
    assert figures["final_baseline_total"]["value"] == str(baselines[3].total) == "1200.00"
    assert figures["final_baseline_total"]["sources"][0]["id"] == baselines[3].id
    assert figures["baseline_moved"]["value"] == "200.00"

    # Every approved change, with the baseline it made.
    assert figures[f"change_{first.id}_cost"]["value"] == "300.00"
    assert figures[f"change_{second.id}_cost"]["value"] == "-100.00"
    assert figures[f"change_{first.id}_baseline"]["value"] == 2
    assert figures[f"change_{second.id}_baseline"]["value"] == 3
    assert figures[f"change_{second.id}_baseline"]["sources"] == [
        {"table": "project_baselines", "id": baselines[3].id, "field": "number"}
    ]
    assert figures["approved_changes"]["value"] == 2
    assert figures["approved_changes_cost"]["value"] == "200.00"
    # Baseline 1 plus every approved change is the final baseline.
    assert Decimal(figures["first_baseline_total"]["value"]) + Decimal(
        figures["approved_changes_cost"]["value"]
    ) == Decimal(figures["final_baseline_total"]["value"])

    # Final spend against the final baseline.
    assert figures["final_spend"]["value"] == "1400.00"
    assert figures["spend_less_final_baseline"]["value"] == "200.00"
    assert figures["final_spent_pct"]["value"] == "116.67"
    assert {s["table"] for s in figures["spend_less_final_baseline"]["sources"]} == {
        "project_spend",
        "project_baselines",
    }

    # Milestones against their baseline 1 target, not a later one.
    milestone = db.scalar(select(PlanMilestone).where(PlanMilestone.plan_id.is_not(None)))
    target = figures[f"milestone_{milestone.id}_target"]
    assert target["value"] == "2027-03-01"
    assert target["sources"][0]["id"] == baselines[1].id
    assert figures[f"milestone_{milestone.id}_days_late"]["value"] == 1
    assert figures["milestones_reached"]["value"] == 1
    assert figures["milestones_on_target"]["value"] == 0

    assert figures["deliverables_in_baseline"]["value"] == 1
    assert figures["deliverables_accepted"]["value"] == 1
    assert figures["accepted_risks"]["value"] == 0
    # Every source names a row that exists.
    for figure in figures.values():
        for source in figure["sources"]:
            assert isinstance(source["id"], int) and source["table"] and source["field"]


def test_accepted_risks_still_standing_are_listed(db, web, pm):
    pid = closing_project(db, pm)
    risk = Risk(
        project_id=pid,
        kind="threat",
        title="Key analyst leaves",
        cause="c",
        effect="e",
        category="project",
        objectives=["schedule"],
        response="accept",
        response_plan="p",
        status="accepted",
        source="person",
        band="Low",
    )
    db.add(risk)
    db.commit()
    body = create(web, pid).json()
    assert body["accepted_risks"] == [{"id": risk.id, "title": "Key analyst leaves", "band": "Low"}]
    assert _figures(body)["accepted_risks"]["sources"] == [
        {"table": "risks", "id": risk.id, "field": "status"}
    ]


# --- Writes, versions and audit rows


def test_create_edit_refresh_each_write_one_audit_row(db, web, pm):
    pid = closing_project(db, pm)
    assert create(web, pid).status_code == 201
    again = create(web, pid)
    assert again.status_code == 409
    edited = web.patch(
        REPORT.format(pid=pid),
        json={"version": 1, "sections": {"delivered": "The pilot, accepted by the client."}},
    )
    assert edited.status_code == 200, edited.text
    report = edited.json()["report"]
    assert report["version"] == 2
    assert report["section_states"]["delivered"]["source"] == "person"
    assert report["section_states"]["delivered"]["unreviewed"] is False
    refreshed = web.post(REPORT.format(pid=pid) + "/refresh", json={"version": 2})
    assert refreshed.status_code == 200
    db.expire_all()
    rows = db.scalars(
        select(AuditDecision)
        .where(AuditDecision.agent == "closure", AuditDecision.status == "ok")
        .order_by(AuditDecision.id)
    ).all()
    assert [r.action_taken["action"] for r in rows] == ["created", "text_edited", "refreshed"]
    assert [r.version for r in rows] == [1, 2, 3]
    assert refusals(db) == ["report_exists"]


def test_a_stale_version_is_refused_and_audited(db, web, pm):
    pid = closing_project(db, pm)
    create(web, pid)
    web.patch(REPORT.format(pid=pid), json={"version": 1, "sections": {"remaining": "Nothing."}})
    stale = web.patch(
        REPORT.format(pid=pid), json={"version": 1, "sections": {"remaining": "Something."}}
    )
    assert stale.status_code == 409
    assert web.post(REPORT.format(pid=pid) + "/refresh", json={"version": 1}).status_code == 409
    assert refusals(db) == ["stale_version", "stale_version"]
    assert web.get(CLOSURE.format(pid=pid)).json()["report"]["sections"]["remaining"] == "Nothing."


def test_a_project_not_in_closure_is_refused_and_audited(db, web, pm):
    pid = closing_project(db, pm, stage="delivery")
    refused = create(web, pid)
    assert refused.status_code == 409
    assert refusals(db) == ["not_in_closure"]
    assert web.get(CLOSURE.format(pid=pid)).json()["report"] is None


def test_a_role_without_report_publish_is_refused_and_audited(db, web, pm, sign_in):
    pid = closing_project(db, pm)
    sponsor = TestClient(app, base_url="http://localhost")
    sign_in(sponsor, db, "sponsor")
    assert create(sponsor, pid).status_code == 403
    assert refusals(db) == ["no_permission"]


def test_confirm_needs_an_unreviewed_agent_section(db, web, pm):
    pid = closing_project(db, pm)
    create(web, pid)
    refused = web.post(REPORT.format(pid=pid) + "/sections/delivered/confirm", json={"version": 1})
    assert refused.status_code == 409
    assert refusals(db) == ["not_agent_draft"]


# --- Benefits


def _benefit_id(db: Session) -> int:
    return db.scalar(select(ValueCaseBenefit.id))


def test_a_person_enters_a_benefits_value_now_with_its_date_and_version(db, web, pm):
    pid = closing_project(db, pm)
    bid = _benefit_id(db)
    url = REPORT.format(pid=pid) + f"/benefits/{bid}"
    missing_day = web.put(url, json={"version": 0, "value_now": "three days"})
    assert missing_day.status_code == 422
    done = web.put(
        url, json={"version": 0, "value_now": "three days", "value_now_on": "2027-05-30"}
    )
    assert done.status_code == 200, done.text
    benefit = done.json()["benefits"][0]
    assert benefit | {"benefit_id": None} == {
        "benefit_id": None,
        "position": 0,
        "description": "Faster planning 0",
        "measure": "Days to plan",
        "value_before": "ten days",
        "target": "two days",
        "target_date": "2027-09-01",
        "value_now": "three days",
        "value_now_on": "2027-05-30",
        "measure_owner_id": None,
        "measure_owner_name": None,
        "measure_by": None,
        "version": 1,
    }
    stale = web.put(url, json={"version": 0, "measure_owner_id": pm.id, "measure_by": "2027-09-01"})
    assert stale.status_code == 409
    owned = web.put(url, json={"version": 1, "measure_owner_id": pm.id, "measure_by": "2027-09-01"})
    assert owned.status_code == 200
    assert owned.json()["benefits"][0]["measure_owner_name"] == pm.display_name
    db.expire_all()
    rows = db.scalars(
        select(AuditDecision).where(AuditDecision.subject_type == "closure_benefit")
    ).all()
    assert [(r.status, r.version) for r in rows] == [("ok", 1), ("rejected", 1), ("ok", 2)]
    # The value case itself is never changed.
    assert db.get(ValueCaseBenefit, bid).current_value == "ten days"


# --- Lessons


def _lesson(**extra) -> dict:
    return {
        "category": "schedule",
        "what_happened": "The data access approval took longer than planned.",
        "do_differently": "Ask for data access before the go-ahead.",
        **extra,
    }


def test_lessons_are_recorded_in_delivery_and_closure_only(db, web, pm):
    in_delivery = closing_project(db, pm, stage="delivery")
    made = web.post(LESSONS.format(pid=in_delivery), json=_lesson(suggest_as_risk=True))
    assert made.status_code == 201, made.text
    assert made.json() | {"id": 0, "created_at": None, "reviewed_at": None} == {
        "id": 0,
        "project_id": in_delivery,
        "category": "schedule",
        "what_happened": "The data access approval took longer than planned.",
        "do_again": None,
        "do_differently": "Ask for data access before the go-ahead.",
        "suggest_as_risk": True,
        "source": "person",
        "status": "recorded",
        "reviewed_by": pm.id,
        "reviewed_at": None,
        "version": 1,
        "created_at": None,
    }
    planning = closing_project(db, pm, stage="planning")
    assert web.post(LESSONS.format(pid=planning), json=_lesson()).status_code == 409
    no_takeaway = web.post(LESSONS.format(pid=in_delivery), json=_lesson(do_differently=None))
    assert no_takeaway.status_code == 422
    assert refusals(db) == ["lessons_not_open"]


def test_a_suggestion_is_recorded_edited_or_discarded_by_a_person(db, web, pm):
    pid = closing_project(db, pm)
    suggestions = [
        Lesson(
            project_id=pid,
            category="ai",
            what_happened=f"The model drifted {word}.",
            do_differently="Monitor it weekly.",
            source="agent",
            status="suggested",
        )
        for word in ("once", "twice", "again")
    ]
    db.add_all(suggestions)
    db.commit()
    url = LESSONS.format(pid=pid)
    kept = web.post(f"{url}/{suggestions[0].id}/record", json={"version": 1})
    assert kept.json()["status"] == "recorded" and kept.json()["source"] == "agent"
    edited = web.patch(f"{url}/{suggestions[1].id}", json={**_lesson(category="ai"), "version": 1})
    assert edited.json()["status"] == "recorded"
    dropped = web.post(f"{url}/{suggestions[2].id}/discard", json={"version": 1})
    assert dropped.json()["status"] == "discarded"
    again = web.post(f"{url}/{suggestions[2].id}/record", json={"version": 2})
    assert again.status_code == 409
    twice = web.post(f"{url}/{suggestions[0].id}/discard", json={"version": 2})
    assert twice.status_code == 409
    assert refusals(db) == ["discarded", "not_suggested"]


# --- A closed project refuses every write


def test_a_closed_project_refuses_every_write(db, web, pm):
    pid = closing_project(db, pm)
    create(web, pid)
    lesson = web.post(LESSONS.format(pid=pid), json=_lesson()).json()
    db.execute(update(Project).where(Project.id == pid).values(stage="closed"))
    db.commit()
    url = REPORT.format(pid=pid)
    bid = _benefit_id(db)
    attempts = [
        web.post(url),
        web.patch(url, json={"version": 1, "sections": {"remaining": "x"}}),
        web.post(url + "/refresh", json={"version": 1}),
        web.put(
            url + f"/benefits/{bid}",
            json={"version": 0, "value_now": "x", "value_now_on": "2027-06-01"},
        ),
        web.post(LESSONS.format(pid=pid), json=_lesson()),
        web.patch(f"{LESSONS.format(pid=pid)}/{lesson['id']}", json={**_lesson(), "version": 1}),
    ]
    assert [a.status_code for a in attempts] == [409] * 6
    assert refusals(db) == ["project_closed"] * 6
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ClosureBenefit)) == 0
