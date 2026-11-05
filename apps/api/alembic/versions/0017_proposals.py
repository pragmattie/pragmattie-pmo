"""A client project's proposal, its frozen versions and what people did with them (item 5.2).

Adds `risks.show_in_proposal`, off for every existing risk.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROPOSALS = table_name("project_proposals")
VERSIONS = table_name("proposal_versions")
EVENTS = table_name("proposal_events")
PROJECTS = table_name("projects")
USERS = table_name("users")
RISKS = table_name("risks")


def upgrade() -> None:
    op.create_table(
        PROPOSALS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.String(length=2000), nullable=True),
        sa.Column("scope_in", sa.String(length=2000), nullable=True),
        sa.Column("scope_out", sa.String(length=2000), nullable=True),
        sa.Column("assumptions", sa.String(length=2000), nullable=True),
        sa.Column("terms", sa.String(length=2000), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("estimate_lines", sa.JSON(), nullable=False),
        sa.Column("milestones", sa.JSON(), nullable=False),
        sa.Column("deliverables", sa.JSON(), nullable=False),
        sa.Column("risks", sa.JSON(), nullable=False),
        sa.Column("plan_version_seen", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'sent', 'accepted', 'declined', "
            "'withdrawn')",
            name="ck_project_proposals_status",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_project_proposals_project"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], [f"{USERS}.id"], name="fk_project_proposals_created_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", name="uq_project_proposals_project"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{PROPOSALS}_created_by", PROPOSALS, ["created_by"])
    op.create_table(
        VERSIONS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("submitted_by", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"], [f"{PROPOSALS}.id"], name="fk_proposal_versions_proposal"
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by"], [f"{USERS}.id"], name="fk_proposal_versions_submitted_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proposal_id", "number", name="uq_proposal_versions_number"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{VERSIONS}_submitted_by", VERSIONS, ["submitted_by"])
    op.create_table(
        EVENTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=True),
        sa.Column("sent_to", sa.String(length=200), nullable=True),
        sa.Column("sent_on", sa.Date(), nullable=True),
        sa.Column("means", sa.String(length=200), nullable=True),
        sa.Column("answer", sa.String(length=16), nullable=True),
        sa.Column("answered_on", sa.Date(), nullable=True),
        sa.Column("note", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('approve', 'return', 'sent', 'answer', 'withdraw')",
            name="ck_proposal_events_kind",
        ),
        sa.CheckConstraint("answer IN ('accepted', 'declined')", name="ck_proposal_events_answer"),
        sa.ForeignKeyConstraint(
            ["proposal_id"], [f"{PROPOSALS}.id"], name="fk_proposal_events_proposal"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"], [f"{VERSIONS}.id"], name="fk_proposal_events_version"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], [f"{USERS}.id"], name="fk_proposal_events_actor"
        ),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{EVENTS}_proposal_id", EVENTS, ["proposal_id"])
    op.create_index(f"ix_{EVENTS}_version_id", EVENTS, ["version_id"])
    op.create_index(f"ix_{EVENTS}_actor_user_id", EVENTS, ["actor_user_id"])
    # Batch mode keeps SQLite's AUTOINCREMENT when it rebuilds the table.
    with op.batch_alter_table(RISKS, table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.add_column(
            sa.Column("show_in_proposal", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    # On MySQL, drop the tables rather than their indexes (foreign-key indexes can't go first).
    op.drop_table(EVENTS)
    op.drop_table(VERSIONS)
    op.drop_table(PROPOSALS)
    with op.batch_alter_table(RISKS, table_kwargs={"sqlite_autoincrement": True}) as batch:
        batch.drop_column("show_in_proposal")
