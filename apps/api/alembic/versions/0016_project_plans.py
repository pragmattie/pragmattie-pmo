"""A project's plan: scope, deliverables, milestones, estimate and reporting plan (item 5.1).

Every existing project gets an empty plan, with scope in and out copied from its brief if it has
one.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-04
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLANS = table_name("project_plans")
MILESTONES = table_name("plan_milestones")
DELIVERABLES = table_name("plan_deliverables")
LINES = table_name("plan_estimate_lines")
PROJECTS = table_name("projects")
BRIEFS = table_name("project_briefs")
USERS = table_name("users")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def upgrade() -> None:
    plans = op.create_table(
        PLANS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("scope_in", sa.String(length=2000), nullable=True),
        sa.Column("scope_out", sa.String(length=2000), nullable=True),
        sa.Column("contingency_pct", sa.Integer(), nullable=False),
        sa.Column("report_period", sa.String(length=16), nullable=True),
        sa.Column("report_due_day", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("contingency_pct BETWEEN 0 AND 50", name="ck_project_plans_contingency"),
        sa.CheckConstraint(
            "report_period IN ('weekly', 'fortnightly', 'monthly')",
            name="ck_project_plans_report_period",
        ),
        sa.CheckConstraint(
            "report_due_day BETWEEN 1 AND 28", name="ck_project_plans_report_due_day"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_project_plans_project"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_project_plans_project"),
    )
    op.create_table(
        MILESTONES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("contributors", sa.String(length=500), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["plan_id"], [f"{PLANS}.id"], name="fk_plan_milestones_plan"),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], [f"{USERS}.id"], name="fk_plan_milestones_owner"
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{MILESTONES}_plan_id", MILESTONES, ["plan_id"])
    op.create_index(f"ix_{MILESTONES}_owner_user_id", MILESTONES, ["owner_user_id"])
    op.create_table(
        DELIVERABLES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("milestone_id", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["plan_id"], [f"{PLANS}.id"], name="fk_plan_deliverables_plan"),
        sa.ForeignKeyConstraint(
            ["milestone_id"], [f"{MILESTONES}.id"], name="fk_plan_deliverables_milestone"
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{DELIVERABLES}_plan_id", DELIVERABLES, ["plan_id"])
    op.create_index(f"ix_{DELIVERABLES}_milestone_id", DELIVERABLES, ["milestone_id"])
    op.create_table(
        LINES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "category IN ('people', 'software', 'services', 'other')",
            name="ck_plan_estimate_lines_category",
        ),
        sa.CheckConstraint("amount > 0", name="ck_plan_estimate_lines_amount"),
        sa.ForeignKeyConstraint(["plan_id"], [f"{PLANS}.id"], name="fk_plan_estimate_lines_plan"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{LINES}_plan_id", LINES, ["plan_id"])
    _backfill(plans)


def _backfill(plans: sa.Table) -> None:
    """An empty plan for every existing project, its scope copied from the brief if any."""
    projects = sa.table(PROJECTS, sa.column("id", sa.Integer()))
    briefs = sa.table(
        BRIEFS,
        sa.column("project_id", sa.Integer()),
        sa.column("scope_in", sa.String()),
        sa.column("scope_out", sa.String()),
    )
    rows = op.get_bind().execute(
        sa.select(projects.c.id, briefs.c.scope_in, briefs.c.scope_out)
        .select_from(projects.outerjoin(briefs, briefs.c.project_id == projects.c.id))
        .order_by(projects.c.id)
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    values = [
        {
            "project_id": project_id,
            "scope_in": scope_in,
            "scope_out": scope_out,
            "contingency_pct": 0,
            "report_period": None,
            "report_due_day": None,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }
        for project_id, scope_in, scope_out in rows
    ]
    if values:
        op.bulk_insert(plans, values)


def downgrade() -> None:
    # On MySQL, drop the tables rather than their indexes (foreign-key indexes can't go first).
    op.drop_table(LINES)
    op.drop_table(DELIVERABLES)
    op.drop_table(MILESTONES)
    op.drop_table(PLANS)
