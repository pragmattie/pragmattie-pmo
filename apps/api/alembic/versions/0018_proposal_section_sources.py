"""Who wrote each section of a proposal: an agent or a person (item 5.3).

Every existing non-empty text section is marked as a person's, so nothing written before the
proposal agent counts as an agent's draft.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROPOSALS = table_name("project_proposals")
SECTIONS = ("summary", "scope_in", "scope_out", "assumptions", "terms")
KEEP_IDS = {"sqlite_autoincrement": True}


def upgrade() -> None:
    with op.batch_alter_table(PROPOSALS, table_kwargs=KEEP_IDS) as batch:
        batch.add_column(sa.Column("section_sources", sa.JSON(), nullable=True))
    proposals = sa.table(
        PROPOSALS,
        sa.column("id", sa.Integer()),
        sa.column("section_sources", sa.JSON()),
        *(sa.column(name, sa.String()) for name in SECTIONS),
    )
    bind = op.get_bind()
    for row in bind.execute(sa.select(proposals)).mappings().all():
        sources = {
            name: {"source": "person", "reviewed_by": None, "reviewed_at": None}
            for name in SECTIONS
            if (row[name] or "").strip()
        }
        bind.execute(
            proposals.update().where(proposals.c.id == row["id"]).values(section_sources=sources)
        )
    with op.batch_alter_table(PROPOSALS, table_kwargs=KEEP_IDS) as batch:
        batch.alter_column("section_sources", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table(PROPOSALS, table_kwargs=KEEP_IDS) as batch:
        batch.drop_column("section_sources")
