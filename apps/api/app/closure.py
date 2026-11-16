"""What a closure report says (item 7.7). Rules in code, no model.

- **Figures** (`build_figures`), each with what it counts (`note`) and its sources (table, row id
  and field), as a status report's (`app.reports`):
  - baseline 1 (its total and the day it was agreed) and the final baseline (its number and
    total), and how far the total moved between them;
  - every approved change request, oldest first: the cost it added (less what it took off), the
    day it was decided and the baseline it made, and their count and total;
  - final spend (the newest spend entry) against the final baseline: the amount, its date, the
    difference (below zero is under the baseline) and the share spent;
  - each milestone against its baseline 1 target (not a later one): the target, the day it was
    reached and the working days late (`app.delivery.days_late`), and how many were reached and
    how many on or before their baseline 1 target;
  - deliverables in the final baseline, and how many of them are accepted;
  - accepted risks still standing: an accepted risk is a decision the right person took, so it
    doesn't hold back close-out, but the report names each one for the sponsor.
- **Benefits** (`benefits`): each benefit of the value case of the request the project came from,
  with its measure, the value before, the target and its date, and, as measured at close, the
  value now and its date (a person enters both) or the owner and date for measuring it later.
- **Sections**: what was delivered, how it compares with the value case, and what remains. Who
  wrote each is kept as on a status report (`app.reports.mark_section`), and the checker
  (`app.reports.check`) flags any number in the text that isn't a figure.
- **Freezing** (`freeze`): passing the close-out gate stamps `frozen_at`, and the figures, text
  and benefits as they were then are what the closed project shows.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import delivery
from app import reports as rules
from app.audit import next_attempt, record_decision
from app.baselines import latest_baseline
from app.models import (
    ChangeRequest,
    PlanDeliverable,
    Project,
    ProjectPlan,
    ProjectRequest,
    ProjectSpend,
    Risk,
    ValueCase,
    ValueCaseBenefit,
)
from app.models.base import utcnow
from app.models.baseline import ProjectBaseline
from app.models.closure import (
    CLOSURE_SECTIONS,
    RECORDED,
    ClosureBenefit,
    ClosureReport,
    Lesson,
)
from app.models.plan import ACCEPTED
from app.models.portfolio import CANCELLED
from app.plans import CENT, milestones_of

CLOSURE = "closure"
CLOSED = "closed"
AUDIT_AGENT = "closure"
SUBJECT = "closure_report"
BENEFIT_SUBJECT = "closure_benefit"
LESSON_SUBJECT = "lesson"

_src, _figure, _money = rules._src, rules._figure, rules._money

CANCELLED_PROJECT = "this project is cancelled and takes no further changes"
NOT_IN_CLOSURE = "a closure report is made and changed while the project is in closure"
CLOSED_PROJECT = "this project is closed: its closure report, benefits and lessons are final"
LESSONS_NOT_OPEN = "lessons are recorded while the project is in delivery or closure"


def refusal(project: Project) -> tuple[str, str] | None:
    """Why the project's closure report and benefits take no write now, or None."""
    if project.status == CANCELLED:
        return "cancelled", CANCELLED_PROJECT
    if project.stage == CLOSED:
        return "project_closed", CLOSED_PROJECT
    if project.stage != CLOSURE:
        return "not_in_closure", NOT_IN_CLOSURE
    return None


def lesson_refusal(project: Project) -> tuple[str, str] | None:
    """Why the project's lessons take no write now, or None: open in delivery and closure."""
    if project.status == CANCELLED:
        return "cancelled", CANCELLED_PROJECT
    if project.stage == CLOSED:
        return "project_closed", CLOSED_PROJECT
    if project.stage not in (delivery.DELIVERY, CLOSURE):
        return "lessons_not_open", LESSONS_NOT_OPEN
    return None


# --- Figures


def approved_changes(db: Session, project_id: int) -> list[ChangeRequest]:
    return list(
        db.scalars(
            select(ChangeRequest)
            .where(ChangeRequest.project_id == project_id, ChangeRequest.status == "approved")
            .order_by(ChangeRequest.id)
        )
    )


def accepted_risks(db: Session, project_id: int) -> list[Risk]:
    return list(
        db.scalars(
            select(Risk)
            .where(Risk.project_id == project_id, Risk.status == "accepted")
            .order_by(Risk.id)
        )
    )


def build_figures(db: Session, project: Project, read_on: date) -> list[dict[str, Any]]:
    """The closure report's figures, read from the records on `read_on`."""
    first = rules.first_baseline(db, project.id)
    final = latest_baseline(db, project.id)
    assert first is not None and final is not None  # a project in closure has baseline 1
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    figures: list[dict[str, Any]] = []
    add = figures.append
    B = rules.BASELINES

    # Baselines.
    add(
        _figure(
            "first_baseline_total",
            "Baseline 1 total",
            "money",
            _money(first.total),
            "The total agreed at the go-ahead, before any change, contingency included",
            [_src(B, first.id, "total")],
        )
    )
    add(
        _figure(
            "first_baseline_date",
            "Baseline 1 agreed",
            "date",
            first.created_at.date().isoformat(),
            "The day baseline 1 was agreed at the go-ahead",
            [_src(B, first.id, "created_at")],
        )
    )
    add(
        _figure(
            "final_baseline_number",
            "Final baseline number",
            "number",
            final.number,
            "The number of the last baseline: 1 at the go-ahead, one more per approved change",
            [_src(B, final.id, "number")],
        )
    )
    add(
        _figure(
            "final_baseline_total",
            "Final baseline total",
            "money",
            _money(final.total),
            "The total of the last baseline, contingency included",
            [_src(B, final.id, "total")],
        )
    )
    add(
        _figure(
            "baseline_moved",
            "Final baseline less baseline 1",
            "money",
            _money(final.total - first.total),
            "How much the approved changes moved the total from baseline 1 (below zero, down)",
            [_src(B, first.id, "total"), _src(B, final.id, "total")],
        )
    )

    # Every approved change.
    changes = approved_changes(db, project.id)
    made = {
        b.change_request_id: b
        for b in db.scalars(
            select(ProjectBaseline).where(
                ProjectBaseline.project_id == project.id,
                ProjectBaseline.change_request_id.is_not(None),
            )
        )
    }
    for change in changes:
        label = f"Change {change.id}: {change.title}"
        add(
            _figure(
                f"change_{change.id}_cost",
                f"{label} (cost)",
                "money",
                _money(Decimal(change.effect["cost_difference"])),
                "The cost the change added to the baseline (less what it took off)",
                [_src(rules.CHANGES, change.id, "effect.cost_difference")],
            )
        )
        if change.decided_at is not None:
            add(
                _figure(
                    f"change_{change.id}_decided",
                    f"{label} (approved)",
                    "date",
                    change.decided_at.date().isoformat(),
                    "The day the change was approved",
                    [_src(rules.CHANGES, change.id, "decided_at")],
                )
            )
        baseline = made.get(change.id)
        if baseline is not None:
            add(
                _figure(
                    f"change_{change.id}_baseline",
                    f"{label} (baseline)",
                    "number",
                    baseline.number,
                    "The number of the baseline the change made",
                    [_src(B, baseline.id, "number")],
                )
            )
    add(
        _figure(
            "approved_changes",
            "Changes approved",
            "count",
            len(changes),
            "Change requests approved over the project's life",
            [_src(rules.CHANGES, c.id, "status") for c in changes],
        )
    )
    add(
        _figure(
            "approved_changes_cost",
            "Cost of the changes approved",
            "money",
            _money(sum((Decimal(c.effect["cost_difference"]) for c in changes), Decimal(0))),
            "The cost those changes added to the baseline (less what they took off)",
            [_src(rules.CHANGES, c.id, "effect.cost_difference") for c in changes],
        )
    )

    # Final spend against the final baseline.
    spend = db.scalar(
        select(ProjectSpend)
        .where(ProjectSpend.project_id == project.id)
        .order_by(ProjectSpend.id.desc())
        .limit(1)
    )
    spend_src = [_src(rules.SPEND, spend.id, "amount")] if spend else []
    final_src = _src(B, final.id, "total")
    add(
        _figure(
            "final_spend",
            "Final spend",
            "money",
            _money(spend.amount) if spend else None,
            "The project's newest spend entry; none if no spend was recorded",
            spend_src,
        )
    )
    add(
        _figure(
            "final_spend_as_of",
            "Final spend as of",
            "date",
            spend.as_of.isoformat() if spend else None,
            "The date the final spend figure is true for",
            [_src(rules.SPEND, spend.id, "as_of")] if spend else [],
        )
    )
    add(
        _figure(
            "spend_less_final_baseline",
            "Final spend less the final baseline",
            "money",
            _money(Decimal(spend.amount) - final.total) if spend else None,
            "Final spend less the final baseline's total; below zero, under the baseline",
            [*spend_src, final_src] if spend else [],
        )
    )
    spent_pct = (
        (Decimal(spend.amount) * 100 / final.total).quantize(CENT, rounding=ROUND_HALF_UP)
        if spend and final.total
        else None
    )
    add(
        _figure(
            "final_spent_pct",
            "Final baseline spent (%)",
            "percent",
            _money(spent_pct) if spent_pct is not None else None,
            "Final spend as a percentage of the final baseline's total",
            [*spend_src, final_src] if spent_pct is not None else [],
        )
    )

    # Milestones against their baseline 1 targets.
    targets = delivery.baseline_targets(first)
    milestones = milestones_of(db, plan.id) if plan is not None else []
    reached, on_target = [], []
    for m in milestones:
        target = targets.get(m.id)
        target_src = _src(B, first.id, f"plan.milestones[{m.id}].target_date")
        if target is not None:
            add(
                _figure(
                    f"milestone_{m.id}_target",
                    f"Baseline 1 target: {m.name}",
                    "date",
                    target.isoformat(),
                    "The milestone's target date in baseline 1",
                    [target_src],
                )
            )
        if m.reached_on is not None:
            reached.append(m)
            add(
                _figure(
                    f"milestone_{m.id}_reached",
                    f"Reached: {m.name}",
                    "date",
                    m.reached_on.isoformat(),
                    "The day the milestone was reached",
                    [_src(rules.MILESTONES, m.id, "reached_on")],
                )
            )
        days = delivery.days_late(target, m.reached_on, read_on)
        if days is not None:
            if m.reached_on is not None and days <= 0:
                on_target.append(m)
            add(
                _figure(
                    f"milestone_{m.id}_days_late",
                    f"Working days late against baseline 1: {m.name}",
                    "days",
                    days,
                    "Working days from the baseline 1 target to the day it was reached (below "
                    "zero, early), or to the day the figures were read if it wasn't reached",
                    [target_src, _src(rules.MILESTONES, m.id, "reached_on")],
                )
            )
    add(
        _figure(
            "milestones_total",
            "Milestones",
            "count",
            len(milestones),
            "Milestones in the plan",
            [_src(rules.MILESTONES, m.id, "name") for m in milestones],
        )
    )
    add(
        _figure(
            "milestones_reached",
            "Milestones reached",
            "count",
            len(reached),
            "Milestones reached",
            [_src(rules.MILESTONES, m.id, "reached_on") for m in reached],
        )
    )
    add(
        _figure(
            "milestones_on_target",
            "Milestones reached by their baseline 1 target",
            "count",
            len(on_target),
            "Milestones reached on or before their target in baseline 1",
            [_src(rules.MILESTONES, m.id, "reached_on") for m in on_target],
        )
    )

    # Deliverables in the final baseline, and how many are accepted.
    agreed = final.plan.get("deliverables") or []
    rows = (
        {
            d.id: d
            for d in db.scalars(select(PlanDeliverable).where(PlanDeliverable.plan_id == plan.id))
        }
        if plan is not None
        else {}
    )
    accepted = [
        rows[d["id"]] for d in agreed if d["id"] in rows and rows[d["id"]].status == ACCEPTED
    ]
    add(
        _figure(
            "deliverables_in_baseline",
            "Deliverables in the final baseline",
            "count",
            len(agreed),
            "Deliverables the final baseline lists",
            [_src(B, final.id, f"plan.deliverables[{d['id']}]") for d in agreed],
        )
    )
    add(
        _figure(
            "deliverables_accepted",
            "Deliverables accepted",
            "count",
            len(accepted),
            "Deliverables in the final baseline whose status is accepted",
            [_src(rules.DELIVERABLES, d.id, "status") for d in accepted],
        )
    )

    # Accepted risks still standing.
    standing = accepted_risks(db, project.id)
    add(
        _figure(
            "accepted_risks",
            "Accepted risks still standing",
            "count",
            len(standing),
            "Risks accepted by the person their band names and not closed: they don't hold back "
            "close-out, and the sponsor sees each one",
            [_src(rules.RISKS, r.id, "status") for r in standing],
        )
    )
    return figures


# --- Benefits


def value_case_benefits(db: Session, project_id: int) -> list[ValueCaseBenefit]:
    """The benefits of the value case of the request the project came from, in order; none for a
    project that didn't come from a request."""
    return list(
        db.scalars(
            select(ValueCaseBenefit)
            .join(ValueCase, ValueCase.id == ValueCaseBenefit.value_case_id)
            .join(ProjectRequest, ProjectRequest.id == ValueCase.request_id)
            .where(ProjectRequest.project_id == project_id)
            .order_by(ValueCaseBenefit.position, ValueCaseBenefit.id)
        )
    )


def closure_benefits(db: Session, project_id: int) -> dict[int, ClosureBenefit]:
    """Each benefit as measured at close, by the value case benefit's id."""
    return {
        b.benefit_id: b
        for b in db.scalars(select(ClosureBenefit).where(ClosureBenefit.project_id == project_id))
    }


def uses_ai(db: Session, project_id: int) -> bool:
    """Whether the project uses AI, as the request it came from says."""
    return bool(
        db.scalar(select(ProjectRequest.uses_ai).where(ProjectRequest.project_id == project_id))
    )


def benefit_gaps(db: Session, project_id: int) -> list[str]:
    """Each benefit with neither a value now nor an owner and date to measure it, by measure."""
    measured = closure_benefits(db, project_id)
    gaps = []
    for benefit in value_case_benefits(db, project_id):
        row = measured.get(benefit.id)
        has_value = row is not None and row.value_now is not None
        owned = row is not None and row.measure_owner_id is not None and row.measure_by is not None
        if not (has_value or owned):
            gaps.append(
                f"benefit {benefit.position + 1} '{benefit.measure or benefit.description}'"
            )
    return gaps


def benefits_snapshot(db: Session, project_id: int) -> list[dict[str, Any]]:
    """Each benefit as the closure report shows it, as plain data."""
    measured = closure_benefits(db, project_id)
    found = []
    for benefit in value_case_benefits(db, project_id):
        row = measured.get(benefit.id)
        found.append(
            {
                "benefit_id": benefit.id,
                "position": benefit.position,
                "description": benefit.description,
                "measure": benefit.measure,
                "value_before": benefit.current_value,
                "target": benefit.target,
                "target_date": benefit.target_date.isoformat() if benefit.target_date else None,
                "value_now": row.value_now if row else None,
                "value_now_on": row.value_now_on.isoformat() if row and row.value_now_on else None,
                "measure_owner_id": row.measure_owner_id if row else None,
                "measure_by": row.measure_by.isoformat() if row and row.measure_by else None,
                "version": row.version if row else 0,
            }
        )
    return found


# --- Sections


def unreviewed_sections(report: ClosureReport) -> list[str]:
    return [name for name in CLOSURE_SECTIONS if rules.is_unreviewed(report, name)]


def empty_sections(report: ClosureReport) -> list[str]:
    return [n for n in CLOSURE_SECTIONS if not ((report.sections or {}).get(n) or "").strip()]


def run_check(report: ClosureReport) -> list[dict[str, str]]:
    report.flags = [
        f.as_dict() for f in rules.check(report.sections, report.figures, None, CLOSURE_SECTIONS)
    ]
    return report.flags


def fill(db: Session, report: ClosureReport, project: Project) -> None:
    """Read the figures from the records and run the checker."""
    report.figures_at = utcnow()
    report.figures = build_figures(db, project, report.figures_at.date())
    run_check(report)


def new_report(db: Session, project: Project, *, created_by: int | None) -> ClosureReport:
    """A new closure report with its figures and empty text, flushed. `created_by` is None when
    the orchestrator makes it."""
    report = ClosureReport(
        project_id=project.id,
        sections=dict.fromkeys(CLOSURE_SECTIONS),
        section_sources={},
        flags=[],
        created_by=created_by,
    )
    fill(db, report, project)
    db.add(report)
    db.flush()
    return report


def report_of(db: Session, project_id: int, *, lock: bool = False) -> ClosureReport | None:
    stmt = select(ClosureReport).where(ClosureReport.project_id == project_id)
    return db.scalar(stmt.with_for_update() if lock else stmt)


def freeze(db: Session, report: ClosureReport, project: Project) -> None:
    """Freeze the report as it is when the close-out gate is passed: its figures are read once
    more, so they are the records at that moment, and nothing on it changes again."""
    fill(db, report, project)
    report.frozen_at = report.figures_at
    db.flush()


def recorded_lessons(db: Session, project_id: int) -> list[Lesson]:
    return list(
        db.scalars(
            select(Lesson)
            .where(Lesson.project_id == project_id, Lesson.status == RECORDED)
            .order_by(Lesson.id)
        )
    )


# --- Audit


def snapshot(report: ClosureReport) -> dict[str, Any]:
    return {
        "figures": report.figures,
        "sections": report.sections,
        "section_sources": report.section_sources,
        "flags": report.flags,
        "frozen_at": report.frozen_at.isoformat() if report.frozen_at else None,
    }


def record(
    db: Session,
    *,
    subject_type: str,
    subject_id: int,
    version: int,
    project_id: int,
    actor_user_id: int | None,
    before: Any,
    after: Any,
    action: str,
    trigger: str = "human",
    **extra: Any,
) -> None:
    """One audit row for a write already flushed (so `version` is the new one)."""
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger=trigger,
        subject_type=subject_type,
        subject_id=str(subject_id),
        version=version,
        attempt=next_attempt(
            db,
            agent=AUDIT_AGENT,
            subject_type=subject_type,
            subject_id=str(subject_id),
            version=version,
        ),
        status="ok",
        actor_user_id=actor_user_id,
        signals=before,
        output=after,
        action_taken={"action": action, "project_id": project_id, **extra},
    )


def record_report(
    db: Session,
    report: ClosureReport,
    *,
    actor_user_id: int | None,
    before: dict[str, Any] | None,
    action: str,
    trigger: str = "human",
    **extra: Any,
) -> None:
    record(
        db,
        subject_type=SUBJECT,
        subject_id=report.id,
        version=report.version,
        project_id=report.project_id,
        actor_user_id=actor_user_id,
        before=before,
        after=snapshot(report),
        action=action,
        trigger=trigger,
        **extra,
    )


def lesson_snapshot(lesson: Lesson) -> dict[str, Any]:
    return {
        "category": lesson.category,
        "what_happened": lesson.what_happened,
        "do_again": lesson.do_again,
        "do_differently": lesson.do_differently,
        "suggest_as_risk": lesson.suggest_as_risk,
        "source": lesson.source,
        "status": lesson.status,
    }


def benefit_snapshot(row: ClosureBenefit) -> dict[str, Any]:
    return {
        "benefit_id": row.benefit_id,
        "value_now": row.value_now,
        "value_now_on": row.value_now_on.isoformat() if row.value_now_on else None,
        "measure_owner_id": row.measure_owner_id,
        "measure_by": row.measure_by.isoformat() if row.measure_by else None,
    }
