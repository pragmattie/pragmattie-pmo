"""Status reports (item 7.3): periods from the plan, figures with sources read from records,
ratings from the reporting policy at each edge, the checker blocking publish, frozen published
reports, corrections that withdraw the old report, drafts hidden from readers without
`report:publish`, and every write and refusal audited."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from policy_files import APPLIED_POLICIES, DRAFT_POLICIES
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import delivery
from app import policy as policy_module
from app import reports as rules
from app.baselines import freeze
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    ChangeRequest,
    GateDecision,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectPlan,
    ProjectSpend,
    Risk,
    User,
)
from app.policy import load_reporting

FIXTURE = Path(__file__).parent / "fixtures" / "policies" / "reporting"
POLICY = load_reporting(FIXTURE)
# Delivery starts on a Monday; the milestone's baseline target is a Monday two months on.
STARTED = datetime(2027, 1, 4, 9, 0)
TARGET = date(2027, 3, 1)
FINISH = date(2027, 5, 3)
TODAY = date(2027, 3, 10)
# Weekly, due on Mondays: the period ending Sunday 7 March is due Monday 8 March.
PERIOD_END = date(2027, 3, 7)


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


def _client(db, sign_in, role: str, email: str | None = None) -> tuple[TestClient, User]:
    web = TestClient(app, base_url="http://localhost")
    return web, sign_in(web, db, role, email)


@pytest.fixture
def pm(db, sign_in):
    return _client(db, sign_in, "project_manager")


@pytest.fixture
def sponsor(db, sign_in):
    return _client(db, sign_in, "sponsor")


def _project(db: Session, owner: User, *, report_period: str | None = "weekly") -> dict:
    """A project in delivery managed by `owner`: two milestones (on `TARGET` and `FINISH`), one
    deliverable, one line of 1000.00, a weekly report due on Mondays, and baseline 1 frozen on
    `STARTED`."""
    project = Project(
        name="Forecast",
        stage="delivery",
        sponsor="S",
        pm="P",
        pm_user_id=owner.id,
        status="on_track",
    )
    db.add(project)
    db.flush()
    plan = ProjectPlan(
        project_id=project.id,
        scope_in="The pilot",
        contingency_pct=0,
        report_period=report_period,
        report_due_day=1 if report_period else None,
    )
    db.add(plan)
    db.flush()
    first = PlanMilestone(plan_id=plan.id, name="Pilot live", target_date=TARGET)
    last = PlanMilestone(plan_id=plan.id, name="Handover", target_date=FINISH)
    db.add_all([first, last])
    db.flush()
    deliverable = PlanDeliverable(plan_id=plan.id, name="Model", milestone_id=first.id)
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
    baseline = freeze(db, plan, decision.id)
    baseline.created_at = STARTED
    db.commit()
    return {
        "pid": project.id,
        "plan": plan.id,
        "milestone": first.id,
        "deliverable": deliverable.id,
        "baseline": baseline.id,
    }


@pytest.fixture
def ids(db, pm):
    return _project(db, pm[1])


def _url(pid: int, suffix: str = "") -> str:
    return f"/api/v1/projects/{pid}/status-reports{suffix}"


def _create(web: TestClient, pid: int, end: date = PERIOD_END, **extra):
    return web.post(_url(pid), json={"period_end": end.isoformat(), **extra})


def _edit(web: TestClient, pid: int, rid: int, version: int, **sections):
    return web.patch(_url(pid, f"/{rid}"), json={"version": version, "sections": sections})


def _act(web: TestClient, pid: int, rid: int, what: str, version: int, **extra):
    return web.post(_url(pid, f"/{rid}/{what}"), json={"version": version, **extra})


def _figures(report: dict) -> dict:
    return {f["key"]: f for f in report["figures"]}


def _rows(db: Session, **filters) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(AuditDecision.agent == "status_report")
    for name, value in filters.items():
        stmt = stmt.where(getattr(AuditDecision, name) == value)
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def _refusals(db: Session) -> list[str]:
    return [r.action_taken["refusal"] for r in _rows(db, status="rejected")]


def _risk(db: Session, pid: int, band: str | None, **extra) -> Risk:
    risk = Risk(
        project_id=pid,
        kind="threat",
        title=f"A {band} risk",
        cause="A cause",
        effect="An effect",
        category="project",
        objectives=["schedule"],
        band=band,
        response="reduce",
        response_plan="A plan",
        status="open",
        source="person",
        **extra,
    )
    db.add(risk)
    db.commit()
    return risk


def _change(db: Session, pid: int, raised_by: int, **fields) -> ChangeRequest:
    values = {
        "title": "More analysis",
        "reason": "The client asked for more",
        "changes": {},
        "effect": {"cost_difference": "50.00"},
        "decider": "project_manager",
        "measured_baseline_number": 1,
        "status": "submitted",
        **fields,
    }
    change = ChangeRequest(project_id=pid, raised_by=raised_by, **values)
    db.add(change)
    db.commit()
    return change


# --- Periods


def test_weekly_periods_end_the_day_before_the_due_day():
    found = rules.periods("weekly", 1, date(2027, 1, 4), date(2027, 1, 20))
    assert found == [
        rules.Period(date(2027, 1, 4), date(2027, 1, 10), date(2027, 1, 11)),
        rules.Period(date(2027, 1, 11), date(2027, 1, 17), date(2027, 1, 18)),
        rules.Period(date(2027, 1, 18), date(2027, 1, 24), date(2027, 1, 25)),
    ]


def test_fortnights_count_from_the_first_due_day_in_delivery():
    # Delivery starts on a Wednesday; the first Monday on or after it is 11 January.
    found = rules.periods("fortnightly", 1, date(2027, 1, 6), date(2027, 2, 10))
    assert [(p.start, p.end, p.due_on) for p in found] == [
        (date(2026, 12, 28), date(2027, 1, 10), date(2027, 1, 11)),
        (date(2027, 1, 11), date(2027, 1, 24), date(2027, 1, 25)),
        (date(2027, 1, 25), date(2027, 2, 7), date(2027, 2, 8)),
        (date(2027, 2, 8), date(2027, 2, 21), date(2027, 2, 22)),
    ]
    # Delivery starting on the due day itself: that period ended before it, so the first
    # report is due a fortnight later, and the periods still don't drift.
    on_the_day = rules.periods("fortnightly", 1, date(2027, 1, 4), date(2027, 2, 1))
    assert [p.due_on for p in on_the_day] == [
        date(2027, 1, 18),
        date(2027, 2, 1),
        date(2027, 2, 15),
    ]


def test_monthly_periods_are_calendar_months_due_the_next_month_across_a_year_end():
    found = rules.periods("monthly", 10, date(2026, 11, 20), date(2027, 1, 5))
    assert found == [
        rules.Period(date(2026, 11, 1), date(2026, 11, 30), date(2026, 12, 10)),
        rules.Period(date(2026, 12, 1), date(2026, 12, 31), date(2027, 1, 10)),
        rules.Period(date(2027, 1, 1), date(2027, 1, 31), date(2027, 2, 10)),
    ]
    october = rules.periods("monthly", 10, date(2026, 10, 1), date(2026, 10, 2))[0]
    assert october.due_on == date(2026, 11, 10)
    assert rules.next_period("monthly", 10, found[1]) == found[2]
    assert rules.next_period(
        "weekly", 1, rules.Period(date(2027, 1, 4), date(2027, 1, 10), date(2027, 1, 11))
    ) == rules.Period(date(2027, 1, 11), date(2027, 1, 17), date(2027, 1, 18))


def test_the_list_shows_the_project_periods_newest_first(db, pm, ids):
    response = pm[0].get(_url(ids["pid"]), params={"limit": 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["report_period"], body["report_due_day"]) == ("weekly", 1)
    # 4 January to 14 March, the period holding today: 10 weeks.
    assert body["total"] == 10
    assert [(p["start"], p["end"], p["due_on"]) for p in body["items"]] == [
        ("2027-03-08", "2027-03-14", "2027-03-15"),
        ("2027-03-01", "2027-03-07", "2027-03-08"),
    ]


# --- Ratings at each edge


def _rated(**values) -> dict:
    figures = [
        {"key": "most_days_late", "label": "", "kind": "days", "value": 0},
        {"key": "spend_to_date", "label": "", "kind": "money", "value": "100.00"},
        {"key": "spend_ahead_points", "label": "", "kind": "percent", "value": "0.00"},
        {"key": "scope_moved_pct", "label": "", "kind": "percent", "value": "0.00"},
    ]
    for figure in figures:
        if figure["key"] in values:
            figure["value"] = values.pop(figure["key"])
    for band, count in values.items():
        figures.append(
            {
                "key": f"open_risks_{band.lower()}",
                "label": f"Open risks: {band}",
                "kind": "count",
                "value": count,
            }
        )
    return rules.rate(figures, POLICY)


@pytest.mark.parametrize(
    ("days", "rating", "rule"),
    [
        (0, "green", "schedule.amber_above_working_days"),
        (1, "amber", "schedule.amber_above_working_days"),
        (10, "amber", "schedule.amber_above_working_days"),
        (11, "red", "schedule.red_above_working_days"),
    ],
)
def test_schedule_rating_edges(days, rating, rule):
    schedule = _rated(most_days_late=days)["schedule"]
    assert (schedule["rating"], schedule["rule"], schedule["measure"]) == (rating, rule, str(days))


@pytest.mark.parametrize(
    ("points", "rating", "rule"),
    [
        ("-20.00", "green", "cost.amber_above_points"),
        ("5.00", "green", "cost.amber_above_points"),
        ("5.01", "amber", "cost.amber_above_points"),
        ("15.00", "amber", "cost.amber_above_points"),
        ("15.01", "red", "cost.red_above_points"),
    ],
)
def test_cost_rating_edges(points, rating, rule):
    cost = _rated(spend_ahead_points=points)["cost"]
    assert (cost["rating"], cost["rule"]) == (rating, rule)


def test_cost_with_no_spend_recorded_is_green_and_says_so():
    cost = _rated(spend_to_date=None, spend_ahead_points=None)["cost"]
    assert (cost["rating"], cost["rule"]) == ("green", "cost.no_spend")


@pytest.mark.parametrize(
    ("pct", "rating", "rule"),
    [
        ("5.00", "green", "scope.amber_above_pct"),
        ("5.01", "amber", "scope.amber_above_pct"),
        ("10.00", "amber", "scope.amber_above_pct"),
        ("10.01", "red", "scope.red_above_pct"),
        (None, "amber", "scope.not_measurable"),
    ],
)
def test_scope_rating_edges(pct, rating, rule):
    scope = _rated(scope_moved_pct=pct)["scope"]
    assert (scope["rating"], scope["rule"]) == (rating, rule)


@pytest.mark.parametrize(
    ("bands", "rating", "rule"),
    [
        ({"Low": 5}, "green", "risk.amber_at"),
        ({"Medium": 1}, "amber", "risk.amber_at.Medium"),
        ({"High": 1}, "amber", "risk.amber_at.High"),
        ({"High": 2}, "red", "risk.red_at.High"),
    ],
)
def test_risk_rating_edges(bands, rating, rule):
    risk = _rated(**bands)["risk"]
    assert (risk["rating"], risk["rule"]) == (rating, rule)


def test_overall_is_the_worst_of_the_four():
    ratings = _rated(most_days_late=3, scope_moved_pct="10.01")
    assert ratings["schedule"]["rating"] == "amber"
    assert ratings["overall"]["rating"] == "red"
    assert ratings["overall"]["rule"] == "overall.worst_of_four"
    assert _rated()["overall"]["rating"] == "green"


# --- Figures from records


def test_a_draft_reads_its_figures_from_records_with_sources(db, pm, ids):
    pid = ids["pid"]
    db.add(
        ProjectSpend(
            project_id=pid,
            amount="400.00",
            as_of=date(2027, 3, 5),
            note="Finance",
            created_by=pm[1].id,
        )
    )
    db.commit()
    response = _create(pm[0], pid)
    assert response.status_code == 201, response.text
    report = response.json()
    assert (report["status"], report["period_start"], report["period_end"], report["due_on"]) == (
        "draft",
        "2027-03-01",
        "2027-03-07",
        "2027-03-08",
    )
    figures = _figures(report)
    baseline = {"table": "project_baselines", "id": ids["baseline"], "field": "total"}
    assert figures["baseline_total"]["value"] == "1000.00"
    assert figures["baseline_total"]["sources"] == [baseline]
    assert figures["baseline_number"]["value"] == 1
    assert figures["spend_to_date"]["value"] == "400.00"
    assert figures["spend_as_of"]["value"] == "2027-03-05"
    assert figures["spent_pct"]["value"] == "40.00"
    # 62 of the 119 days from 4 January to 3 May had passed by 7 March.
    assert figures["time_elapsed_pct"]["value"] == "52.10"
    assert figures["spend_ahead_points"]["value"] == "-12.10"
    # Not reached by Sunday 7 March: 4 working days (Tuesday to Friday) past Monday 1 March.
    late = figures[f"milestone_{ids['milestone']}_days_late"]
    assert late["value"] == 4
    assert {"table": "plan_milestones", "id": ids["milestone"], "field": "reached_on"} in late[
        "sources"
    ]
    assert figures["most_days_late"]["value"] == 4
    assert figures["milestones_late"]["value"] == 1
    assert figures["milestones_reached"]["value"] == 0
    assert figures["deliverables_not_started"]["sources"] == [
        {"table": "plan_deliverables", "id": ids["deliverable"], "field": "status"}
    ]
    for figure in report["figures"]:
        assert figure["note"], figure["key"]
        if figure["value"] not in (None, 0, "0.00"):
            assert figure["sources"], figure["key"]
    assert report["ratings"]["schedule"]["rating"] == "amber"
    assert report["ratings"]["cost"]["rating"] == "green"
    assert report["ratings"]["overall"]["rating"] == "amber"
    assert report["sections"] == dict.fromkeys(["summary", "done", "next", "issues", "decisions"])


def test_refresh_changes_a_draft_and_a_published_report_stays_as_it_was(db, pm, ids):
    pid, mid = ids["pid"], ids["milestone"]
    web = pm[0]
    published = _create(web, pid, date(2027, 2, 28)).json()
    assert _act(web, pid, published["id"], "publish", 1).status_code == 200
    draft = _create(web, pid).json()
    assert _figures(draft)["milestones_reached"]["value"] == 0

    # The source row changes: the milestone was reached on Tuesday 2 March.
    db.execute(
        update(PlanMilestone.__table__)
        .where(PlanMilestone.__table__.c.id == mid)
        .values(reached_on=date(2027, 3, 2))
    )
    _risk(db, pid, "High")
    db.commit()

    refreshed = _act(web, pid, draft["id"], "refresh", 1)
    assert refreshed.status_code == 200, refreshed.text
    figures = _figures(refreshed.json())
    assert figures["milestones_reached"]["value"] == 1
    assert figures[f"milestone_{mid}_reached"]["value"] == "2027-03-02"
    assert figures[f"milestone_{mid}_days_late"]["value"] == 1
    assert figures["open_risks_high"]["value"] == 1
    assert refreshed.json()["ratings"]["risk"]["rating"] == "amber"
    assert refreshed.json()["version"] == 2

    # The published one is frozen: its figures stay, and it can't be refreshed.
    again = web.get(_url(pid, f"/{published['id']}")).json()
    assert again["figures"] == published["figures"]
    assert again["ratings"] == published["ratings"]
    assert _act(web, pid, published["id"], "refresh", 2).status_code == 409
    assert _edit(web, pid, published["id"], 2, summary="Changed").status_code == 409
    assert _refusals(db) == ["not_draft", "not_draft"]


def test_figures_are_as_of_the_period_end_where_the_records_allow(db, pm, ids):
    pid, web, who = ids["pid"], pm[0], pm[1].id
    plan = db.get(ProjectPlan, ids["plan"])
    # Baseline 2, approved on Tuesday 9 March, after the period ending 7 March: 1500.00.
    db.add(
        PlanEstimateLine(plan_id=plan.id, description="Extra", category="people", amount="500.00")
    )
    change = _change(
        db, pid, who, status="approved", decided_at=datetime(2027, 3, 9, 10), decider="sponsor"
    )
    second = freeze(db, plan, change_request_id=change.id)
    second.created_at = datetime(2027, 3, 9, 10)
    # Spend: 400.00 as of 5 March, then a newer entry dated 9 March, after the period.
    before = ProjectSpend(
        project_id=pid, amount="400.00", as_of=date(2027, 3, 5), note="Finance", created_by=who
    )
    db.add(before)
    db.flush()
    db.add(
        ProjectSpend(
            project_id=pid, amount="900.00", as_of=date(2027, 3, 9), note="Later", created_by=who
        )
    )
    db.commit()

    report = _create(web, pid).json()
    figures = _figures(report)
    assert figures["baseline_number"]["value"] == 1
    assert figures["baseline_total"]["value"] == "1000.00"
    assert figures["baseline_total"]["sources"] == [
        {"table": "project_baselines", "id": ids["baseline"], "field": "total"}
    ]
    assert figures["scope_moved_pct"]["value"] == "0.00"
    assert figures["spend_to_date"]["value"] == "400.00"
    assert figures["spend_to_date"]["sources"] == [
        {"table": "project_spend", "id": before.id, "field": "amount"}
    ]
    assert figures["spend_as_of"]["value"] == "2027-03-05"
    assert figures["spent_pct"]["value"] == "40.00"
    assert figures["approved_changes"]["value"] == 0
    assert report["ratings"]["scope"]["rating"] == "green"

    # The period holding 9 March reads the new baseline and spend.
    later = _figures(_create(web, pid, date(2027, 3, 14)).json())
    assert (later["baseline_number"]["value"], later["baseline_total"]["value"]) == (2, "1500.00")
    assert later["spend_to_date"]["value"] == "900.00"
    assert later["approved_changes"]["value"] == 1

    # Figures with no history are current values, and their notes say as of when.
    read_on = report["figures_at"][:10]
    for key in ("deliverables_not_started", "open_escalations", "changes_waiting"):
        assert f"as of {read_on}" in figures[key]["note"], key
    _risk(db, pid, "Low")
    refreshed = _figures(_act(web, pid, report["id"], "refresh", 1).json())
    assert f"as of {read_on}" in refreshed["open_risks_low"]["note"]


def test_escalations_count_only_decisions_waiting_above_the_project_manager(db, pm, ids):
    pid, who = ids["pid"], pm[1].id
    _risk(db, pid, "High", escalated_to="sponsor", escalated_band="High")
    _risk(db, pid, "Low", escalated_to="project_manager", escalated_band="Low")
    _risk(db, pid, "High", escalated_to="sponsor", escalated_band="High").status = "accepted"
    db.commit()
    sponsor_change = _change(db, pid, who, decider="sponsor")
    _change(db, pid, who, decider="project_manager")
    approved = _change(
        db, pid, who, status="approved", decided_at=datetime(2027, 3, 3, 12), decider="sponsor"
    )
    figures = _figures(_create(pm[0], pid).json())
    assert figures["open_escalations"]["value"] == 2
    assert {s["table"] for s in figures["open_escalations"]["sources"]} == {
        "risks",
        "change_requests",
    }
    assert {"table": "change_requests", "id": sponsor_change.id, "field": "decider"} in figures[
        "open_escalations"
    ]["sources"]
    # Every waiting change, whoever decides it.
    assert figures["changes_waiting"]["value"] == 2
    assert figures["approved_changes"]["value"] == 1
    assert figures["approved_changes_cost"]["value"] == "50.00"
    assert figures["approved_changes_cost"]["sources"] == [
        {"table": "change_requests", "id": approved.id, "field": "effect.cost_difference"}
    ]
    assert figures["open_risks_high"]["value"] == 1
    assert figures["open_risks_low"]["value"] == 1


# --- The checker


def test_the_checker_flags_stray_figures_and_blocks_publishing(db, pm, ids):
    pid, web = ids["pid"], pm[0]
    rid = _create(web, pid).json()["id"]
    response = _edit(
        web,
        pid,
        rid,
        1,
        summary="Spent $999.00 so far and ran 7 workshops.",
        issues="Testing slipped to 2027-03-05.",
    )
    assert response.status_code == 200, response.text
    assert response.json()["flags"] == [
        {"rule": "stray_money", "section": "summary", "text": "$999.00"},
        {"rule": "stray_number", "section": "summary", "text": "7"},
        {"rule": "stray_date", "section": "issues", "text": "2027-03-05"},
    ]
    refused = _act(web, pid, rid, "publish", 2)
    assert refused.status_code == 409
    assert refused.json()["detail"]["flags"] == response.json()["flags"]
    row = _rows(db, status="rejected")[-1]
    assert row.action_taken["refusal"] == "flags_open"
    assert row.subject_id == str(rid)

    # Quoting the figures is fine: the baseline total, days late, a target date, the period.
    fixed = _edit(
        web,
        pid,
        rid,
        2,
        summary="Against a baseline of $1,000.00, Pilot live is 4 working days late.",
        issues="Pilot live was due 1 March 2027; this covers 2027-03-01 to 2027-03-07.",
    )
    assert fixed.json()["flags"] == []
    published = _act(web, pid, rid, "publish", 3)
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["published_by"] == pm[1].id


def test_plain_numbers_match_figures_whatever_their_form():
    figures = [
        {"key": "a", "kind": "percent", "value": "12.50"},
        {"key": "b", "kind": "count", "value": 3},
        {"key": "c", "kind": "money", "value": "1000.00"},
    ]
    period = rules.Period(date(2027, 3, 1), date(2027, 3, 7), date(2027, 3, 8))
    text = "12.5% spent, 3 open, 1000 in total, phase Q1 and 4 more"
    flags = rules.check({"summary": text}, figures, period)
    assert [(f.rule, f.text) for f in flags] == [("stray_number", "4")]


def test_a_negative_figure_quoted_as_written_is_not_flagged():
    figures = [
        {"key": "a", "kind": "percent", "value": "-12.10"},
        {"key": "b", "kind": "count", "value": 4},
    ]
    period = rules.Period(date(2027, 3, 1), date(2027, 3, 7), date(2027, 3, 8))
    text = "Spend is -12.10% behind time; 4 open, not -4, and 3-4 weeks out"
    flags = rules.check({"summary": text}, figures, period)
    assert [(f.rule, f.text) for f in flags] == [("stray_number", "-4"), ("stray_number", "3")]


# --- Who sees what


def test_readers_without_report_publish_get_404_for_a_draft(db, pm, sponsor, sign_in, ids):
    pid = ids["pid"]
    draft = _create(pm[0], pid).json()
    auditor, _ = _client(db, sign_in, "auditor")
    for web in (sponsor[0], auditor):
        assert web.get(_url(pid, f"/{draft['id']}")).status_code == 404
        listed = web.get(_url(pid)).json()["items"]
        assert all(p["reports"] == [] for p in listed)
    assert pm[0].get(_url(pid, f"/{draft['id']}")).status_code == 200
    assert pm[0].get(_url(pid)).json()["items"][1]["reports"][0]["id"] == draft["id"]

    # Published, every reader sees it.
    assert _act(pm[0], pid, draft["id"], "publish", 1).status_code == 200
    assert sponsor[0].get(_url(pid, f"/{draft['id']}")).status_code == 200
    # A sponsor can't write one (403, audited).
    assert _create(sponsor[0], pid, date(2027, 2, 28)).status_code == 403
    assert "no_permission" in _refusals(db)


def test_a_withdrawn_draft_stays_hidden_from_the_sponsor(db, pm, sponsor, ids):
    pid = ids["pid"]
    draft = _create(pm[0], pid).json()
    withdrawn = _act(pm[0], pid, draft["id"], "withdraw", 1, reason="Started too early")
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["withdrawn_reason"] == "Started too early"
    assert sponsor[0].get(_url(pid, f"/{draft['id']}")).status_code == 404
    # The period is free for a new draft.
    assert _create(pm[0], pid).status_code == 201


# --- Corrections


def test_a_correction_withdraws_the_published_report_with_its_reason(db, pm, sponsor, ids):
    pid, web = ids["pid"], pm[0]
    old = _create(web, pid).json()
    assert _act(web, pid, old["id"], "publish", 1).status_code == 200
    assert _create(web, pid).status_code == 409
    assert _create(web, pid, replaces_id=old["id"]).status_code == 422
    correction = _create(web, pid, replaces_id=old["id"], reason="Spend was misread")
    assert correction.status_code == 201, correction.text
    new = correction.json()
    assert new["replaces_id"] == old["id"]
    # The old one stays published until the correction is.
    assert sponsor[0].get(_url(pid, f"/{old['id']}")).json()["status"] == "published"
    assert _create(web, pid, replaces_id=old["id"], reason="Again").status_code == 409

    assert _act(web, pid, new["id"], "publish", 1).status_code == 200
    seen = sponsor[0].get(_url(pid, f"/{old['id']}")).json()
    assert seen["status"] == "withdrawn"
    assert seen["withdrawn_reason"] == "Spend was misread"
    assert seen["replaced_by_id"] == new["id"]
    assert seen["figures"] == old["figures"]
    period = sponsor[0].get(_url(pid)).json()["items"][1]
    assert [r["id"] for r in period["reports"]] == [new["id"], old["id"]]
    assert _refusals(db) == ["already_published", "needs_reason", "draft_exists"]
    publish_row = _rows(db, subject_id=str(new["id"]), status="ok")[-1]
    assert publish_row.action_taken == {
        "action": "published",
        "project_id": pid,
        "withdrew_id": old["id"],
    }


# --- Refusals and the audit trail


def test_refusals_are_409_or_422_and_audited(db, pm, sign_in, ids):
    pid, web = ids["pid"], pm[0]
    assert _create(web, pid, date(2027, 3, 6)).status_code == 422  # not a period end
    assert _create(web, pid, date(2027, 3, 21)).status_code == 422  # not started yet
    rid = _create(web, pid).json()["id"]
    assert _create(web, pid).status_code == 409
    assert _edit(web, pid, rid, 5, summary="Old").status_code == 409
    no_period = _project(db, pm[1], report_period=None)
    assert _create(web, no_period["pid"]).status_code == 409
    assert _refusals(db) == [
        "unknown_period",
        "unknown_period",
        "draft_exists",
        "stale_version",
        "no_report_period",
    ]
    db.execute(update(Project).where(Project.id == pid).values(status="cancelled"))
    db.commit()
    assert _act(web, pid, rid, "refresh", 1).status_code == 409
    assert _refusals(db)[-1] == "cancelled"


def test_without_the_reporting_policy_create_and_refresh_are_503_and_audited(
    db, pm, ids, monkeypatch
):
    pid, web = ids["pid"], pm[0]
    rid = _create(web, pid).json()["id"]

    def missing():
        raise policy_module.PolicyError("reporting.yaml: policy file not found")

    monkeypatch.setattr(policy_module, "get_reporting", missing)
    created = _create(web, pid, date(2027, 2, 28))
    assert created.status_code == 503
    assert "reporting policy" in created.json()["detail"]
    assert _act(web, pid, rid, "refresh", 1).status_code == 503
    rows = _rows(db, status="rejected")
    assert [(r.action_taken["action"], r.action_taken["refusal"]) for r in rows] == [
        ("create_refused", "policy_unavailable"),
        ("refresh_refused", "policy_unavailable"),
    ]
    assert [(r.subject_type, r.subject_id) for r in rows] == [
        ("project", str(pid)),
        ("status_report", str(rid)),
    ]
    # Nothing was written: no new report, and the draft is as it was.
    assert web.get(_url(pid, f"/{rid}")).json()["version"] == 1
    listed = web.get(_url(pid)).json()["items"]
    assert [r["id"] for p in listed for r in p["reports"]] == [rid]


def test_every_write_is_one_audit_row(db, pm, ids):
    pid, web = ids["pid"], pm[0]
    rid = _create(web, pid).json()["id"]
    _edit(web, pid, rid, 1, summary="On track")
    _act(web, pid, rid, "refresh", 2)
    _act(web, pid, rid, "publish", 3)
    _act(web, pid, rid, "withdraw", 4, reason="Wrong project")
    rows = _rows(db, status="ok")
    assert [r.action_taken["action"] for r in rows] == [
        "created",
        "text_edited",
        "refreshed",
        "published",
        "withdrawn",
    ]
    assert [r.version for r in rows] == [1, 2, 3, 4, 5]
    assert all(r.actor_user_id == pm[1].id and r.trigger == "human" for r in rows)
    assert rows[1].output["sections"]["summary"] == "On track"
    assert rows[1].signals["sections"]["summary"] is None


def test_the_reporting_policy_draft_is_valid():
    found = APPLIED_POLICIES if (APPLIED_POLICIES / "reporting.yaml").exists() else DRAFT_POLICIES
    assert load_reporting(found).schedule.red >= Decimal(0)
