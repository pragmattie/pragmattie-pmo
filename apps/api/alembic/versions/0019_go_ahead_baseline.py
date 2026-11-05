"""The go-ahead gate and the baseline (item 5.4).

Projects get a `version` (every existing project starts at 1) and a check constraint on `status`,
which now includes `cancelled`. A new table holds each project's baselines: frozen copies of its
plan.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECTS = table_name("projects")
BASELINES = table_name("project_baselines")
GATE_DECISIONS = table_name("gate_decisions")
STATUSES = "'on_track', 'at_risk', 'off_track', 'on_hold', 'complete', 'cancelled'"


def upgrade() -> None:
    with op.batch_alter_table(PROJECTS) as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=True))
    projects = sa.table(PROJECTS, sa.column("version", sa.Integer()))
    op.get_bind().execute(projects.update().values(version=1))
    with op.batch_alter_table(PROJECTS) as batch:
        batch.alter_column("version", existing_type=sa.Integer(), nullable=False)
        batch.create_check_constraint("ck_projects_status", f"status IN ({STATUSES})")
    op.create_table(
        BASELINES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("gate_decision_id", sa.Integer(), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_project_baselines_project"
        ),
        sa.ForeignKeyConstraint(
            ["gate_decision_id"],
            [f"{GATE_DECISIONS}.id"],
            name="fk_project_baselines_gate_decision",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "number", name="uq_project_baselines_number"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{BASELINES}_gate_decision_id", BASELINES, ["gate_decision_id"])


def downgrade() -> None:
    # On MySQL, drop the table rather than its indexes (foreign-key indexes can't go first).
    op.drop_table(BASELINES)
    with op.batch_alter_table(PROJECTS) as batch:
        batch.drop_constraint("ck_projects_status", type_="check")
        batch.drop_column("version")
