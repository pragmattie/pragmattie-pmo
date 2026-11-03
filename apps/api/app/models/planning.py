"""The records planning starts from, made when a request passes the approval gate (item 4.6).

- `ProjectBrief`: one per project. Why it exists, what is in and out of scope, the rough dates and
  budget, and who decides what. Prefilled from the request; the project manager edits it.
- `ProjectStakeholder`: who has an interest in the project, what they need to hear and how often.
  A stakeholder may be a user (`user_id`) or just a name, such as the client's contact.
- `ProjectAssumption`: something the plan takes as true until its owner confirms it (or finds it
  wrong) by the confirm-by date.

`version` guards concurrent edits as on requests and risks: a client sends the version it read,
and SQLAlchemy adds it to every UPDATE (`version_id_col`), so a write that lost a race fails.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    column,
    or_,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, Money, utcnow
from app.models.intake import LONG_TEXT_MAX

BRIEFS_TABLE = table_name("project_briefs")
STAKEHOLDERS_TABLE = table_name("project_stakeholders")
ASSUMPTIONS_TABLE = table_name("project_assumptions")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

NAME_MAX = 200
INTEREST_MAX = 200
NEEDS_MAX = 500
HOW_OFTEN_MAX = 100
ASSUMPTION_MAX = 500
ASSUMPTION_STATUSES = ("open", "confirmed", "wrong")
# Who decides what: one entry per role. A handful in practice; 10 keeps a brief inside the
# API's body limit with every field at its longest.
DECISION_RIGHTS_MAX = 10


class ProjectBrief(Base):
    __tablename__ = BRIEFS_TABLE
    __table_args__ = (
        CheckConstraint(
            or_(
                column("budget_low").is_(None),
                column("budget_high").is_(None),
                column("budget_low") <= column("budget_high"),
            ),
            name="ck_project_briefs_budget",
        ),
        UniqueConstraint("project_id", name="uq_project_briefs_project"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_project_briefs_project")
    )
    purpose: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    objectives: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    scope_in: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    scope_out: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    wanted_by: Mapped[date | None] = mapped_column(Date)
    budget_low: Mapped[Decimal | None] = mapped_column(Money)
    budget_high: Mapped[Decimal | None] = mapped_column(Money)
    # [{"role": ..., "title": ..., "decides": ...}], from the approvers file's roles.
    decision_rights: Mapped[Any] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class ProjectStakeholder(Base):
    __tablename__ = STAKEHOLDERS_TABLE
    __table_args__ = (
        # Audit rows name a stakeholder by id, so an id is never reused after a delete. MySQL
        # never reuses one; SQLite needs AUTOINCREMENT for that.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_project_stakeholders_project"), index=True
    )
    name: Mapped[str] = mapped_column(String(NAME_MAX))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_project_stakeholders_user"), index=True
    )
    role_or_interest: Mapped[str] = mapped_column(String(INTEREST_MAX))
    needs_to_hear: Mapped[str | None] = mapped_column(String(NEEDS_MAX))
    how_often: Mapped[str | None] = mapped_column(String(HOW_OFTEN_MAX))
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class ProjectAssumption(Base):
    __tablename__ = ASSUMPTIONS_TABLE
    __table_args__ = (
        CheckConstraint(
            column("status").in_(ASSUMPTION_STATUSES), name="ck_project_assumptions_status"
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_project_assumptions_project"), index=True
    )
    text: Mapped[str] = mapped_column(String(ASSUMPTION_MAX))
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_project_assumptions_owner"), index=True
    )
    confirm_by: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="open")
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}
