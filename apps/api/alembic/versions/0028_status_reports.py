"""Status reports (item 7.3).

`status_reports` is new: one project's report for one period, with the figures and ratings code
worked out, the text a person wrote and the checker's flags. A correction names the report it
replaces (`replaces_id`).

On MySQL the downgrade drops the table rather than its indexes (foreign-key indexes can't be
dropped first).

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPORTS = table_name("status_reports")
PROJECTS = table_name("projects")
USERS = table_name("users")


def upgrade() -> None:
    op.create_table(
        REPORTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("figures", sa.JSON(), nullable=False),
        sa.Column("ratings", sa.JSON(), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.Column("figures_at", sa.DateTime(), nullable=False),
        sa.Column("replaces_id", sa.Integer(), nullable=True),
        sa.Column("replace_reason", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("published_by", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("withdrawn_by", sa.Integer(), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(), nullable=True),
        sa.Column("withdrawn_reason", sa.String(length=500), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'withdrawn')", name="ck_status_reports_status"
        ),
        sa.CheckConstraint("period_start <= period_end", name="ck_status_reports_period"),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_status_reports_project"
        ),
        sa.ForeignKeyConstraint(
            ["replaces_id"], [f"{REPORTS}.id"], name="fk_status_reports_replaces"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], [f"{USERS}.id"], name="fk_status_reports_created_by"
        ),
        sa.ForeignKeyConstraint(
            ["published_by"], [f"{USERS}.id"], name="fk_status_reports_published_by"
        ),
        sa.ForeignKeyConstraint(
            ["withdrawn_by"], [f"{USERS}.id"], name="fk_status_reports_withdrawn_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_status_reports_project_period", REPORTS, ["project_id", "period_end"])
    op.create_index(f"ix_{REPORTS}_replaces_id", REPORTS, ["replaces_id"])
    op.create_index(f"ix_{REPORTS}_created_by", REPORTS, ["created_by"])
    op.create_index(f"ix_{REPORTS}_published_by", REPORTS, ["published_by"])
    op.create_index(f"ix_{REPORTS}_withdrawn_by", REPORTS, ["withdrawn_by"])


def downgrade() -> None:
    op.drop_table(REPORTS)
