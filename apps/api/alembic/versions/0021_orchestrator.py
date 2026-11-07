"""The orchestrator (item 6.2).

Three new tables: the orchestrator's pending actions, the inbox items it makes for people, and
the lease a pass takes so two passes never overlap. The audit table's `trigger` check also
allows `orchestrator`; a CHECK can't be changed in place, so it is dropped and added again. The
downgrade restores the old check, so it fails while any `orchestrator` audit row exists: audit
rows are never deleted.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIONS = table_name("orchestrator_actions")
INBOX = table_name("inbox_items")
LEASES = table_name("orchestrator_leases")
AUDIT = table_name("pmo_audit")
USERS = table_name("users")

OLD_TRIGGERS = ("agent", "human", "schedule", "trial")
NEW_TRIGGERS = (*OLD_TRIGGERS, "orchestrator")


def _trigger_check(batch, triggers: tuple[str, ...]) -> None:
    batch.drop_constraint("ck_pmo_audit_trigger", type_="check")
    # An expression, not a string, so each dialect quotes the reserved word `trigger`.
    batch.create_check_constraint("ck_pmo_audit_trigger", sa.column("trigger").in_(triggers))


def upgrade() -> None:
    op.create_table(
        ACTIONS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("step", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=True),
        sa.Column("who", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_try_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("wait_reason", sa.String(length=500), nullable=True),
        sa.Column("held_by", sa.Integer(), nullable=True),
        sa.Column("hold_reason", sa.String(length=2000), nullable=True),
        sa.Column("held_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("closed_reason", sa.String(length=500), nullable=True),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("open_key", sa.String(length=200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'held', 'cancelled')",
            name="ck_orchestrator_actions_status",
        ),
        sa.CheckConstraint("mode IN ('agent', 'window')", name="ck_orchestrator_actions_mode"),
        sa.ForeignKeyConstraint(
            ["held_by"], [f"{USERS}.id"], name="fk_orchestrator_actions_held_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("open_key", name="uq_orchestrator_actions_open_key"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{ACTIONS}_held_by", ACTIONS, ["held_by"])
    op.create_index("ix_orchestrator_actions_status", ACTIONS, ["status"])
    op.create_index("ix_orchestrator_actions_subject", ACTIONS, ["subject_type", "subject_id"])
    op.create_table(
        INBOX,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("step", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("who", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("handoff", sa.Boolean(), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("closed_reason", sa.String(length=500), nullable=True),
        sa.Column("policy_digest", sa.String(length=64), nullable=False),
        sa.Column("open_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'done', 'cancelled')", name="ck_inbox_items_status"),
        sa.ForeignKeyConstraint(["action_id"], [f"{ACTIONS}.id"], name="fk_inbox_items_action"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("open_key", name="uq_inbox_items_open_key"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{INBOX}_action_id", INBOX, ["action_id"])
    op.create_index("ix_inbox_items_subject", INBOX, ["subject_type", "subject_id"])
    op.create_index("ix_inbox_items_who_status", INBOX, ["who", "status"])
    op.create_table(
        LEASES,
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("holder", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    with op.batch_alter_table(AUDIT) as batch:
        _trigger_check(batch, NEW_TRIGGERS)


def downgrade() -> None:
    with op.batch_alter_table(AUDIT) as batch:
        _trigger_check(batch, OLD_TRIGGERS)
    # On MySQL, drop the tables rather than their indexes (foreign-key indexes can't go first).
    op.drop_table(LEASES)
    op.drop_table(INBOX)
    op.drop_table(ACTIONS)
