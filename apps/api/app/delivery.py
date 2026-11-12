"""How a project in delivery is going (item 7.2): milestones reached, deliverable status and spend
to date. Rules in code, no model.

- `refusal`: why a project takes no progress now (cancelled, or not in delivery), or None.
- `baseline_targets`: each milestone's target date in the current baseline, by milestone id.
  Lateness is always measured against it, never against a date a change request has moved.
- `days_late`: working days (Monday to Friday) from the baseline target to the reached date:
  above 0 is late, below 0 early, 0 on time. A milestone not yet reached counts to today (UTC)
  and is never below 0, since it can't be early before it is reached. No target, no figure.
- `set_reached` and `set_status`: write progress onto the live plan row with its own counter,
  checking the counter in the UPDATE itself. They never touch the row's `version` (or its
  `updated_at`), so progress never looks like a plan edit and the plan and baseline stay as they
  were. They return False when the counter had already moved (a lost race).
"""

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.changes import working_days
from app.models import PlanDeliverable, PlanMilestone, Project
from app.models.baseline import ProjectBaseline
from app.models.portfolio import CANCELLED

DELIVERY = "delivery"

CANCELLED_PROJECT = "this project is cancelled and takes no further progress"
NOT_IN_DELIVERY = "progress is recorded for a project in delivery"


def today() -> date:
    """Today's date in UTC."""
    return datetime.now(UTC).date()


def refusal(project: Project) -> tuple[str, str] | None:
    if project.status == CANCELLED:
        return "cancelled", CANCELLED_PROJECT
    if project.stage != DELIVERY:
        return "not_in_delivery", NOT_IN_DELIVERY
    return None


def baseline_targets(baseline: ProjectBaseline | None) -> dict[int, date]:
    if baseline is None:
        return {}
    return {m["id"]: date.fromisoformat(m["target_date"]) for m in baseline.plan["milestones"]}


def days_late(target: date | None, reached_on: date | None, on: date) -> int | None:
    if target is None:
        return None
    if reached_on is not None:
        return working_days(target, reached_on)
    return max(0, working_days(target, on))


def _write(db: Session, table: Any, row_id: int, counter: str, version: int, **values) -> bool:
    result = db.execute(
        update(table)
        .where(table.c.id == row_id, table.c[counter] == version)
        .values(**values, **{counter: version + 1, "updated_at": table.c.updated_at})
    )
    return result.rowcount == 1


def set_reached(
    db: Session, milestone: PlanMilestone, version: int, reached_on: date | None, note: str | None
) -> bool:
    written = _write(
        db,
        PlanMilestone.__table__,
        milestone.id,
        "reached_version",
        version,
        reached_on=reached_on,
        reached_note=note if reached_on is not None else None,
    )
    db.refresh(milestone)
    return written


def set_status(db: Session, deliverable: PlanDeliverable, version: int, status: str) -> bool:
    written = _write(
        db, PlanDeliverable.__table__, deliverable.id, "status_version", version, status=status
    )
    db.refresh(deliverable)
    return written
