"""Project requests: the front door for new work (item 4.1).

A request is `draft` while its project manager works on it, `submitted` once it joins the queue,
and `withdrawn` when the project manager takes it back. `approved`, `declined` and a return to
`draft` are set only at the approval gate (a later item). `declined` and `withdrawn` are final.

`requested_by` is the signed-in person who created it. A request with no client is internal work.

`version` guards concurrent edits: a client sends the version it read, and SQLAlchemy also adds
it to every UPDATE (`version_id_col`), so a write that lost a race fails instead of overwriting.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String, column, or_
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, Money, utcnow

REQUESTS_TABLE = table_name("project_requests")
CLIENTS_TABLE = table_name("clients")
USERS_TABLE = table_name("users")

STATUSES = ("draft", "submitted", "approved", "declined", "withdrawn")
FINAL_STATUSES = ("declined", "withdrawn")

TITLE_MAX = 200
# The longest a free-text field may be, in characters. Four of these sit in one row, so they stay
# well inside MySQL's row size limit for VARCHAR columns in utf8mb4.
LONG_TEXT_MAX = 2000


class ProjectRequest(Base):
    __tablename__ = REQUESTS_TABLE
    __table_args__ = (
        CheckConstraint(column("status").in_(STATUSES), name="ck_project_requests_status"),
        CheckConstraint(
            or_(
                column("budget_low").is_(None),
                column("budget_high").is_(None),
                column("budget_low") <= column("budget_high"),
            ),
            name="ck_project_requests_budget",
        ),
        Index("ix_project_requests_status", "status"),
        Index("ix_project_requests_submitted_at", "submitted_at"),
        Index("ix_project_requests_wanted_by", "wanted_by"),
        # Audit rows name a request by id, so an id is never reused. MySQL never reuses one;
        # SQLite needs AUTOINCREMENT for that.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(TITLE_MAX))
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{CLIENTS_TABLE}.id", name="fk_project_requests_client"), index=True
    )
    asked_for: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    problem: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    outcome: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    budget_low: Mapped[Decimal | None] = mapped_column(Money)
    budget_high: Mapped[Decimal | None] = mapped_column(Money)
    wanted_by: Mapped[date | None] = mapped_column(Date)
    uses_ai: Mapped[bool] = mapped_column(default=False)
    known_risks: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    requested_by: Mapped[int] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_project_requests_requested_by"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}
