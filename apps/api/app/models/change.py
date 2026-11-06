"""Change requests: how a project's scope, dates and cost change once it is in delivery (item 5.5).

- `changes`: what the change asks for (scope text, deliverables added or removed, milestone dates,
  estimate lines added, removed or changed, contingency), as the API validated it.
- `effect`: what code computed from `changes` against the baseline numbered
  `measured_baseline_number`: the cost difference and percentage, the largest date moves, whether
  scope changes, and why it goes to its decider. Never taken from a client.
- `decider`: `project_manager` or `sponsor`, computed with the effect.

Statuses: `draft` (the only one that can be edited), `submitted`, and the final `approved`,
`rejected` and `withdrawn`. The baseline an approved change produced points back at it
(`project_baselines.change_request_id`).

`version` guards concurrent writes (`version_id_col`), so a write that lost a race fails.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String, column
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, utcnow
from app.models.intake import LONG_TEXT_MAX

CHANGE_REQUESTS_TABLE = table_name("change_requests")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

TITLE_MAX = 200
STATUSES = ("draft", "submitted", "approved", "rejected", "withdrawn")
FINAL_STATUSES = frozenset({"approved", "rejected", "withdrawn"})
DECIDERS = ("project_manager", "sponsor")


class ChangeRequest(Base):
    __tablename__ = CHANGE_REQUESTS_TABLE
    __table_args__ = (
        CheckConstraint(column("status").in_(STATUSES), name="ck_change_requests_status"),
        CheckConstraint(column("decider").in_(DECIDERS), name="ck_change_requests_decider"),
        # Audit rows name a change request by id, so an id is never reused.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_change_requests_project"), index=True
    )
    title: Mapped[str] = mapped_column(String(TITLE_MAX))
    reason: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    changes: Mapped[Any] = mapped_column(JSON)
    effect: Mapped[Any] = mapped_column(JSON)
    decider: Mapped[str] = mapped_column(String(32))
    measured_baseline_number: Mapped[int]
    status: Mapped[str] = mapped_column(String(16), default="draft")
    raised_by: Mapped[int] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_change_requests_raised_by"), index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_change_requests_decided_by"), index=True
    )
    decision_reason: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}
