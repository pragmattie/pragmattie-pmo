"""A seed key on risks, so the demo seed can find its own risks again; risk ids never reused.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RISKS = table_name("risks")


def upgrade() -> None:
    # Batch mode, so SQLite (which can't add a constraint in place) rebuilds the table, this time
    # with AUTOINCREMENT: audit rows name a risk by id, so an id must never be reused. MySQL
    # ignores the option and never reuses an id anyway.
    with op.batch_alter_table(RISKS, table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.add_column(sa.Column("seed_key", sa.String(length=64), nullable=True))
        batch.create_unique_constraint("uq_risks_seed_key", ["seed_key"])


def downgrade() -> None:
    # The unique key is not a foreign-key index, so it can go before its column on MySQL.
    with op.batch_alter_table(RISKS) as batch:
        batch.drop_constraint("uq_risks_seed_key", type_="unique")
        batch.drop_column("seed_key")
