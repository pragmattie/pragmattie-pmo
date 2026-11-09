"""The risk agent's suggested score (item 6.7).

`risks` gets the risk agent's suggestion, kept apart from the real score: a suggested probability
and impact, a one-sentence reason for each, the model that made it and the audit row of the run.
All are empty until the agent scores the risk; the real `probability` and `impact` stay empty
until a person or the orchestrator's window applies the suggestion.

None of the new columns is indexed, so the downgrade only drops them.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RISKS = table_name("risks")
KEEP_IDS = {"sqlite_autoincrement": True}
COLUMNS = (
    "suggested_probability",
    "suggested_impact",
    "suggested_probability_reason",
    "suggested_impact_reason",
    "suggested_model",
    "suggested_audit_id",
)


def upgrade() -> None:
    with op.batch_alter_table(RISKS, table_kwargs=KEEP_IDS) as batch:
        batch.add_column(sa.Column("suggested_probability", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("suggested_impact", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("suggested_probability_reason", sa.String(length=500), nullable=True)
        )
        batch.add_column(sa.Column("suggested_impact_reason", sa.String(length=500), nullable=True))
        batch.add_column(sa.Column("suggested_model", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("suggested_audit_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(RISKS, table_kwargs=KEEP_IDS) as batch:
        for name in COLUMNS:
            batch.drop_column(name)
