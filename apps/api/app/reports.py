"""What a status report says and how it is rated (item 7.3). Rules in code, no model.

- **Periods** (`periods`, `period_ending`, `next_period`), from the plan's `report_period` and
  `report_due_day`:
  - weekly: due on a weekday; a period is the 7 days before its due day.
  - fortnightly: due every other week on a weekday, counted from the first due day on or after the
    project entered delivery (baseline 1's date), so the periods never drift; a period is the 14
    days before its due day.
  - monthly: a calendar month, due on the due day of the following month (due day 10: October's
    report is due 10 November).
  A project's periods run from the one holding its first day in delivery to the one holding
  today; a report can be made for any of them.
- **Figures** (`build_figures`): each with what it counts (`note`) and its sources (table, row
  id and field). A figure with no source row (a count of zero) has no sources. Figures are as of
  the period's last day where the records allow:
  - the baseline is the newest one approved on or before the period's last day (not the latest
    overall), and milestones are measured against its targets, in working days
    (`app.delivery.days_late`), counting a milestone reached after the period as not reached yet;
  - spend is the newest entry dated on or before the period's last day.
  Figures with no history to read as of a date (deliverables by status, open risks by band, open
  escalations and change requests waiting) are current values: each says so in its note
  ("as of" the day the figures were read), and the report shows that day (`figures_at`).
  - The next period's start, end and due day come from the plan's reporting fields.
  - Open escalations count only what is still waiting for a decision above the project manager:
    open risks escalated beyond the project manager, and submitted change requests a sponsor
    decides. Change requests waiting counts every submitted change, so one change can be in both.
- **Ratings** (`rate`): green, amber or red for schedule, cost, scope and risk, from the figures
  and the reporting policy file (`app.policy.get_reporting`), each with the rule that set it
  and the measure it used. The overall rating is the worst of the four.
- **Checker** (`check`): flags each money figure, date or plain number in the text that isn't
  one of the report's figures. Money and dates are read as the proposal checker reads them
  (`app.proposals`); a plain number is any other number, a percentage included, keeps a leading
  minus sign, and matches when it equals a figure's value (`12.5` matches `12.50`, `-12.10`
  matches a figure of `-12.10` and `-4` doesn't match `4`). Dates also match the period's start, end
  and due day.
"""

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import delivery
from app.audit import next_attempt, record_decision
from app.changes import waiting_changes
from app.models import ChangeRequest, PlanDeliverable, ProjectPlan, ProjectSpend, Risk
from app.models.base import utcnow
from app.models.baseline import ProjectBaseline
from app.models.plan import DELIVERABLE_STATUSES
from app.models.report import DRAFT, SECTIONS, StatusReport
from app.plans import CENT, milestones_of
from app.policy import Limits, ReportingPolicy
from app.proposals import _MONEY, Flag, _amounts, _dates, _without_dates

WEEKLY, FORTNIGHTLY, MONTHLY = "weekly", "fortnightly", "monthly"
PROJECT_MANAGER = "project_manager"
GREEN, AMBER, RED = "green", "amber", "red"
RATING_ORDER = (GREEN, AMBER, RED)
RATED = ("schedule", "cost", "scope", "risk")
UNSCORED = "Unscored"

# Source tables, by their names in the product (a table prefix is a deployment detail).
BASELINES = "project_baselines"
MILESTONES = "plan_milestones"
DELIVERABLES = "plan_deliverables"
SPEND = "project_spend"
PLANS = "project_plans"
CHANGES = "change_requests"
RISKS = "risks"


# --- Periods


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    due_on: date


def _first_weekday_on_or_after(day: date, weekday: int) -> date:
    """The first date on or after `day` that is `weekday` (1 to 7, Monday is 1)."""
    return day + timedelta(days=(weekday - day.isoweekday()) % 7)


def _month_period(year: int, month: int, due_day: int) -> Period:
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    after = end + timedelta(days=1)
    return Period(start, end, date(after.year, after.month, due_day))


def _due_dates(kind: str, due_day: int, delivery_start: date):
    """Every due day whose period holds a day in delivery, oldest first, without end."""
    if kind == MONTHLY:
        year, month = delivery_start.year, delivery_start.month
        while True:
            yield _month_period(year, month, due_day)
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    length = 7 if kind == WEEKLY else 14
    if kind == WEEKLY:
        due = _first_weekday_on_or_after(delivery_start + timedelta(days=1), due_day)
    else:
        due = _first_weekday_on_or_after(delivery_start, due_day)
        if due == delivery_start:  # its period ends the day before delivery started
            due += timedelta(days=length)
    while True:
        yield Period(due - timedelta(days=length), due - timedelta(days=1), due)
        due += timedelta(days=length)


def periods(kind: str, due_day: int, delivery_start: date, today: date) -> list[Period]:
    """The project's periods from the one holding `delivery_start` to the one holding `today`,
    oldest first."""
    found = []
    for period in _due_dates(kind, due_day, delivery_start):
        if period.start > today:
            return found
        found.append(period)
    return found  # pragma: no cover - the generator never ends


def period_ending(
    kind: str, due_day: int, delivery_start: date, today: date, end: date
) -> Period | None:
    """The project's period that ends on `end`, or None when no period of it does."""
    return next((p for p in periods(kind, due_day, delivery_start, today) if p.end == end), None)


def next_period(kind: str, due_day: int, period: Period) -> Period:
    if kind == MONTHLY:
        after = period.end + timedelta(days=1)
        return _month_period(after.year, after.month, due_day)
    length = (period.end - period.start).days + 1
    return Period(
        period.start + timedelta(days=length),
        period.end + timedelta(days=length),
        period.due_on + timedelta(days=length),
    )


# --- Figures


def _src(table: str, row_id: int, field: str) -> dict[str, Any]:
    return {"table": table, "id": row_id, "field": field}


def _money(value: Decimal) -> str:
    return str(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP))


def _figure(
    key: str, label: str, kind: str, value: Any, note: str, sources: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "kind": kind,
        "value": value,
        "note": note,
        "sources": sources,
    }


def first_baseline(db: Session, project_id: int) -> ProjectBaseline | None:
    return db.scalar(
        select(ProjectBaseline).where(
            ProjectBaseline.project_id == project_id, ProjectBaseline.number == 1
        )
    )


def baseline_as_of(db: Session, project_id: int, day: date) -> ProjectBaseline | None:
    """The newest baseline approved on or before `day`."""
    return db.scalar(
        select(ProjectBaseline)
        .where(
            ProjectBaseline.project_id == project_id,
            ProjectBaseline.created_at < datetime.combine(day + timedelta(days=1), time.min),
        )
        .order_by(ProjectBaseline.number.desc())
        .limit(1)
    )


def _elapsed_pct(start: date, finish: date | None, on: date) -> Decimal:
    """The share of the time from `start` to `finish` that has passed by `on`, 0 to 100."""
    if finish is None or finish <= start:
        return Decimal(100)
    share = Decimal((on - start).days * 100) / Decimal((finish - start).days)
    return min(Decimal(100), max(Decimal(0), share)).quantize(CENT, rounding=ROUND_HALF_UP)


def build_figures(
    db: Session, project_id: int, plan: ProjectPlan, period: Period, next_: Period, read_on: date
) -> list[dict[str, Any]]:
    """The report's figures for `period`, read from the records on `read_on`."""
    end = period.end
    first = first_baseline(db, project_id)
    assert first is not None  # a project in delivery has baseline 1
    # Every period ends on or after baseline 1's day, so there is always one; baseline 1 if not.
    current = baseline_as_of(db, project_id, end) or first
    now = f"as of {read_on.isoformat()}, the day the figures were read"
    figures: list[dict[str, Any]] = []
    add = figures.append

    # Baseline.
    add(
        _figure(
            "baseline_number",
            "Baseline number",
            "number",
            current.number,
            "The number of the baseline in force on the period's last day: 1 at the go-ahead, one "
            "more per approved change",
            [_src(BASELINES, current.id, "number")],
        )
    )
    add(
        _figure(
            "baseline_total",
            "Baseline total",
            "money",
            _money(current.total),
            "The total of the baseline in force on the period's last day, contingency included",
            [_src(BASELINES, current.id, "total")],
        )
    )
    add(
        _figure(
            "first_baseline_total",
            "Baseline 1 total",
            "money",
            _money(first.total),
            "The total agreed at the go-ahead, before any change",
            [_src(BASELINES, first.id, "total")],
        )
    )
    scope_pct = (
        None
        if first.total == 0
        else (abs(current.total - first.total) * 100 / first.total).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
    )
    add(
        _figure(
            "scope_moved_pct",
            "Baseline moved since baseline 1 (%)",
            "percent",
            _money(scope_pct) if scope_pct is not None else None,
            "How far the total of the baseline in force on the period's last day is from "
            "baseline 1's, either way, as a percentage of baseline 1's total",
            [_src(BASELINES, first.id, "total"), _src(BASELINES, current.id, "total")],
        )
    )

    # The next period, worked out from the plan's reporting fields (item 7.4), so a person or the
    # agent can name its dates in "planned for next period".
    reporting = [_src(PLANS, plan.id, "report_period"), _src(PLANS, plan.id, "report_due_day")]
    for key, label, day, note in (
        ("next_period_start", "Next period starts", next_.start, "The next period's first day"),
        ("next_period_end", "Next period ends", next_.end, "The next period's last day"),
        ("next_period_due", "Next report due", next_.due_on, "The day the next report is due"),
    ):
        add(
            _figure(
                key,
                label,
                "date",
                day.isoformat(),
                f"{note}, from the plan's reporting period and due day",
                reporting,
            )
        )

    # Approved changes in the period.
    approved = [
        c
        for c in db.scalars(
            select(ChangeRequest)
            .where(ChangeRequest.project_id == project_id, ChangeRequest.status == "approved")
            .order_by(ChangeRequest.id)
        )
        if c.decided_at is not None and period.start <= c.decided_at.date() <= end
    ]
    add(
        _figure(
            "approved_changes",
            "Changes approved this period",
            "count",
            len(approved),
            "Change requests approved between the period's first and last day",
            [_src(CHANGES, c.id, "status") for c in approved],
        )
    )
    add(
        _figure(
            "approved_changes_cost",
            "Cost of changes approved this period",
            "money",
            _money(sum((Decimal(c.effect["cost_difference"]) for c in approved), Decimal(0))),
            "The cost those changes added to the baseline (less what they took off)",
            [_src(CHANGES, c.id, "effect.cost_difference") for c in approved],
        )
    )

    # Spend.
    spend = db.scalar(
        select(ProjectSpend)
        .where(ProjectSpend.project_id == project_id, ProjectSpend.as_of <= end)
        .order_by(ProjectSpend.id.desc())
        .limit(1)
    )
    spend_sources = [_src(SPEND, spend.id, "amount")] if spend else []
    add(
        _figure(
            "spend_to_date",
            "Spend to date",
            "money",
            _money(spend.amount) if spend else None,
            "The project's newest spend entry dated on or before the period's last day; none "
            "until one is recorded",
            spend_sources,
        )
    )
    add(
        _figure(
            "spend_as_of",
            "Spend as of",
            "date",
            spend.as_of.isoformat() if spend else None,
            "The date the spend figure is true for",
            [_src(SPEND, spend.id, "as_of")] if spend else [],
        )
    )
    spent_pct = (
        (Decimal(spend.amount) * 100 / current.total).quantize(CENT, rounding=ROUND_HALF_UP)
        if spend and current.total
        else None
    )
    add(
        _figure(
            "spent_pct",
            "Baseline spent (%)",
            "percent",
            _money(spent_pct) if spent_pct is not None else None,
            "Spend to date as a percentage of the total of the baseline in force on the period's "
            "last day",
            [*spend_sources, _src(BASELINES, current.id, "total")] if spend else [],
        )
    )
    targets = delivery.baseline_targets(current)
    finish = max(targets.values(), default=None)
    finish_id = next((mid for mid, day in targets.items() if day == finish), None)
    started = first.created_at.date()
    elapsed = _elapsed_pct(started, finish, end)
    add(
        _figure(
            "time_elapsed_pct",
            "Delivery time passed (%)",
            "percent",
            _money(elapsed),
            "The share of the time from baseline 1 to the latest milestone target in the "
            "baseline in force that had passed by the period's last day",
            [_src(BASELINES, first.id, "created_at")]
            + (
                [_src(BASELINES, current.id, f"plan.milestones[{finish_id}].target_date")]
                if finish_id is not None
                else []
            ),
        )
    )
    ahead = spent_pct - elapsed if spent_pct is not None else None
    add(
        _figure(
            "spend_ahead_points",
            "Spend ahead of time (points)",
            "percent",
            _money(ahead) if ahead is not None else None,
            "Baseline spent (%) less delivery time passed (%); below zero, spending is behind time",
            [
                *spend_sources,
                _src(BASELINES, current.id, "total"),
                _src(BASELINES, first.id, "created_at"),
            ]
            if ahead is not None
            else [],
        )
    )

    # Milestones.
    milestones = milestones_of(db, plan.id)
    reached, late, due_next = [], [], []
    worst: tuple[int, Any] | None = None
    for m in milestones:
        target = targets.get(m.id)
        reached_on = m.reached_on if m.reached_on and m.reached_on <= end else None
        days = delivery.days_late(target, reached_on, end)
        target_src = _src(BASELINES, current.id, f"plan.milestones[{m.id}].target_date")
        if target is not None:
            add(
                _figure(
                    f"milestone_{m.id}_target",
                    f"Target: {m.name}",
                    "date",
                    target.isoformat(),
                    "The milestone's target date in the baseline in force on the period's last day",
                    [target_src],
                )
            )
        if m.target_date != target:
            add(
                _figure(
                    f"milestone_{m.id}_plan_date",
                    f"Plan date: {m.name}",
                    "date",
                    m.target_date.isoformat(),
                    "The milestone's date in the plan now",
                    [_src(MILESTONES, m.id, "target_date")],
                )
            )
        if reached_on is not None:
            reached.append(m)
            add(
                _figure(
                    f"milestone_{m.id}_reached",
                    f"Reached: {m.name}",
                    "date",
                    reached_on.isoformat(),
                    "The day the milestone was reached",
                    [_src(MILESTONES, m.id, "reached_on")],
                )
            )
        if days is not None and days > 0:
            late.append(m)
            sources = [target_src, _src(MILESTONES, m.id, "reached_on")]
            add(
                _figure(
                    f"milestone_{m.id}_days_late",
                    f"Days late: {m.name}",
                    "days",
                    days,
                    "Working days from the baseline target to the day it was reached, or to the "
                    "period's last day if it wasn't reached by then",
                    sources,
                )
            )
            if worst is None or days > worst[0]:
                worst = (days, sources)
        if reached_on is None and next_.start <= m.target_date <= next_.end:
            due_next.append(m)
    add(
        _figure(
            "milestones_reached",
            "Milestones reached",
            "count",
            len(reached),
            "Milestones reached by the period's last day",
            [_src(MILESTONES, m.id, "reached_on") for m in reached],
        )
    )
    add(
        _figure(
            "milestones_late",
            "Milestones late",
            "count",
            len(late),
            "Milestones more than 0 working days past their baseline target by the period's "
            "last day, reached or not",
            [_src(MILESTONES, m.id, "reached_on") for m in late],
        )
    )
    add(
        _figure(
            "most_days_late",
            "Most working days late",
            "days",
            worst[0] if worst else 0,
            "The most working days any milestone is late against its baseline target",
            worst[1] if worst else [],
        )
    )
    add(
        _figure(
            "milestones_due_next",
            "Milestones due next period",
            "count",
            len(due_next),
            f"Milestones not yet reached whose plan date falls from {next_.start.isoformat()} "
            f"to {next_.end.isoformat()}",
            [_src(MILESTONES, m.id, "target_date") for m in due_next],
        )
    )

    # Deliverables by status.
    deliverables = list(
        db.scalars(
            select(PlanDeliverable)
            .where(PlanDeliverable.plan_id == plan.id)
            .order_by(PlanDeliverable.id)
        )
    )
    for status in DELIVERABLE_STATUSES:
        rows = [d for d in deliverables if d.status == status]
        add(
            _figure(
                f"deliverables_{status}",
                f"Deliverables {status.replace('_', ' ')}",
                "count",
                len(rows),
                f"Deliverables whose status is {status.replace('_', ' ')}, {now}",
                [_src(DELIVERABLES, d.id, "status") for d in rows],
            )
        )

    # Risks, escalations and waiting changes.
    open_risks = list(
        db.scalars(
            select(Risk)
            .where(Risk.project_id == project_id, Risk.status == "open")
            .order_by(Risk.id)
        )
    )
    bands: dict[str, list[Risk]] = {}
    for risk in open_risks:
        bands.setdefault(risk.band or UNSCORED, []).append(risk)
    for band in sorted(bands):
        add(
            _figure(
                f"open_risks_{band.lower()}",
                f"Open risks: {band}",
                "count",
                len(bands[band]),
                f"Open risks in the risk register whose band is {band}, {now}"
                if band != UNSCORED
                else f"Open risks in the risk register not yet scored, {now}",
                [_src(RISKS, r.id, "band") for r in bands[band]],
            )
        )
    escalated_risks = [r for r in open_risks if r.escalated_to not in (None, PROJECT_MANAGER)]
    waiting = waiting_changes(db, project_id)
    escalated_changes = [c for c in waiting if c.decider != PROJECT_MANAGER]
    add(
        _figure(
            "open_escalations",
            "Open escalations",
            "count",
            len(escalated_risks) + len(escalated_changes),
            "Decisions waiting above the project manager: open risks escalated beyond the "
            f"project manager, and submitted change requests a sponsor decides; {now}",
            [_src(RISKS, r.id, "escalated_to") for r in escalated_risks]
            + [_src(CHANGES, c.id, "decider") for c in escalated_changes],
        )
    )
    add(
        _figure(
            "changes_waiting",
            "Change requests waiting",
            "count",
            len(waiting),
            f"Change requests submitted and waiting for a decision, whoever decides them; {now}",
            [_src(CHANGES, c.id, "status") for c in waiting],
        )
    )
    return figures


def figure_value(figures: list[dict[str, Any]], key: str) -> Any:
    return next((f["value"] for f in figures if f["key"] == key), None)


# --- Ratings


def _measured(name: str, measure: Decimal | None, limits: Limits, keys: tuple[str, str], what: str):
    """Rate one measure against its amber and red limits."""
    amber_key, red_key = keys
    if measure is None:
        return {
            "rating": AMBER,
            "measure": None,
            "rule": f"{name}.not_measurable",
            "text": f"{what} can't be measured from the records, so it can't be shown as green",
        }
    for rating, key, limit in ((RED, red_key, limits.red), (AMBER, amber_key, limits.amber)):
        if measure > limit:
            return {
                "rating": rating,
                "measure": str(measure),
                "rule": f"{name}.{key}",
                "text": f"{what} is {measure}, above the {rating} limit of {limit}",
            }
    return {
        "rating": GREEN,
        "measure": str(measure),
        "rule": f"{name}.{amber_key}",
        "text": f"{what} is {measure}, within the amber limit of {limits.amber}",
    }


def _risk_rating(figures: list[dict[str, Any]], policy: ReportingPolicy) -> dict[str, Any]:
    counts = {
        f["label"].removeprefix("Open risks: "): f["value"]
        for f in figures
        if f["key"].startswith("open_risks_")
    }
    for rating, key, limits in (
        (RED, "red_at", policy.risk_red_at),
        (AMBER, "amber_at", policy.risk_amber_at),
    ):
        for band, limit in limits.items():
            if counts.get(band, 0) >= limit:
                return {
                    "rating": rating,
                    "measure": str(counts[band]),
                    "rule": f"risk.{key}.{band}",
                    "text": f"{counts[band]} open {band} risks, at or above the {rating} count "
                    f"of {limit}",
                }
    named = ", ".join(f"{b} {n}" for b, n in policy.risk_amber_at.items())
    return {
        "rating": GREEN,
        "measure": None,
        "rule": "risk.amber_at",
        "text": f"open risks are below every amber count ({named})",
    }


def rate(figures: list[dict[str, Any]], policy: ReportingPolicy) -> dict[str, dict[str, Any]]:
    """Each rating with its rule, and the overall rating: the worst of the four."""

    def number(key: str) -> Decimal | None:
        value = figure_value(figures, key)
        return None if value is None else Decimal(str(value))

    spend = figure_value(figures, "spend_to_date")
    cost = (
        {
            "rating": GREEN,
            "measure": None,
            "rule": "cost.no_spend",
            "text": "no spend recorded yet, so nothing is spent ahead of time",
        }
        if spend is None
        else _measured(
            "cost",
            number("spend_ahead_points"),
            policy.cost,
            ("amber_above_points", "red_above_points"),
            "Spend ahead of time (points)",
        )
    )
    ratings = {
        "schedule": _measured(
            "schedule",
            number("most_days_late"),
            policy.schedule,
            ("amber_above_working_days", "red_above_working_days"),
            "The most working days a milestone is late",
        ),
        "cost": cost,
        "scope": _measured(
            "scope",
            number("scope_moved_pct"),
            policy.scope,
            ("amber_above_pct", "red_above_pct"),
            "The baseline's move since baseline 1 (%)",
        ),
        "risk": _risk_rating(figures, policy),
    }
    worst = max(RATED, key=lambda name: RATING_ORDER.index(ratings[name]["rating"]))
    ratings["overall"] = {
        "rating": ratings[worst]["rating"],
        "measure": None,
        "rule": "overall.worst_of_four",
        "text": f"the worst of schedule, cost, scope and risk ({worst} is "
        f"{ratings[worst]['rating']})",
    }
    return ratings


# --- Making and recording a report


AUDIT_AGENT = "status_report"
SUBJECT = "status_report"


def period_of(report: StatusReport) -> Period:
    return Period(report.period_start, report.period_end, report.due_on)


def run_check(report: StatusReport) -> list[dict[str, str]]:
    """Run the checker on the report's text and keep its flags on the report."""
    report.flags = [f.as_dict() for f in check(report.sections, report.figures, period_of(report))]
    return report.flags


def fill(
    db: Session, report: StatusReport, plan: ProjectPlan, period: Period, policy: ReportingPolicy
) -> None:
    """Read the figures from the records, rate them and run the checker."""
    next_ = next_period(plan.report_period, plan.report_due_day, period)
    report.figures_at = utcnow()
    report.figures = build_figures(
        db, report.project_id, plan, period, next_, report.figures_at.date()
    )
    report.ratings = rate(report.figures, policy)
    run_check(report)


def new_draft(
    db: Session,
    project_id: int,
    plan: ProjectPlan,
    period: Period,
    policy: ReportingPolicy,
    *,
    created_by: int | None,
    replaces_id: int | None = None,
    replace_reason: str | None = None,
) -> StatusReport:
    """A new draft for `period`, with its figures, ratings and empty text, flushed. `created_by`
    is None when the orchestrator makes it (item 7.4)."""
    report = StatusReport(
        project_id=project_id,
        period_start=period.start,
        period_end=period.end,
        due_on=period.due_on,
        status=DRAFT,
        sections=dict.fromkeys(SECTIONS),
        section_sources={},
        replaces_id=replaces_id,
        replace_reason=replace_reason,
        created_by=created_by,
    )
    fill(db, report, plan, period, policy)
    db.add(report)
    db.flush()
    return report


def snapshot(report: StatusReport) -> dict[str, Any]:
    return {
        "status": report.status,
        "period_start": report.period_start.isoformat(),
        "period_end": report.period_end.isoformat(),
        "figures": report.figures,
        "ratings": {name: r["rating"] for name, r in report.ratings.items()},
        "sections": report.sections,
        "section_sources": report.section_sources,
        "flags": report.flags,
        "replaces_id": report.replaces_id,
        "replace_reason": report.replace_reason,
        "withdrawn_reason": report.withdrawn_reason,
    }


def record(
    db: Session,
    report: StatusReport,
    *,
    actor_user_id: int | None,
    before: dict[str, Any] | None,
    action: str,
    trigger: str = "human",
    **extra: Any,
) -> None:
    """One audit row for a change already flushed to `report` (so its version is the new one)."""
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger=trigger,
        subject_type=SUBJECT,
        subject_id=str(report.id),
        version=report.version,
        attempt=next_attempt(
            db,
            agent=AUDIT_AGENT,
            subject_type=SUBJECT,
            subject_id=str(report.id),
            version=report.version,
        ),
        status="ok",
        actor_user_id=actor_user_id,
        signals=before,
        output=snapshot(report),
        action_taken={"action": action, "project_id": report.project_id, **extra},
    )


# --- Who wrote each section (item 7.4)

AGENT_SOURCE, PERSON_SOURCE = "agent", "person"


def _source(report: StatusReport, name: str) -> dict[str, Any]:
    return (report.section_sources or {}).get(name) or {}


def is_unreviewed(report: StatusReport, name: str) -> bool:
    """Whether the section is written and is an agent's draft no person has edited or confirmed."""
    entry = _source(report, name)
    written = bool(((report.sections or {}).get(name) or "").strip())
    return written and entry.get("source") == AGENT_SOURCE and entry.get("reviewed_by") is None


def unreviewed_sections(report: StatusReport) -> list[str]:
    return [name for name in SECTIONS if is_unreviewed(report, name)]


def empty_sections(report: StatusReport) -> list[str]:
    return [name for name in SECTIONS if not ((report.sections or {}).get(name) or "").strip()]


def mark_section(
    report: StatusReport,
    name: str,
    *,
    source: str,
    reviewed_by: int | None,
    reviewed_at: str | None,
) -> None:
    """Record who wrote, or who reviewed, a section. The dict is replaced, so it is saved."""
    sources = dict(report.section_sources or {})
    sources[name] = {"source": source, "reviewed_by": reviewed_by, "reviewed_at": reviewed_at}
    report.section_sources = sources


# --- Checker

# A leading minus sign belongs to the number, so a negative figure quoted as written matches.
_NUMBER = re.compile(r"(?:(?<![\w.,])-)?(?<![\w.,])\d+(?:\.\d+)?(?![\w]|[.,]\d)")
# A currency sign, perhaps with one space, right before a number.
_CURRENCY_BEFORE = re.compile(r"[$£€]\s?$")


def _without_money(text: str) -> str:
    """`text` with what the proposal checker reads as money blanked, so its digits aren't read
    again as plain numbers."""

    def blank(match: re.Match) -> str:
        number = match.group("number")
        if (
            match.group("sign")
            or match.group("scale")
            or "," in number
            or re.search(r"\.\d{2}$", number)
        ):
            return " " * len(match.group(0))
        return match.group(0)

    return _MONEY.sub(blank, text)


def _without_known_numbers(text: str, numbers: set[Decimal]) -> str:
    """`text` with each plain number that is one of the report's figures as written (its minus
    sign included) blanked, whatever the figure's kind, so a percent or points figure quoted
    without its unit isn't read as stray money. A number after a currency sign is left to the
    money check."""

    def blank(match: re.Match) -> str:
        if _CURRENCY_BEFORE.search(text, 0, match.start()):
            return match.group(0)
        if Decimal(match.group(0)) in numbers:
            return " " * len(match.group(0))
        return match.group(0)

    return _NUMBER.sub(blank, text)


def check(
    sections: dict[str, str | None], figures: list[dict[str, Any]], period: Period
) -> list[Flag]:
    """One flag for each money figure, date or plain number in the text that isn't one of the
    report's figures, in section order."""
    money: set[Decimal] = set()
    numbers: set[Decimal] = set()
    dates = {period.start, period.end, period.due_on}
    for figure in figures:
        value = figure["value"]
        if value is None:
            continue
        if figure["kind"] == "date":
            dates.add(date.fromisoformat(value))
            continue
        if figure["kind"] == "money":
            money.add(Decimal(value).quantize(CENT))
        numbers.add(Decimal(str(value)))
    flags: list[Flag] = []
    for section in SECTIONS:
        text = sections.get(section) or ""
        undated = _without_known_numbers(_without_dates(text), numbers)
        for written, amount in _amounts(undated):
            if amount not in money:
                flags.append(Flag("stray_money", section, written))
        for written, day in _dates(text):
            if day not in dates:
                flags.append(Flag("stray_date", section, written))
        for match in _NUMBER.finditer(_without_money(undated)):
            if Decimal(match.group(0)) not in numbers:
                flags.append(Flag("stray_number", section, match.group(0)))
    return flags


# --- The executive view (item 7.5): the essentials on one page, from the frozen figures only

EXECUTIVE_MILESTONES = 3
HIGH_BAND = "High"
SPEND_KEYS = (
    "spend_to_date",
    "spend_as_of",
    "baseline_total",
    "spent_pct",
    "time_elapsed_pct",
    "spend_ahead_points",
)
_MILESTONE_KEY = re.compile(r"^milestone_(\d+)_(target|plan_date|reached|days_late)$")


def report_milestones(figures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every milestone the figures name, by id, as the report froze it: its name, target in the
    baseline, plan date (the target when the report has none apart), reached day and working
    days late, and whether it was due in the next period. Nothing is read from the records."""
    found: dict[int, dict[str, Any]] = {}
    for figure in figures:
        match = _MILESTONE_KEY.match(figure["key"])
        if match is None:
            continue
        milestone_id, what = int(match.group(1)), match.group(2)
        entry = found.setdefault(
            milestone_id,
            {
                "id": milestone_id,
                "name": figure["label"].split(": ", 1)[-1],
                "target": None,
                "plan_date": None,
                "reached_on": None,
                "days_late": 0,
                "due_next": False,
            },
        )
        key = {"reached": "reached_on"}.get(what, what)
        entry[key] = figure["value"]
    due_next = next((f for f in figures if f["key"] == "milestones_due_next"), None)
    for source in due_next["sources"] if due_next else []:
        if source["table"] == MILESTONES and source["id"] in found:
            found[source["id"]]["due_next"] = True
    for entry in found.values():
        entry["plan_date"] = entry["plan_date"] or entry["target"]
    return [found[k] for k in sorted(found)]


def top_milestones(figures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The milestones that matter most, at most three: the late ones, most days late first, then
    the ones due in the next period, by plan date."""
    every = report_milestones(figures)
    late = sorted(
        (m for m in every if m["days_late"] > 0), key=lambda m: (-m["days_late"], m["id"])
    )
    due = sorted(
        (m for m in every if m["due_next"] and m["days_late"] <= 0),
        key=lambda m: (m["plan_date"] or "", m["id"]),
    )
    return (late + due)[:EXECUTIVE_MILESTONES]


def executive(report: StatusReport) -> dict[str, Any]:
    """A published report's one-page view, from what it froze when published."""
    figures = report.figures
    milestones = top_milestones(figures)
    high = next((f for f in figures if f["key"] == f"open_risks_{HIGH_BAND.lower()}"), None)
    return {
        "ratings": report.ratings,
        "summary": (report.sections or {}).get("summary"),
        "decisions": (report.sections or {}).get("decisions"),
        "milestones": milestones,
        "milestones_late": figure_value(figures, "milestones_late") or 0,
        "milestones_due_next": figure_value(figures, "milestones_due_next") or 0,
        "spend": {key: figure_value(figures, key) for key in SPEND_KEYS},
        "high_risks": high["value"] if high else 0,
        "high_risk_ids": [s["id"] for s in high["sources"]] if high else [],
    }
