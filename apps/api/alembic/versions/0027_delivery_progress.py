"""Delivery progress (item 7.2).

- `plan_milestones` gains `reached_on`, `reached_note` and `reached_version`: when the milestone
  was reached and a note on it. `reached_version` is its own counter, so marking a milestone
  reached never looks like a plan edit (the row's `version` stays as it was).
- `plan_deliverables` gains `status` (`not_started` by default, checked) and `status_version`.
- `project_spend` is new and append-only: spend to date for a project, the date it is true for
  and a note on its source. A correction is a new entry whose `supersedes_id` names the one it
  corrects.

On MySQL the downgrade drops the new table rather than its indexes (foreign-key indexes can't be
dropped first), then the check and the added columns.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MILESTONES = table_name("plan_milestones")
DELIVERABLES = table_name("plan_deliverables")
SPEND = table_name("project_spend")
PROJECTS = table_name("projects")
USERS = table_name("users")
KEEP_IDS = {"sqlite_autoincrement": True}
STATUS_CHECK = (
    "status IN ('not_started', 'in_progress', 'ready_for_acceptance', 'accepted')"
)


def upgrade() -> None:
    with op.batch_alter_table(MILESTONES, table_kwargs=KEEP_IDS) as batch:
        batch.add_column(sa.Column("reached_on", sa.Date(), nullable=True))
        batch.add_column(sa.Column("reached_note", sa.String(length=500), nullable=True))
        batch.add_column(
            sa.Column("reached_version", sa.Integer(), nullable=False, server_default="1")
        )
    with op.batch_alter_table(DELIVERABLES, table_kwargs=KEEP_IDS) as batch:
        batch.add_column(
            sa.Column(
                "status", sa.String(length=32), nullable=False, server_default="not_started"
            )
        )
        batch.add_column(
            sa.Column("status_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.create_check_constraint("ck_plan_deliverables_status", STATUS_CHECK)
    op.create_table(
        SPEND,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=False),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_project_spend_amount"),
        sa.ForeignKeyConstraint(["project_id"], [f"{PROJECTS}.id"], name="fk_project_spend_project"),
        sa.ForeignKeyConstraint(
            ["supersedes_id"], [f"{SPEND}.id"], name="fk_project_spend_supersedes"
        ),
        sa.ForeignKeyConstraint(["created_by"], [f"{USERS}.id"], name="fk_project_spend_created_by"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{SPEND}_project_id", SPEND, ["project_id"])
    op.create_index(f"ix_{SPEND}_supersedes_id", SPEND, ["supersedes_id"])
    op.create_index(f"ix_{SPEND}_created_by", SPEND, ["created_by"])


def downgrade() -> None:
    op.drop_table(SPEND)
    with op.batch_alter_table(DELIVERABLES, table_kwargs=KEEP_IDS) as batch:
        # The check goes first: MySQL won't drop a column a check still names.
        batch.drop_constraint("ck_plan_deliverables_status", type_="check")
        batch.drop_column("status_version")
        batch.drop_column("status")
    with op.batch_alter_table(MILESTONES, table_kwargs=KEEP_IDS) as batch:
        batch.drop_column("reached_version")
        batch.drop_column("reached_note")
        batch.drop_column("reached_on")
