"""A project's plan: what will be delivered, by when, for how much, who does what and how it is
reported (item 5.1).

- `ProjectPlan`: one per project. Scope in and out (prefilled from the brief), the contingency
  percentage and the reporting plan. The total is computed from the estimate lines and the
  contingency (`app/plans.py`); it is never stored.
- `PlanMilestone`: a name, a target date, an owner (an active user), a short description and
  who else contributes, as free text.
- `PlanDeliverable`: what the project hands over, and which milestone delivers it.
- `PlanEstimateLine`: one line of the estimate: a description, a category and an amount.

`report_due_day` means a weekday (1 to 7, Monday is 1) for weekly and fortnightly reports and a
day of the month (1 to 28) for monthly ones; the API checks it against the period on every write.

`version` guards concurrent edits as on the brief: SQLAlchemy adds it to every UPDATE
(`version_id_col`), so a write that lost a race fails.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, UniqueConstraint, column
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, Money, utcnow
from app.models.intake import LONG_TEXT_MAX

PLANS_TABLE = table_name("project_plans")
MILESTONES_TABLE = table_name("plan_milestones")
DELIVERABLES_TABLE = table_name("plan_deliverables")
ESTIMATE_LINES_TABLE = table_name("plan_estimate_lines")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

NAME_MAX = 200
DESCRIPTION_MAX = 500
CONTRIBUTORS_MAX = 500
CONTINGENCY_MAX = 50
REPORT_PERIODS = ("weekly", "fortnightly", "monthly")
ESTIMATE_CATEGORIES = ("people", "software", "services", "other")
# The highest due day each period allows: a weekday, or a day every month has.
DUE_DAY_MAX = {"weekly": 7, "fortnightly": 7, "monthly": 28}


class ProjectPlan(Base):
    __tablename__ = PLANS_TABLE
    __table_args__ = (
        CheckConstraint(
            column("contingency_pct").between(0, CONTINGENCY_MAX),
            name="ck_project_plans_contingency",
        ),
        CheckConstraint(
            column("report_period").in_(REPORT_PERIODS), name="ck_project_plans_report_period"
        ),
        CheckConstraint(
            column("report_due_day").between(1, max(DUE_DAY_MAX.values())),
            name="ck_project_plans_report_due_day",
        ),
        UniqueConstraint("project_id", name="uq_project_plans_project"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_project_plans_project")
    )
    scope_in: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    scope_out: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    contingency_pct: Mapped[int] = mapped_column(default=0)
    report_period: Mapped[str | None] = mapped_column(String(16))
    report_due_day: Mapped[int | None] = mapped_column()
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class PlanMilestone(Base):
    __tablename__ = MILESTONES_TABLE
    __table_args__ = (
        # Audit rows name a milestone by id, so an id is never reused after a delete.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PLANS_TABLE}.id", name="fk_plan_milestones_plan"), index=True
    )
    name: Mapped[str] = mapped_column(String(NAME_MAX))
    target_date: Mapped[date] = mapped_column(Date)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_plan_milestones_owner"), index=True
    )
    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX))
    contributors: Mapped[str | None] = mapped_column(String(CONTRIBUTORS_MAX))
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class PlanDeliverable(Base):
    __tablename__ = DELIVERABLES_TABLE
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PLANS_TABLE}.id", name="fk_plan_deliverables_plan"), index=True
    )
    name: Mapped[str] = mapped_column(String(NAME_MAX))
    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX))
    milestone_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{MILESTONES_TABLE}.id", name="fk_plan_deliverables_milestone"), index=True
    )
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class PlanEstimateLine(Base):
    __tablename__ = ESTIMATE_LINES_TABLE
    __table_args__ = (
        CheckConstraint(
            column("category").in_(ESTIMATE_CATEGORIES), name="ck_plan_estimate_lines_category"
        ),
        CheckConstraint(column("amount") > 0, name="ck_plan_estimate_lines_amount"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PLANS_TABLE}.id", name="fk_plan_estimate_lines_plan"), index=True
    )
    description: Mapped[str] = mapped_column(String(DESCRIPTION_MAX))
    category: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Money)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}
