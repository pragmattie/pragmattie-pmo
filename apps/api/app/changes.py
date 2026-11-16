"""What a change request does to a project, and who decides it (item 5.5).

- `working_days`: the move between two dates in working days, Monday to Friday (no holiday
  calendar), positive when later.
- `unknown_items`: the ids a change set names that its baseline doesn't have.
- `compute_effect`: rules in code, no model. A pure function of the baseline's content, the change
  set, the changes policy and who raised the change: the cost difference (decimal, shown as a
  2-decimal string) and its percentage of the baseline total (rounded half-up to 2 places for
  display only). The level compares exactly, `difference * 100 > limit * baseline total`, with no
  rounding first: on a 1000.00 baseline +50.00 stays with the project manager, while +50.01
  (5.001%, shown as 5.00%) goes to the sponsor. Also the largest moves later and earlier in
  working days, whether scope changes, and why the change escalates. The higher decider wins:
  one part needing the sponsor sends the whole change there. A change that lowers the cost or
  brings dates earlier stays with the project manager; any cost increase on a baseline total of
  zero goes to the sponsor. So does a change the project manager raised, or one on a project
  with no project manager.
- `apply`: writes an approved change onto the plan. It refuses (`ChangeConflict`, naming the
  problem) anything that would leave an invalid plan: an item removed or changed that the plan no
  longer has, a milestone that isn't there, or a line not above zero. It only flushes; the route
  freezes the next baseline and commits it with the decision in one transaction.
- `waiting_changes`: a project's change requests that are waiting, meaning submitted and not yet
  decided (item 7.6). The one definition: the status report counts these as "change requests
  waiting" and the acceptance gate's `no_waiting_changes` refuses while there is one, so the two
  never disagree. A draft never submitted is not waiting.
"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChangeRequest,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    ProjectPlan,
)
from app.plans import CENT, compute_total
from app.policy import ChangesPolicy

PROJECT_MANAGER = "project_manager"

# Why a change goes past the project manager.
COST_ABOVE_LIMIT = "cost_above_limit"
COST_ON_ZERO_BASELINE = "cost_increase_on_zero_baseline"
DATES_ABOVE_LIMIT = "dates_above_limit"
SCOPE_CHANGE = "scope_change"
RAISED_BY_PROJECT_MANAGER = "raised_by_project_manager"
NO_PROJECT_MANAGER = "no_project_manager"


class ChangeConflict(Exception):
    """The change can't be applied to the plan as it is now; the message names the problem."""


# A change request waits from its submission until it is decided.
WAITING = "submitted"


def waiting_changes(db: Session, project_id: int) -> list[ChangeRequest]:
    """The project's change requests waiting for a decision, oldest first."""
    stmt = (
        select(ChangeRequest)
        .where(ChangeRequest.project_id == project_id, ChangeRequest.status == WAITING)
        .order_by(ChangeRequest.id)
    )
    return list(db.scalars(stmt))


def working_days(start: date, end: date) -> int:
    """Working days (Monday to Friday) from `start` to `end`: the weekdays after `start` up to
    and including `end`, negative when `end` is earlier. Friday to Monday is 1."""
    if end < start:
        return -working_days(end, start)
    weeks, extra = divmod((end - start).days, 7)
    count = weeks * 5
    for offset in range(1, extra + 1):
        if (start + timedelta(days=offset)).weekday() < 5:
            count += 1
    return count


def _by_id(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {item["id"]: item for item in items}


def unknown_items(baseline: dict[str, Any], changes: dict[str, Any]) -> list[str]:
    """What the change set names that the baseline doesn't have, in plain words."""
    deliverables = _by_id(baseline["deliverables"])
    milestones = _by_id(baseline["milestones"])
    lines = _by_id(baseline["estimate_lines"])
    problems = []
    for item_id in changes["remove_deliverables"]:
        if item_id not in deliverables:
            problems.append(f"deliverable {item_id} is not in the baseline")
    for added in changes["add_deliverables"]:
        if added["milestone_id"] is not None and added["milestone_id"] not in milestones:
            problems.append(f"milestone {added['milestone_id']} is not in the baseline")
    for move in changes["move_milestones"]:
        if move["milestone_id"] not in milestones:
            problems.append(f"milestone {move['milestone_id']} is not in the baseline")
    for item_id in [*changes["remove_lines"], *(c["line_id"] for c in changes["change_lines"])]:
        if item_id not in lines:
            problems.append(f"estimate line {item_id} is not in the baseline")
    return problems


def _scope_changes(baseline: dict[str, Any], changes: dict[str, Any]) -> list[str]:
    changed = []
    for field in ("scope_in", "scope_out"):
        if changes[field] is not None and changes[field] != baseline[field]:
            changed.append(field)
    if changes["add_deliverables"]:
        changed.append("deliverables_added")
    if changes["remove_deliverables"]:
        changed.append("deliverables_removed")
    return changed


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def compute_effect(
    baseline: dict[str, Any],
    changes: dict[str, Any],
    policy: ChangesPolicy,
    *,
    baseline_number: int,
    has_project_manager: bool,
    raised_by_project_manager: bool,
) -> tuple[dict[str, Any], str]:
    """The change's effect against the baseline's content, and who must decide it."""
    amounts = {line["id"]: Decimal(line["amount"]) for line in baseline["estimate_lines"]}
    for line_id in changes["remove_lines"]:
        amounts.pop(line_id, None)
    for changed in changes["change_lines"]:
        if changed["amount"] is not None and changed["line_id"] in amounts:
            amounts[changed["line_id"]] = Decimal(changed["amount"])
    new_amounts = [*amounts.values(), *(Decimal(a["amount"]) for a in changes["add_lines"])]
    contingency = changes["contingency_pct"]
    if contingency is None:
        contingency = baseline["contingency_pct"]
    old_total = Decimal(baseline["total"])
    new_total = compute_total(new_amounts, contingency)
    difference = new_total - old_total
    percent = (
        None
        if old_total == 0
        else (difference * 100 / old_total).quantize(CENT, rounding=ROUND_HALF_UP)
    )

    dates = {m["id"]: date.fromisoformat(m["target_date"]) for m in baseline["milestones"]}
    moves = [
        working_days(dates[m["milestone_id"]], date.fromisoformat(m["target_date"]))
        for m in changes["move_milestones"]
        if m["milestone_id"] in dates
    ]
    later = max((m for m in moves if m > 0), default=0)
    earlier = max((-m for m in moves if m < 0), default=0)
    scope = _scope_changes(baseline, changes)

    escalations = []
    if difference > 0:
        if old_total == 0:
            escalations.append(COST_ON_ZERO_BASELINE)
        # Exact, never the rounded percentage: 5.001% is over a 5% limit.
        elif difference * 100 > policy.max_cost_increase_pct * old_total:
            escalations.append(COST_ABOVE_LIMIT)
    if later > policy.max_date_move_working_days:
        escalations.append(DATES_ABOVE_LIMIT)
    if scope:
        escalations.append(SCOPE_CHANGE)
    if not has_project_manager:
        escalations.append(NO_PROJECT_MANAGER)
    elif raised_by_project_manager:
        escalations.append(RAISED_BY_PROJECT_MANAGER)

    effect = {
        "baseline_number": baseline_number,
        "baseline_total": _money(old_total),
        "new_total": _money(new_total),
        "cost_difference": _money(difference),
        "cost_difference_pct": _money(percent) if percent is not None else None,
        "max_later_working_days": later,
        "max_earlier_working_days": earlier,
        "scope_changes": bool(scope),
        "scope_changed": scope,
        "escalations": escalations,
        "limits": {
            "max_cost_increase_pct": _money(policy.max_cost_increase_pct),
            "max_date_move_working_days": policy.max_date_move_working_days,
        },
    }
    return effect, policy.escalate_to if escalations else PROJECT_MANAGER


def _rows[T](db: Session, model: type[T], plan: ProjectPlan) -> dict[int, T]:
    stmt = select(model).where(model.plan_id == plan.id).with_for_update()  # type: ignore[attr-defined]
    return {row.id: row for row in db.scalars(stmt)}  # type: ignore[attr-defined]


def _name(items: list[dict[str, Any]], item_id: int, what: str) -> str:
    found = next((item for item in items if item["id"] == item_id), None)
    label = found.get("name") or found.get("description") if found else None
    return f"{what} '{label}'" if label else f"{what} {item_id}"


def apply(
    db: Session, plan: ProjectPlan, baseline: dict[str, Any], changes: dict[str, Any]
) -> None:
    """Write the change onto the locked plan and flush; raise `ChangeConflict` if the result
    wouldn't be a valid plan. The caller rolls back on any failure."""
    deliverables = _rows(db, PlanDeliverable, plan)
    milestones = _rows(db, PlanMilestone, plan)
    lines = _rows(db, PlanEstimateLine, plan)

    for item_id in changes["remove_deliverables"]:
        row = deliverables.pop(item_id, None)
        if row is None:
            name = _name(baseline["deliverables"], item_id, "deliverable")
            raise ChangeConflict(f"{name} is no longer in the plan, so it can't be removed")
        db.delete(row)
    for move in changes["move_milestones"]:
        row = milestones.get(move["milestone_id"])
        if row is None:
            name = _name(baseline["milestones"], move["milestone_id"], "milestone")
            raise ChangeConflict(f"{name} is no longer in the plan, so it can't be moved")
        row.target_date = date.fromisoformat(move["target_date"])
    for added in changes["add_deliverables"]:
        if added["milestone_id"] is not None and added["milestone_id"] not in milestones:
            name = _name(baseline["milestones"], added["milestone_id"], "milestone")
            raise ChangeConflict(f"{name} is no longer in the plan, so nothing can be added to it")
        db.add(
            PlanDeliverable(
                plan_id=plan.id,
                name=added["name"],
                description=added["description"],
                milestone_id=added["milestone_id"],
            )
        )
    for line_id in changes["remove_lines"]:
        row = lines.pop(line_id, None)
        if row is None:
            name = _name(baseline["estimate_lines"], line_id, "estimate line")
            raise ChangeConflict(f"{name} is no longer in the plan, so it can't be removed")
        db.delete(row)
    for changed in changes["change_lines"]:
        row = lines.get(changed["line_id"])
        if row is None:
            name = _name(baseline["estimate_lines"], changed["line_id"], "estimate line")
            raise ChangeConflict(f"{name} is no longer in the plan, so it can't be changed")
        for field in ("description", "category"):
            if changed[field] is not None:
                setattr(row, field, changed[field])
        if changed["amount"] is not None:
            row.amount = Decimal(changed["amount"])
    for added in changes["add_lines"]:
        db.add(
            PlanEstimateLine(
                plan_id=plan.id,
                description=added["description"],
                category=added["category"],
                amount=Decimal(added["amount"]),
            )
        )
    for row in lines.values():
        if row.amount <= 0:
            raise ChangeConflict(f"estimate line '{row.description}' would not be above zero")
    for field in ("scope_in", "scope_out", "contingency_pct"):
        if changes[field] is not None:
            setattr(plan, field, changes[field])
    db.flush()
