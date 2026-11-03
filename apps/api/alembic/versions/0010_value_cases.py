"""Value cases: why a project request is worth doing, how we will know, and what it will cost.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VALUE_CASES = table_name("value_cases")
BENEFITS = table_name("value_case_benefits")
REQUESTS = table_name("project_requests")
USERS = table_name("users")


def upgrade() -> None:
    op.create_table(
        VALUE_CASES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("cost_low", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("cost_high", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("cost_method", sa.String(length=2000), nullable=True),
        sa.Column("strategic_fit", sa.String(length=2000), nullable=False),
        sa.Column("cost_of_not_doing", sa.String(length=2000), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "cost_low IS NULL OR cost_high IS NULL OR cost_low <= cost_high",
            name="ck_value_cases_cost",
        ),
        sa.CheckConstraint("source IN ('person', 'agent')", name="ck_value_cases_source"),
        sa.ForeignKeyConstraint(["request_id"], [f"{REQUESTS}.id"], name="fk_value_cases_request"),
        sa.ForeignKeyConstraint(
            ["confirmed_by"], [f"{USERS}.id"], name="fk_value_cases_confirmed_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_value_cases_request"),
    )
    op.create_index(f"ix_{VALUE_CASES}_confirmed_by", VALUE_CASES, ["confirmed_by"])
    op.create_table(
        BENEFITS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("value_case_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("measure", sa.String(length=500), nullable=True),
        sa.Column("current_value", sa.String(length=200), nullable=True),
        sa.Column("target", sa.String(length=200), nullable=True),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(
            ["value_case_id"], [f"{VALUE_CASES}.id"], name="fk_value_case_benefits_value_case"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("value_case_id", "position", name="uq_value_case_benefits_position"),
    )


def downgrade() -> None:
    # On MySQL, drop the tables rather than their indexes (foreign-key indexes can't go first).
    op.drop_table(BENEFITS)
    op.drop_table(VALUE_CASES)
