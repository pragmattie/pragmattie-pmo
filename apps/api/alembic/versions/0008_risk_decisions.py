"""Escalation and acceptance on risks: who a risk is escalated to, its band, and a review date.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RISKS = table_name("risks")


def upgrade() -> None:
    # Batch mode keeps SQLite's AUTOINCREMENT when it rebuilds the table.
    with op.batch_alter_table(RISKS, table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.add_column(sa.Column("escalated_to", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("escalated_band", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("review_date", sa.Date(), nullable=True))
        batch.create_index("ix_risks_escalated_to", ["escalated_to"])


def downgrade() -> None:
    # Plain columns and an index on one of them (not a foreign key), so they can go one by one.
    with op.batch_alter_table(RISKS, table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.drop_index("ix_risks_escalated_to")
        batch.drop_column("review_date")
        batch.drop_column("escalated_band")
        batch.drop_column("escalated_to")
