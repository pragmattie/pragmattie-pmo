"""Freeze a project's plan as a baseline (item 5.4).

`freeze` copies the plan as it is now (scope, deliverables, milestones, estimate lines,
contingency and total) into a new `ProjectBaseline` numbered after the project's last one, made by
the given gate decision (baseline 1) or approved change request (item 5.5, every later one).
Money is stored as 2-decimal strings and dates as ISO text, so the copy reads the same whatever
happens to the plan later.

It only flushes, never commits: the gate and change request routes commit it with the decision in
one transaction, so a failure anywhere leaves no baseline and no decision.
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.intake import _json_value
from app.models import PlanDeliverable, PlanEstimateLine, ProjectPlan
from app.models.baseline import ProjectBaseline
from app.plans import compute_total, milestones_of, subtotal


def plan_snapshot(db: Session, plan: ProjectPlan) -> dict[str, Any]:
    """The plan's content as a baseline keeps it."""
    milestones = milestones_of(db, plan.id)
    deliverables = list(
        db.scalars(
            select(PlanDeliverable)
            .where(PlanDeliverable.plan_id == plan.id)
            .order_by(PlanDeliverable.id)
        )
    )
    lines = list(
        db.scalars(
            select(PlanEstimateLine)
            .where(PlanEstimateLine.plan_id == plan.id)
            .order_by(PlanEstimateLine.id)
        )
    )
    amounts = [line.amount for line in lines]
    lines_total = subtotal(amounts)
    total = compute_total(amounts, plan.contingency_pct)
    names = {m.id: m.name for m in milestones}
    return {
        "plan_id": plan.id,
        "plan_version": plan.version,
        "scope_in": plan.scope_in,
        "scope_out": plan.scope_out,
        "milestones": [
            {
                "id": m.id,
                "name": m.name,
                "target_date": _json_value(m.target_date),
                "owner_user_id": m.owner_user_id,
                "description": m.description,
                "contributors": m.contributors,
            }
            for m in milestones
        ],
        "deliverables": [
            {
                "id": d.id,
                "name": d.name,
                "description": d.description,
                "milestone_id": d.milestone_id,
                "milestone_name": names.get(d.milestone_id) if d.milestone_id else None,
            }
            for d in deliverables
        ],
        "estimate_lines": [
            {
                "id": line.id,
                "description": line.description,
                "category": line.category,
                "amount": _json_value(line.amount),
            }
            for line in lines
        ],
        "contingency_pct": plan.contingency_pct,
        "lines_total": _json_value(lines_total),
        "contingency_amount": _json_value(total - lines_total),
        "total": _json_value(total),
    }


def freeze(
    db: Session,
    plan: ProjectPlan,
    gate_decision_id: int | None = None,
    *,
    change_request_id: int | None = None,
) -> ProjectBaseline:
    """Store the plan as the project's next baseline (1 for the first) and flush it. Exactly
    one of `gate_decision_id` and `change_request_id` says what made it."""
    if (gate_decision_id is None) == (change_request_id is None):
        raise ValueError("a baseline is made by one gate decision or one change request")
    last = db.scalar(
        select(func.max(ProjectBaseline.number)).where(
            ProjectBaseline.project_id == plan.project_id
        )
    )
    content = plan_snapshot(db, plan)
    baseline = ProjectBaseline(
        project_id=plan.project_id,
        number=(last or 0) + 1,
        gate_decision_id=gate_decision_id,
        change_request_id=change_request_id,
        plan=content,
        total=Decimal(content["total"]),
    )
    db.add(baseline)
    db.flush()
    return baseline


def latest_baseline(db: Session, project_id: int) -> ProjectBaseline | None:
    return db.scalar(
        select(ProjectBaseline)
        .where(ProjectBaseline.project_id == project_id)
        .order_by(ProjectBaseline.number.desc())
        .limit(1)
    )
