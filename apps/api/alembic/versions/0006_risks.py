"""The risk register.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RISKS = table_name("risks")
PROJECTS = table_name("projects")
USERS = table_name("users")


def upgrade() -> None:
    op.create_table(
        RISKS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column("effect", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("objectives", sa.JSON(), nullable=False),
        sa.Column("probability", sa.Integer(), nullable=True),
        sa.Column("impact", sa.Integer(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("band", sa.String(length=32), nullable=True),
        sa.Column("response", sa.String(length=16), nullable=False),
        sa.Column("response_plan", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("needs_judgement", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('threat', 'opportunity')", name="ck_risks_kind"),
        sa.CheckConstraint("category IN ('ai', 'project')", name="ck_risks_category"),
        sa.CheckConstraint(
            "response IN ('avoid', 'reduce', 'share', 'accept', 'pursue', 'increase')",
            name="ck_risks_response",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'open', 'accepted', 'closed', 'rejected')",
            name="ck_risks_status",
        ),
        sa.CheckConstraint("source IN ('person', 'agent')", name="ck_risks_source"),
        sa.CheckConstraint("probability BETWEEN 1 AND 5", name="ck_risks_probability"),
        sa.CheckConstraint("impact BETWEEN 1 AND 5", name="ck_risks_impact"),
        sa.CheckConstraint("score BETWEEN 1 AND 25", name="ck_risks_score"),
        sa.ForeignKeyConstraint(["project_id"], [f"{PROJECTS}.id"], name="fk_risks_project"),
        sa.ForeignKeyConstraint(["owner_user_id"], [f"{USERS}.id"], name="fk_risks_owner_user"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_risks_project_status", RISKS, ["project_id", "status"])
    op.create_index("ix_risks_band", RISKS, ["band"])
    op.create_index("ix_risks_score", RISKS, ["score"])
    op.create_index(f"ix_{RISKS}_owner_user_id", RISKS, ["owner_user_id"])


def downgrade() -> None:
    # On MySQL, drop the table rather than its indexes (foreign-key indexes can't go first).
    op.drop_table(RISKS)
