"""Who wrote each plan item: the planner agent or a person (item 6.5).

Milestones, deliverables and estimate lines get `source` (`agent` or `person`). Every existing
item is a person's, so a re-draft by the planner agent never replaces one.

The downgrade drops the check constraints first (MySQL refuses to drop a column a check uses),
then the columns.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("plan_milestones", "plan_deliverables", "plan_estimate_lines")
SOURCES = ("agent", "person")
KEEP_IDS = {"sqlite_autoincrement": True}


def upgrade() -> None:
    for name in TABLES:
        with op.batch_alter_table(table_name(name), table_kwargs=KEEP_IDS) as batch:
            batch.add_column(
                sa.Column("source", sa.String(length=16), nullable=False, server_default="person")
            )
            batch.create_check_constraint(f"ck_{name}_source", sa.column("source").in_(SOURCES))


def downgrade() -> None:
    for name in TABLES:
        with op.batch_alter_table(table_name(name), table_kwargs=KEEP_IDS) as batch:
            batch.drop_constraint(f"ck_{name}_source", type_="check")
        with op.batch_alter_table(table_name(name), table_kwargs=KEEP_IDS) as batch:
            batch.drop_column("source")
