"""Who wrote each section of a status report: the status report agent or a person (item 7.4).

`section_sources` is added, as on proposals (0018). Every existing non-empty text section is
marked as a person's, so nothing written before the status report agent counts as its draft.
`created_by` becomes nullable: the orchestrator creates a period's draft with no person.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPORTS = table_name("status_reports")
SECTIONS = ("summary", "done", "next", "issues", "decisions")
KEEP_IDS = {"sqlite_autoincrement": True}


def upgrade() -> None:
    with op.batch_alter_table(REPORTS, table_kwargs=KEEP_IDS) as batch:
        batch.add_column(sa.Column("section_sources", sa.JSON(), nullable=True))
        batch.alter_column("created_by", existing_type=sa.Integer(), nullable=True)
    reports = sa.table(
        REPORTS,
        sa.column("id", sa.Integer()),
        sa.column("sections", sa.JSON()),
        sa.column("section_sources", sa.JSON()),
    )
    bind = op.get_bind()
    for row in bind.execute(sa.select(reports.c.id, reports.c.sections)).mappings().all():
        sections = row["sections"] or {}
        sources = {
            name: {"source": "person", "reviewed_by": None, "reviewed_at": None}
            for name in SECTIONS
            if (sections.get(name) or "").strip()
        }
        bind.execute(
            reports.update().where(reports.c.id == row["id"]).values(section_sources=sources)
        )
    with op.batch_alter_table(REPORTS, table_kwargs=KEEP_IDS) as batch:
        batch.alter_column("section_sources", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    left = op.get_bind().scalar(sa.text(f"SELECT COUNT(*) FROM {REPORTS} WHERE created_by IS NULL"))
    if left:
        raise RuntimeError(
            f"{left} status report(s) were made by the orchestrator and have no person; a person "
            "must decide what to do with them before this downgrade"
        )
    with op.batch_alter_table(REPORTS, table_kwargs=KEEP_IDS) as batch:
        batch.drop_column("section_sources")
        batch.alter_column("created_by", existing_type=sa.Integer(), nullable=False)
