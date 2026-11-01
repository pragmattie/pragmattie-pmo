"""Clients and projects: the portfolio tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLIENTS = table_name("clients")
PROJECTS = table_name("projects")


def upgrade() -> None:
    op.create_table(
        CLIENTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("industry", sa.String(length=100), nullable=True),
        sa.Column("website", sa.String(length=255), nullable=True),
        sa.Column("contact_name", sa.String(length=200), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("seed_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seed_key", name="uq_clients_seed_key"),
    )
    op.create_table(
        PROJECTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("sponsor", sa.String(length=200), nullable=False),
        sa.Column("sponsor_email", sa.String(length=255), nullable=True),
        sa.Column("pm", sa.String(length=200), nullable=False),
        sa.Column("pm_email", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("seed_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], [f"{CLIENTS}.id"], name="fk_projects_client"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seed_key", name="uq_projects_seed_key"),
    )
    op.create_index(f"ix_{PROJECTS}_client_id", PROJECTS, ["client_id"])
    op.create_index("ix_projects_created_at", PROJECTS, ["created_at"])


def downgrade() -> None:
    # On MySQL, drop the tables rather than their indexes (foreign-key indexes can't go first).
    op.drop_table(PROJECTS)
    op.drop_table(CLIENTS)
