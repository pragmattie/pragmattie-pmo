"""Read receipts for status reports (item 7.5).

`status_report_reads` is new and append-only: the first time a project's sponsor opened a
published report (who and when). `UNIQUE(report_id, user_id)` keeps only that first opening.

On MySQL the downgrade drops the table rather than its indexes (foreign-key indexes can't be
dropped first).

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READS = table_name("status_report_reads")
REPORTS = table_name("status_reports")
USERS = table_name("users")


def upgrade() -> None:
    op.create_table(
        READS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["report_id"], [f"{REPORTS}.id"], name="fk_status_report_reads_report"
        ),
        sa.ForeignKeyConstraint(["user_id"], [f"{USERS}.id"], name="fk_status_report_reads_user"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id", "user_id", name="uq_status_report_reads_report_user"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{READS}_user_id", READS, ["user_id"])


def downgrade() -> None:
    op.drop_table(READS)
