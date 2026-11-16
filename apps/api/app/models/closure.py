"""Closing a project (item 7.7): its closure report, each benefit as measured at close, and its
lessons.

- `ClosureReport`: one per project, made while the project is in closure. `figures` are what code
  read from the records (`app.closure`), each with its sources; `sections` are the text (what was
  delivered, how it compares with the value case, what remains), and `section_sources` says who
  wrote each, the closure agent (`agent`) or a person (`person`), and who reviewed an agent's
  draft. `frozen_at` is set when the close-out gate is passed: the figures and text as they were
  then are what the closed project shows, and nothing on it changes again.
- `ClosureBenefit`: one per benefit of the project's value case. The value now and its date are
  entered only by a person; a benefit not yet measurable has an owner and a date to measure it.
  The value case itself is never changed.
- `Lesson`: what happened, what we would do again or differently, and a category. A person's
  lesson is `recorded`; one the closure agent suggests is `suggested` until a person records
  (confirms or edits) or discards it. `suggest_as_risk` offers a recorded lesson of a closed
  project to the risk agent for future projects.

`version` guards concurrent writes on each (`version_id_col`), so a write that lost a race fails.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    column,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, utcnow
from app.models.intake import BENEFITS_TABLE, LONG_TEXT_MAX, TARGET_MAX

CLOSURE_REPORTS_TABLE = table_name("closure_reports")
CLOSURE_BENEFITS_TABLE = table_name("closure_benefits")
LESSONS_TABLE = table_name("lessons")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

# The closure report's text sections, in the order the report shows them.
CLOSURE_SECTIONS = ("delivered", "value_case", "remaining")
SECTION_MAX = 4000

LESSON_CATEGORIES = ("scope", "schedule", "cost", "risk", "people", "ai")
LESSON_SOURCES = ("person", "agent")
SUGGESTED, RECORDED, DISCARDED = "suggested", "recorded", "discarded"
LESSON_STATUSES = (SUGGESTED, RECORDED, DISCARDED)
LESSON_TEXT_MAX = LONG_TEXT_MAX


class ClosureReport(Base):
    __tablename__ = CLOSURE_REPORTS_TABLE
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_closure_reports_project"),
        # Audit rows name a report by id, so an id is never reused.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_closure_reports_project")
    )
    figures: Mapped[Any] = mapped_column(JSON)
    figures_at: Mapped[datetime] = mapped_column(DateTime)
    sections: Mapped[Any] = mapped_column(JSON)
    section_sources: Mapped[Any] = mapped_column(JSON, default=dict)
    flags: Mapped[Any] = mapped_column(JSON, default=list)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_closure_reports_created_by"), index=True
    )
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class ClosureBenefit(Base):
    __tablename__ = CLOSURE_BENEFITS_TABLE
    __table_args__ = (
        CheckConstraint(
            "(value_now IS NULL) = (value_now_on IS NULL)",
            name="ck_closure_benefits_value_now",
        ),
        UniqueConstraint("benefit_id", name="uq_closure_benefits_benefit"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_closure_benefits_project"), index=True
    )
    benefit_id: Mapped[int] = mapped_column(
        ForeignKey(f"{BENEFITS_TABLE}.id", name="fk_closure_benefits_benefit")
    )
    value_now: Mapped[str | None] = mapped_column(String(TARGET_MAX))
    value_now_on: Mapped[date | None] = mapped_column(Date)
    measure_owner_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_closure_benefits_measure_owner"), index=True
    )
    measure_by: Mapped[date | None] = mapped_column(Date)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_closure_benefits_updated_by"), index=True
    )
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class Lesson(Base):
    __tablename__ = LESSONS_TABLE
    __table_args__ = (
        CheckConstraint(column("category").in_(LESSON_CATEGORIES), name="ck_lessons_category"),
        CheckConstraint(column("source").in_(LESSON_SOURCES), name="ck_lessons_source"),
        CheckConstraint(column("status").in_(LESSON_STATUSES), name="ck_lessons_status"),
        CheckConstraint(
            "do_again IS NOT NULL OR do_differently IS NOT NULL",
            name="ck_lessons_takeaway",
        ),
        Index("ix_lessons_project_status", "project_id", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_lessons_project")
    )
    category: Mapped[str] = mapped_column(String(16))
    what_happened: Mapped[str] = mapped_column(String(LESSON_TEXT_MAX))
    do_again: Mapped[str | None] = mapped_column(String(LESSON_TEXT_MAX))
    do_differently: Mapped[str | None] = mapped_column(String(LESSON_TEXT_MAX))
    suggest_as_risk: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_lessons_created_by"), index=True
    )
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_lessons_reviewed_by"), index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}
