"""Project requests: the front door for new work.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUESTS = table_name("project_requests")
CLIENTS = table_name("clients")
USERS = table_name("users")


def upgrade() -> None:
    op.create_table(
        REQUESTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("asked_for", sa.String(length=2000), nullable=False),
        sa.Column("problem", sa.String(length=2000), nullable=False),
        sa.Column("outcome", sa.String(length=2000), nullable=False),
        sa.Column("budget_low", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("budget_high", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("wanted_by", sa.Date(), nullable=True),
        sa.Column("uses_ai", sa.Boolean(), nullable=False),
        sa.Column("known_risks", sa.String(length=2000), nullable=True),
        sa.Column("requested_by", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'declined', 'withdrawn')",
            name="ck_project_requests_status",
        ),
        sa.CheckConstraint(
            "budget_low IS NULL OR budget_high IS NULL OR budget_low <= budget_high",
            name="ck_project_requests_budget",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], [f"{CLIENTS}.id"], name="fk_project_requests_client"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], [f"{USERS}.id"], name="fk_project_requests_requested_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_project_requests_status", REQUESTS, ["status"])
    op.create_index("ix_project_requests_submitted_at", REQUESTS, ["submitted_at"])
    op.create_index("ix_project_requests_wanted_by", REQUESTS, ["wanted_by"])
    op.create_index(f"ix_{REQUESTS}_client_id", REQUESTS, ["client_id"])
    op.create_index(f"ix_{REQUESTS}_requested_by", REQUESTS, ["requested_by"])


def downgrade() -> None:
    # On MySQL, drop the table rather than its indexes (foreign-key indexes can't go first).
    op.drop_table(REQUESTS)
