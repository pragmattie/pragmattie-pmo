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
- **Figures** (`build_figures`): read from the records as of the period's last day, each with what
  it counts (`note`) and its sources (table, row id and field). A figure with no source row
  (a count of zero) has no sources. Milestones are measured against the current baseline's
  targets, in working days (`app.delivery.days_late`), counting a milestone reached after the
  period as not reached yet. Spend is the project's current figure (its newest entry).
  - Open escalations count only what is still waiting for a decision above the project manager:
    open risks escalated beyond the project manager, and submitted change requests a sponsor
    decides. Change requests waiting counts every submitted change, so one change can be in both.
- **Ratings** (`rate`): green, amber or red for schedule, cost, scope and risk, from the figures
  and the reporting policy file (`app.policy.get_reporting`), each with the rule that set it
  and the measure it used. The overall rating is the worst of the four.
- **Checker** (`check`): flags each money figure, date or plain number in the text that isn't
  one of the report's figures. Money and dates are read as the proposal checker reads them
  (`app.proposals`); a plain number is any other number, a percentage included, and matches when
  it equals a figure's value (`12.5` matches `12.50`). Dates also match the period's start, end
  and due day.
"""

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import delivery
from app.baselines import latest_baseline
from app.models import ChangeRequest, PlanDeliverable, ProjectPlan, ProjectSpend, Risk
from app.models.baseline import ProjectBaseline
from app.models.plan import DELIVERABLE_STATUSES
from app.models.report import SECTIONS
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


def _elapsed_pct(start: date, finish: date | None, on: date) -> Decimal:
    """The share of the time from `start` to `finish` that has passed by `on`, 0 to 100."""
    if finish is None or finish <= start:
        return Decimal(100)
    share = Decimal((on - start).days * 100) / Decimal((finish - start).days)
    return min(Decimal(100), max(Decimal(0), share)).quantize(CENT, rounding=ROUND_HALF_UP)


def build_figures(
    db: Session, project_id: int, plan: ProjectPlan, period: Period, next_: Period
) -> list[dict[str, Any]]:
    """The report's figures for `period`, read from the records now."""
    first = first_baseline(db, project_id)
    current = latest_baseline(db, project_id)
    assert first is not None and current is not None  # a project in delivery has baseline 1
    end = period.end
    figures: list[dict[str, Any]] = []
    add = figures.append

    # Baseline.
    add(
        _figure(
            "baseline_number",
            "Baseline number",
            "number",
            current.number,
            "The number of the current baseline: 1 at the go-ahead, one more per approved change",
            [_src(BASELINES, current.id, "number")],
        )
    )
    add(
        _figure(
            "baseline_total",
            "Baseline total",
            "money",
            _money(current.total),
            "The current baseline's total, contingency included",
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
            "How far the current baseline total is from baseline 1's, either way, as a "
            "percentage of baseline 1's total",
            [_src(BASELINES, first.id, "total"), _src(BASELINES, current.id, "total")],
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
        .where(ProjectSpend.project_id == project_id)
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
            "The project's current spend figure (its newest entry); none until one is recorded",
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
            "Spend to date as a percentage of the current baseline total",
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
            "The share of the time from baseline 1 to the latest milestone target in the current "
            "baseline that had passed by the period's last day",
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
                    "The milestone's target date in the current baseline",
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
                f"Deliverables whose status is {status.replace('_', ' ')} now",
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
                f"Open risks in the risk register whose band is {band}"
                if band != UNSCORED
                else "Open risks in the risk register not yet scored",
                [_src(RISKS, r.id, "band") for r in bands[band]],
            )
        )
    escalated_risks = [r for r in open_risks if r.escalated_to not in (None, PROJECT_MANAGER)]
    waiting = list(
        db.scalars(
            select(ChangeRequest)
            .where(ChangeRequest.project_id == project_id, ChangeRequest.status == "submitted")
            .order_by(ChangeRequest.id)
        )
    )
    escalated_changes = [c for c in waiting if c.decider != PROJECT_MANAGER]
    add(
        _figure(
            "open_escalations",
            "Open escalations",
            "count",
            len(escalated_risks) + len(escalated_changes),
            "Decisions waiting above the project manager: open risks escalated beyond the "
            "project manager, and submitted change requests a sponsor decides",
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
            "Change requests submitted and waiting for a decision, whoever decides them",
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


# --- Checker

_NUMBER = re.compile(r"(?<![\w.,])\d+(?:\.\d+)?(?![\w]|[.,]\d)")


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
        undated = _without_dates(text)
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
