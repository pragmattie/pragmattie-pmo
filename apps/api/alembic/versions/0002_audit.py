"""Audit: the append-only pmo_audit table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = table_name("pmo_audit")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tier", sa.String(length=8), nullable=True),
        sa.Column("signals", sa.JSON(), nullable=True),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("action_taken", sa.JSON(), nullable=True),
        sa.Column("human_override", sa.JSON(), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # Expressions, not strings, so each dialect quotes the reserved word `trigger`.
        sa.CheckConstraint(
            sa.column("trigger").in_(("agent", "human", "schedule", "trial")),
            name="ck_pmo_audit_trigger",
        ),
        sa.CheckConstraint(
            sa.column("status").in_(("ok", "error", "rejected", "skipped")),
            name="ck_pmo_audit_status",
        ),
        sa.ForeignKeyConstraint(["supersedes_id"], [f"{TABLE}.id"], name="fk_pmo_audit_supersedes"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent", "subject_type", "subject_id", "version", "attempt", name="uq_pmo_audit_attempt"
        ),
    )
    op.create_index("ix_pmo_audit_created_at", TABLE, ["created_at"])
    op.create_index("ix_pmo_audit_status", TABLE, ["status"])
    op.create_index("ix_pmo_audit_subject", TABLE, ["subject_type", "subject_id"])
    op.create_index(f"ix_{TABLE}_supersedes_id", TABLE, ["supersedes_id"])
    op.create_index("ix_pmo_audit_tier", TABLE, ["tier"])


def downgrade() -> None:
    # On MySQL, drop the table rather than its indexes (foreign-key indexes can't go first).
    op.drop_table(TABLE)
