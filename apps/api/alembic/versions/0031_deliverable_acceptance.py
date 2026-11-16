"""Deliverable acceptance (item 7.6).

`deliverable_acceptances` is new and append-only: each offer of a deliverable to the client (to
whom, on what date, by what means) and each answer to an offer (`accepted` or `not_accepted`, the
day it came, what is missing, a note), with its `source` (`person`, or `simulated` for the
simulated client) and who recorded it. An offer and its answer share a `round`, and
`UNIQUE(deliverable_id, round, kind)` keeps one of each. `deliverable_id` is not a foreign key, so
the history outlives a deliverable an approved change request removes.

On MySQL the downgrade drops the table rather than its indexes (foreign-key indexes can't be
dropped first).

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCEPTANCES = table_name("deliverable_acceptances")
PROJECTS = table_name("projects")
USERS = table_name("users")


def upgrade() -> None:
    op.create_table(
        ACCEPTANCES,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("deliverable_id", sa.Integer(), nullable=False),
        sa.Column("deliverable_name", sa.String(length=200), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("offered_to", sa.String(length=200), nullable=True),
        sa.Column("offered_on", sa.Date(), nullable=True),
        sa.Column("means", sa.String(length=100), nullable=True),
        sa.Column("answer", sa.String(length=16), nullable=True),
        sa.Column("answered_on", sa.Date(), nullable=True),
        sa.Column("missing", sa.String(length=1000), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("recorded_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('offer', 'answer')", name="ck_deliverable_acceptances_kind"),
        sa.CheckConstraint(
            "answer IN ('accepted', 'not_accepted')", name="ck_deliverable_acceptances_answer"
        ),
        sa.CheckConstraint(
            "source IN ('person', 'simulated')", name="ck_deliverable_acceptances_source"
        ),
        sa.CheckConstraint(
            "(kind = 'offer' AND offered_to IS NOT NULL AND offered_on IS NOT NULL "
            "AND means IS NOT NULL AND answer IS NULL) OR "
            "(kind = 'answer' AND answer IS NOT NULL AND answered_on IS NOT NULL)",
            name="ck_deliverable_acceptances_shape",
        ),
        sa.CheckConstraint(
            "recorded_by IS NOT NULL OR (source = 'simulated' AND kind = 'answer')",
            name="ck_deliverable_acceptances_person",
        ),
        sa.CheckConstraint("round >= 1", name="ck_deliverable_acceptances_round"),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{PROJECTS}.id"], name="fk_deliverable_acceptances_project"
        ),
        sa.ForeignKeyConstraint(
            ["recorded_by"], [f"{USERS}.id"], name="fk_deliverable_acceptances_recorded_by"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deliverable_id", "round", "kind", name="uq_deliverable_acceptances_round"
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(f"ix_{ACCEPTANCES}_project_id", ACCEPTANCES, ["project_id"])
    op.create_index(f"ix_{ACCEPTANCES}_recorded_by", ACCEPTANCES, ["recorded_by"])


def downgrade() -> None:
    op.drop_table(ACCEPTANCES)
