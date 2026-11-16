"""Closure report, benefits measured at close and lessons (item 7.7).

- `closure_reports`: one per project (`UNIQUE(project_id)`), made while the project is in
  closure. Its figures with their sources (JSON), its three text sections and who wrote each, the
  checker's flags, and `frozen_at`, set when the close-out gate is passed: from then on its
  figures and text never change.
- `closure_benefits`: one per benefit of the project's value case (`UNIQUE(benefit_id)`): the
  value now and its date, which only a person enters, or the owner and date for measuring it
  later. The value case itself is never changed.
- `lessons`: what happened, what we would do again or differently, a category, whether it is
  offered to the risk agent as a suggested risk for future projects (`suggest_as_risk`), its
  writer (`person` or `agent`) and status (`suggested`, `recorded` or `discarded`).
- `risks.source_lesson_id`: the lesson a risk the risk agent suggested came from.

On MySQL the downgrade drops the new tables rather than their indexes (foreign-key indexes can't
be dropped first), after dropping the risks column's foreign key and then the column.

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPORTS = table_name("closure_reports")
BENEFITS = table_name("closure_benefits")
LESSONS = table_name("lessons")
RISKS = table_name("risks")
PROJECTS = table_name("projects")
USERS = table_name("users")
VALUE_BENEFITS = table_name("value_case_benefits")
KEEP_IDS = {"sqlite_autoincrement": True}


def upgrade() -> None:
    op.create_table(
        REPORTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("figures", sa.JSON(), nullable=False),
        sa.Column("figures_at", sa.DateTime(), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("section_sources", sa.JSON(), nullable=False),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.Column("frozen_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_closure_reports_project"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], [f"{USERS}.id"], name="fk_closure_reports_created_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_closure_reports_project"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{REPORTS}_created_by", REPORTS, ["created_by"])

    op.create_table(
        BENEFITS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("benefit_id", sa.Integer(), nullable=False),
        sa.Column("value_now", sa.String(length=200), nullable=True),
        sa.Column("value_now_on", sa.Date(), nullable=True),
        sa.Column("measure_owner_id", sa.Integer(), nullable=True),
        sa.Column("measure_by", sa.Date(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "(value_now IS NULL) = (value_now_on IS NULL)", name="ck_closure_benefits_value_now"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_closure_benefits_project"
        ),
        sa.ForeignKeyConstraint(
            ["benefit_id"], [f"{VALUE_BENEFITS}.id"], name="fk_closure_benefits_benefit"
        ),
        sa.ForeignKeyConstraint(
            ["measure_owner_id"], [f"{USERS}.id"], name="fk_closure_benefits_measure_owner"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], [f"{USERS}.id"], name="fk_closure_benefits_updated_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("benefit_id", name="uq_closure_benefits_benefit"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{BENEFITS}_project_id", BENEFITS, ["project_id"])
    op.create_index(f"ix_{BENEFITS}_measure_owner_id", BENEFITS, ["measure_owner_id"])
    op.create_index(f"ix_{BENEFITS}_updated_by", BENEFITS, ["updated_by"])

    op.create_table(
        LESSONS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("what_happened", sa.String(length=2000), nullable=False),
        sa.Column("do_again", sa.String(length=2000), nullable=True),
        sa.Column("do_differently", sa.String(length=2000), nullable=True),
        sa.Column("suggest_as_risk", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "category IN ('scope', 'schedule', 'cost', 'risk', 'people', 'ai')",
            name="ck_lessons_category",
        ),
        sa.CheckConstraint("source IN ('person', 'agent')", name="ck_lessons_source"),
        sa.CheckConstraint(
            "status IN ('suggested', 'recorded', 'discarded')", name="ck_lessons_status"
        ),
        sa.CheckConstraint(
            "do_again IS NOT NULL OR do_differently IS NOT NULL", name="ck_lessons_takeaway"
        ),
        sa.ForeignKeyConstraint(["project_id"], [f"{PROJECTS}.id"], name="fk_lessons_project"),
        sa.ForeignKeyConstraint(["created_by"], [f"{USERS}.id"], name="fk_lessons_created_by"),
        sa.ForeignKeyConstraint(["reviewed_by"], [f"{USERS}.id"], name="fk_lessons_reviewed_by"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_lessons_project_status", LESSONS, ["project_id", "status"])
    op.create_index(f"ix_{LESSONS}_created_by", LESSONS, ["created_by"])
    op.create_index(f"ix_{LESSONS}_reviewed_by", LESSONS, ["reviewed_by"])

    # Batch mode, so SQLite (which can't add a foreign key in place) rebuilds the table; risk
    # ids are never reused, so the rebuild keeps AUTOINCREMENT.
    with op.batch_alter_table(RISKS, table_kwargs=KEEP_IDS) as batch:
        batch.add_column(sa.Column("source_lesson_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_risks_source_lesson", LESSONS, ["source_lesson_id"], ["id"])
        batch.create_index(f"ix_{RISKS}_source_lesson_id", ["source_lesson_id"])


def downgrade() -> None:
    # Drop the foreign key first (MySQL won't drop an index a foreign key still uses), then the
    # index, then the column.
    with op.batch_alter_table(RISKS, table_kwargs=KEEP_IDS) as batch:
        batch.drop_constraint("fk_risks_source_lesson", type_="foreignkey")
        batch.drop_index(f"ix_{RISKS}_source_lesson_id")
        batch.drop_column("source_lesson_id")
    op.drop_table(LESSONS)
    op.drop_table(BENEFITS)
    op.drop_table(REPORTS)
