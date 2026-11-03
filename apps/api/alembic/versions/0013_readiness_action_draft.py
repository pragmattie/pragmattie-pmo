"""Readiness actions may have no owner or due date yet: an agent's draft leaves both to a person.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIONS = table_name("readiness_actions")


def upgrade() -> None:
    # Batch mode, so SQLite (which can't change a column in place) rebuilds the table.
    with op.batch_alter_table(ACTIONS) as batch:
        batch.alter_column("owner_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("due_date", existing_type=sa.Date(), nullable=True)


def downgrade() -> None:
    # Actions still waiting for a person's owner or date can't be kept as NOT NULL rows.
    op.execute(f"DELETE FROM {ACTIONS} WHERE owner_id IS NULL OR due_date IS NULL")
    with op.batch_alter_table(ACTIONS) as batch:
        batch.alter_column("owner_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("due_date", existing_type=sa.Date(), nullable=False)
