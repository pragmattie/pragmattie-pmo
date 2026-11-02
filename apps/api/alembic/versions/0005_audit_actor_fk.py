"""Audit actor foreign key, and the lockout window start on users.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIT = table_name("pmo_audit")
USERS = table_name("users")
ACTOR_INDEX = f"ix_{AUDIT}_actor_user_id"


def upgrade() -> None:
    op.add_column(USERS, sa.Column("failed_window_started_at", sa.DateTime(), nullable=True))
    # Batch mode, so SQLite (which can't add a foreign key in place) rebuilds the table.
    with op.batch_alter_table(AUDIT) as batch:
        batch.create_index(ACTOR_INDEX, ["actor_user_id"])
        batch.create_foreign_key("fk_pmo_audit_actor_user", USERS, ["actor_user_id"], ["id"])


def downgrade() -> None:
    # On MySQL the foreign key must go before the index it uses.
    with op.batch_alter_table(AUDIT) as batch:
        batch.drop_constraint("fk_pmo_audit_actor_user", type_="foreignkey")
    with op.batch_alter_table(AUDIT) as batch:
        batch.drop_index(ACTOR_INDEX)
    with op.batch_alter_table(USERS) as batch:
        batch.drop_column("failed_window_started_at")
