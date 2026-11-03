"""The records planning starts from: a project's brief, stakeholders and assumptions (item 4.6).

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BRIEFS = table_name("project_briefs")
STAKEHOLDERS = table_name("project_stakeholders")
ASSUMPTIONS = table_name("project_assumptions")
PROJECTS = table_name("projects")
USERS = table_name("users")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        BRIEFS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=2000), nullable=False),
        sa.Column("objectives", sa.String(length=2000), nullable=False),
        sa.Column("scope_in", sa.String(length=2000), nullable=True),
        sa.Column("scope_out", sa.String(length=2000), nullable=True),
        sa.Column("wanted_by", sa.Date(), nullable=True),
        sa.Column("budget_low", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("budget_high", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("decision_rights", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "budget_low IS NULL OR budget_high IS NULL OR budget_low <= budget_high",
            name="ck_project_briefs_budget",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_project_briefs_project"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_project_briefs_project"),
    )
    op.create_table(
        STAKEHOLDERS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("role_or_interest", sa.String(length=200), nullable=False),
        sa.Column("needs_to_hear", sa.String(length=500), nullable=True),
        sa.Column("how_often", sa.String(length=100), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_project_stakeholders_project"
        ),
        sa.ForeignKeyConstraint(["user_id"], [f"{USERS}.id"], name="fk_project_stakeholders_user"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{STAKEHOLDERS}_project_id", STAKEHOLDERS, ["project_id"])
    op.create_index(f"ix_{STAKEHOLDERS}_user_id", STAKEHOLDERS, ["user_id"])
    op.create_table(
        ASSUMPTIONS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(length=500), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("confirm_by", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('open', 'confirmed', 'wrong')", name="ck_project_assumptions_status"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_project_assumptions_project"
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], [f"{USERS}.id"], name="fk_project_assumptions_owner"
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{ASSUMPTIONS}_project_id", ASSUMPTIONS, ["project_id"])
    op.create_index(f"ix_{ASSUMPTIONS}_owner_user_id", ASSUMPTIONS, ["owner_user_id"])


def downgrade() -> None:
    # On MySQL, drop the tables rather than their indexes (foreign-key indexes can't go first).
    op.drop_table(ASSUMPTIONS)
    op.drop_table(STAKEHOLDERS)
    op.drop_table(BRIEFS)
