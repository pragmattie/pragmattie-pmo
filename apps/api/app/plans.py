"""What a plan adds up to, and what it still lacks (item 5.1).

- `compute_total`: the estimate lines plus the contingency, rounded half-up to 2 places. It is
  computed on every read and never stored or taken from a client.
- `budget_fit`: the total against the brief's budget range: `within`, `below` or `above`, or
  `no_range` when the brief gives neither bound. Whichever bound exists is compared.
- `missing`: the gaps the go-ahead gate (5.4) will check, in plain words; empty for a complete
  plan.
"""

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    ProjectBrief,
    ProjectPlan,
    User,
)

CENT = Decimal("0.01")

NO_SCOPE_IN = "Scope in is empty"
NO_DELIVERABLES = "No deliverables"
NO_MILESTONES = "No milestones"
NO_ESTIMATE = "No estimate lines"
NO_REPORTING = "No reporting plan"


def new_plan(project_id: int, brief: ProjectBrief | None) -> ProjectPlan:
    """An empty plan, its scope prefilled from the brief."""
    return ProjectPlan(
        project_id=project_id,
        scope_in=brief.scope_in if brief else None,
        scope_out=brief.scope_out if brief else None,
        contingency_pct=0,
    )


def subtotal(amounts: Iterable[Decimal]) -> Decimal:
    return sum((Decimal(a) for a in amounts), Decimal("0")).quantize(CENT)


def compute_total(amounts: Iterable[Decimal], contingency_pct: int) -> Decimal:
    """The lines plus contingency, rounded half-up to 2 decimal places."""
    lines = subtotal(amounts)
    return (lines * (100 + contingency_pct) / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def budget_fit(total: Decimal, low: Decimal | None, high: Decimal | None) -> str:
    if low is None and high is None:
        return "no_range"
    if low is not None and total < low:
        return "below"
    if high is not None and total > high:
        return "above"
    return "within"


def milestones_of(db: Session, plan_id: int) -> list[PlanMilestone]:
    """A plan's milestones in date order (id breaks ties)."""
    stmt = (
        select(PlanMilestone)
        .where(PlanMilestone.plan_id == plan_id)
        .order_by(PlanMilestone.target_date, PlanMilestone.id)
    )
    return list(db.scalars(stmt))


def missing(db: Session, plan: ProjectPlan) -> list[str]:
    """What the plan still lacks, in a fixed order; empty for a complete plan."""
    gaps = []
    if not plan.scope_in:
        gaps.append(NO_SCOPE_IN)
    deliverables = list(
        db.scalars(
            select(PlanDeliverable)
            .where(PlanDeliverable.plan_id == plan.id)
            .order_by(PlanDeliverable.id)
        )
    )
    if not deliverables:
        gaps.append(NO_DELIVERABLES)
    milestones = milestones_of(db, plan.id)
    if not milestones:
        gaps.append(NO_MILESTONES)
    owners = {m.owner_user_id for m in milestones if m.owner_user_id is not None}
    active = (
        set(db.scalars(select(User.id).where(User.id.in_(owners), User.is_active.is_(True))))
        if owners
        else set()
    )
    for milestone in milestones:
        if milestone.owner_user_id is None:
            gaps.append(f"Milestone '{milestone.name}' has no owner")
        elif milestone.owner_user_id not in active:
            gaps.append(f"Milestone '{milestone.name}': its owner is no longer active")
    for deliverable in deliverables:
        if deliverable.milestone_id is None:
            gaps.append(f"Deliverable '{deliverable.name}' has no milestone")
    has_line = db.scalar(
        select(PlanEstimateLine.id).where(PlanEstimateLine.plan_id == plan.id).limit(1)
    )
    if has_line is None:
        gaps.append(NO_ESTIMATE)
    if plan.report_period is None or plan.report_due_day is None:
        gaps.append(NO_REPORTING)
    return gaps
