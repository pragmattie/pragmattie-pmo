"""Change requests (item 5.5).

A new table holds each project's change requests. A baseline is now made either by a gate
decision (baseline 1, as before) or by an approved change request: `project_baselines` gets
`change_request_id`, `gate_decision_id` becomes nullable, and a check requires exactly one of the
two. Every existing baseline keeps its gate decision, so it passes the check.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHANGES = table_name("change_requests")
BASELINES = table_name("project_baselines")
PROJECTS = table_name("projects")
USERS = table_name("users")
MADE_BY_ONE = (
    "(gate_decision_id IS NOT NULL AND change_request_id IS NULL) OR "
    "(gate_decision_id IS NULL AND change_request_id IS NOT NULL)"
)


def upgrade() -> None:
    op.create_table(
        CHANGES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("effect", sa.JSON(), nullable=False),
        sa.Column("decider", sa.String(length=32), nullable=False),
        sa.Column("measured_baseline_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("raised_by", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.Column("decision_reason", sa.String(length=2000), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'rejected', 'withdrawn')",
            name="ck_change_requests_status",
        ),
        sa.CheckConstraint(
            "decider IN ('project_manager', 'sponsor')", name="ck_change_requests_decider"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_change_requests_project"
        ),
        sa.ForeignKeyConstraint(
            ["raised_by"], [f"{USERS}.id"], name="fk_change_requests_raised_by"
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], [f"{USERS}.id"], name="fk_change_requests_decided_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{CHANGES}_project_id", CHANGES, ["project_id"])
    op.create_index(f"ix_{CHANGES}_raised_by", CHANGES, ["raised_by"])
    op.create_index(f"ix_{CHANGES}_decided_by", CHANGES, ["decided_by"])
    with op.batch_alter_table(BASELINES) as batch:
        batch.add_column(sa.Column("change_request_id", sa.Integer(), nullable=True))
        batch.alter_column("gate_decision_id", existing_type=sa.Integer(), nullable=True)
        batch.create_unique_constraint("uq_project_baselines_change_request", ["change_request_id"])
        batch.create_foreign_key(
            "fk_project_baselines_change_request", CHANGES, ["change_request_id"], ["id"]
        )
        batch.create_check_constraint("ck_project_baselines_made_by", MADE_BY_ONE)


def downgrade() -> None:
    # Baselines made by a change request have no gate decision, so they can't survive the
    # downgrade; they go with the change requests that made them.
    baselines = sa.table(BASELINES, sa.column("change_request_id", sa.Integer()))
    op.get_bind().execute(baselines.delete().where(baselines.c.change_request_id.is_not(None)))
    with op.batch_alter_table(BASELINES) as batch:
        # The foreign key goes first: on MySQL it holds the unique index it uses.
        batch.drop_constraint("fk_project_baselines_change_request", type_="foreignkey")
        batch.drop_constraint("ck_project_baselines_made_by", type_="check")
        batch.drop_constraint("uq_project_baselines_change_request", type_="unique")
        batch.drop_column("change_request_id")
        batch.alter_column("gate_decision_id", existing_type=sa.Integer(), nullable=False)
    # On MySQL, drop the table rather than its indexes (foreign-key indexes can't go first).
    op.drop_table(CHANGES)
