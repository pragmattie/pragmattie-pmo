"""Gate decisions: a person's pass, return or decline at a stage gate (item 4.4).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DECISIONS = table_name("gate_decisions")
USERS = table_name("users")


def upgrade() -> None:
    op.create_table(
        DECISIONS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gate_id", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("decided_by", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('pass', 'return', 'decline')", name="ck_gate_decisions_outcome"
        ),
        sa.CheckConstraint(
            "subject_type IN ('request', 'project')", name="ck_gate_decisions_subject_type"
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], [f"{USERS}.id"], name="fk_gate_decisions_decided_by"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(f"ix_{DECISIONS}_decided_by", DECISIONS, ["decided_by"])
    op.create_index("ix_gate_decisions_subject", DECISIONS, ["subject_type", "subject_id"])


def downgrade() -> None:
    # On MySQL, drop the table rather than its indexes (foreign-key indexes can't go first).
    op.drop_table(DECISIONS)
