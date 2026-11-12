"""Status reports (item 7.3): one project's report for one reporting period.

A report covers `period_start` to `period_end` (both included) and is due on `due_on`, all worked
out from the plan's `report_period` and `report_due_day` (`app.reports`). Its status is `draft`,
`published` or `withdrawn`.

- `figures`: what code read from the records when the draft was made or last refreshed, each with
  its sources (table, row id and field). `ratings`: green, amber or red for schedule, cost, scope
  and risk, and the overall rating, each with the rule that set it (`app.reports`).
- `sections`: the text: summary, done this period, planned for next period, issues, and
  decisions needed from the sponsor. `flags`: the checker's flags on that text.
  `section_sources` (item 7.4): who wrote each section, the status report agent (`agent`) or a
  person (`person`), and who reviewed an agent's draft and when. An agent's draft no person has
  edited or confirmed is unreviewed, and the report can't be published while one is.
- `created_by` is empty for a draft the orchestrator created when the report fell due.
- Publishing freezes a report: its figures, ratings and text never change again. A correction is
  a new report for the same period naming the one it replaces (`replaces_id`, with
  `replace_reason`); publishing the correction withdraws the old one with that reason.
  `published_at` stays set on a withdrawn report that had been published, so the sponsor still
  sees it; a withdrawn draft was never shown to anyone without `report:publish`.

`version` guards concurrent writes (`version_id_col`), so a write that lost a race fails.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Date, DateTime, ForeignKey, Index, String, column
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, utcnow

STATUS_REPORTS_TABLE = table_name("status_reports")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

DRAFT = "draft"
PUBLISHED = "published"
WITHDRAWN = "withdrawn"
STATUSES = (DRAFT, PUBLISHED, WITHDRAWN)
REASON_MAX = 500
SECTION_MAX = 4000
# The text sections, in the order a report shows them.
SECTIONS = ("summary", "done", "next", "issues", "decisions")


class StatusReport(Base):
    __tablename__ = STATUS_REPORTS_TABLE
    __table_args__ = (
        CheckConstraint(column("status").in_(STATUSES), name="ck_status_reports_status"),
        CheckConstraint(
            column("period_start") <= column("period_end"), name="ck_status_reports_period"
        ),
        Index("ix_status_reports_project_period", "project_id", "period_end"),
        # Audit rows name a report by id, so an id is never reused.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_status_reports_project")
    )
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    due_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default=DRAFT)
    figures: Mapped[Any] = mapped_column(JSON)
    ratings: Mapped[Any] = mapped_column(JSON)
    sections: Mapped[Any] = mapped_column(JSON)
    section_sources: Mapped[Any] = mapped_column(JSON, default=dict)
    flags: Mapped[Any] = mapped_column(JSON)
    figures_at: Mapped[datetime] = mapped_column(DateTime)
    replaces_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{STATUS_REPORTS_TABLE}.id", name="fk_status_reports_replaces"), index=True
    )
    replace_reason: Mapped[str | None] = mapped_column(String(REASON_MAX))
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_status_reports_created_by"), index=True
    )
    published_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_status_reports_published_by"), index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    withdrawn_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_status_reports_withdrawn_by"), index=True
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime)
    withdrawn_reason: Mapped[str | None] = mapped_column(String(REASON_MAX))
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}
