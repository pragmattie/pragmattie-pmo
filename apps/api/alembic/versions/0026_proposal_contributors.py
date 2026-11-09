"""Who contributed to each submitted copy of a proposal (item 6.10).

- `proposal_versions.contributors`: the user ids of everyone who helped make the copy, sorted:
  the person who submitted it (none when the orchestrator did) and everyone recorded as
  `reviewed_by` on one of its sections when it was submitted. Nobody on the list may approve or
  return the copy. Existing copies get their submitter only (`[]` when there was none): who
  reviewed their sections then was not kept.

The column is added nullable, filled, then made NOT NULL, since MySQL gives a JSON column no
literal default. It is not indexed, so the downgrade only drops it.

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VERSIONS = table_name("proposal_versions")
KEEP_IDS = {"sqlite_autoincrement": True}


def upgrade() -> None:
    with op.batch_alter_table(VERSIONS, table_kwargs=KEEP_IDS) as batch:
        batch.add_column(sa.Column("contributors", sa.JSON(), nullable=True))
    versions = sa.table(
        VERSIONS,
        sa.column("id", sa.Integer),
        sa.column("submitted_by", sa.Integer),
        sa.column("contributors", sa.JSON),
    )
    conn = op.get_bind()
    for row in conn.execute(sa.select(versions.c.id, versions.c.submitted_by)).all():
        contributors = [] if row.submitted_by is None else [row.submitted_by]
        conn.execute(
            versions.update().where(versions.c.id == row.id).values(contributors=contributors)
        )
    with op.batch_alter_table(VERSIONS, table_kwargs=KEEP_IDS) as batch:
        batch.alter_column("contributors", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table(VERSIONS, table_kwargs=KEEP_IDS) as batch:
        batch.drop_column("contributors")
