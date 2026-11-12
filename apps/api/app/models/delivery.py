"""Spend to date for a project in delivery (item 7.2).

`ProjectSpend` is append-only, as the audit table is: the newest entry is the project's current
figure, and a correction is a new entry whose `supersedes_id` names the one it corrects. Changing
or deleting a loaded row, and a bulk `update()` or `delete()` on this table through any `Session`,
raise `AppendOnlyError`. Raw SQL on a plain connection can still bypass this, as on the audit
table.

Milestone and deliverable progress live on the plan rows themselves (`app.models.plan`).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, Delete, ForeignKey, String, Update, event
from sqlalchemy import column as sql_column
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column

from app.migrations_filter import table_name
from app.models.audit import AppendOnlyError
from app.models.base import Base, Money, utcnow

SPEND_TABLE = table_name("project_spend")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

NOTE_MAX = 200


class ProjectSpend(Base):
    __tablename__ = SPEND_TABLE
    __table_args__ = (
        CheckConstraint(sql_column("amount") >= 0, name="ck_project_spend_amount"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_project_spend_project"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Money)
    as_of: Mapped[date] = mapped_column(Date)
    note: Mapped[str] = mapped_column(String(NOTE_MAX))
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SPEND_TABLE}.id", name="fk_project_spend_supersedes"), index=True
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_project_spend_created_by"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def _refuse(*_args: Any) -> None:
    raise AppendOnlyError(f"{SPEND_TABLE} is append-only: record a correcting entry instead")


event.listen(ProjectSpend, "before_update", _refuse)
event.listen(ProjectSpend, "before_delete", _refuse)


@event.listens_for(Session, "do_orm_execute")
def _refuse_bulk_changes(state: ORMExecuteState) -> None:
    """Bulk `update()` and `delete()` skip the mapper hooks above, so refuse them here."""
    statement = state.statement
    if isinstance(statement, Update | Delete) and statement.table.name == SPEND_TABLE:
        _refuse()
