"""Readiness checks: whether a project request that uses AI is ready (item 4.3).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKS = table_name("readiness_checks")
ACTIONS = table_name("readiness_actions")
REQUESTS = table_name("project_requests")
USERS = table_name("users")

AREAS = ("problem", "approach", "data")


def _rating_check(area: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{area}_rating IS NULL OR {area}_rating IN ('green', 'yellow', 'red')",
        name=f"ck_readiness_checks_{area}_rating",
    )


def upgrade() -> None:
    op.create_table(
        CHECKS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("problem_rating", sa.String(length=8), nullable=True),
        sa.Column("problem_note", sa.String(length=2000), nullable=True),
        sa.Column("approach_rating", sa.String(length=8), nullable=True),
        sa.Column("approach_note", sa.String(length=2000), nullable=True),
        sa.Column("data_rating", sa.String(length=8), nullable=True),
        sa.Column("data_note", sa.String(length=2000), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        *(_rating_check(area) for area in AREAS),
        sa.CheckConstraint("source IN ('person', 'agent')", name="ck_readiness_checks_source"),
        sa.ForeignKeyConstraint(
            ["request_id"], [f"{REQUESTS}.id"], name="fk_readiness_checks_request"
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by"], [f"{USERS}.id"], name="fk_readiness_checks_confirmed_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_readiness_checks_request"),
    )
    op.create_index(f"ix_{CHECKS}_confirmed_by", CHECKS, ["confirmed_by"])
    op.create_table(
        ACTIONS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("check_id", sa.Integer(), nullable=False),
        sa.Column("area", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.CheckConstraint(
            "area IN ('problem', 'approach', 'data')", name="ck_readiness_actions_area"
        ),
        sa.ForeignKeyConstraint(["check_id"], [f"{CHECKS}.id"], name="fk_readiness_actions_check"),
        sa.ForeignKeyConstraint(["owner_id"], [f"{USERS}.id"], name="fk_readiness_actions_owner"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("check_id", "area", "position", name="uq_readiness_actions_position"),
    )
    op.create_index(f"ix_{ACTIONS}_owner_id", ACTIONS, ["owner_id"])


def downgrade() -> None:
    # On MySQL, drop the tables rather than their indexes (foreign-key indexes can't go first).
    op.drop_table(ACTIONS)
    op.drop_table(CHECKS)
