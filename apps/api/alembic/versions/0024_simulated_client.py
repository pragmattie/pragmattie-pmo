"""The simulated client and proposal automation (item 6.8).

- `clients.simulated`: true exactly when the client's contact email ends in `.example`, the
  reserved demo domain that never reaches a real person. Backfilled here; the model sets it again
  whenever a client is created or its email changes.
- `proposal_events.source`: `person`, or `simulated` for an answer the simulated client gave.
  `actor_user_id` becomes nullable, and a check allows a null one only on a `simulated` event.
- `project_proposals.created_by` and `proposal_versions.submitted_by` become nullable: a proposal
  the orchestrator created, or a copy it submitted, has no person (its audit row says so).

The downgrade deletes the `source = simulated` events before `actor_user_id` is NOT NULL again
(they have no person to put back). It refuses, changing nothing, while a proposal the
orchestrator created or a copy it submitted remains: a person decides what to do with those.
None of the new columns is indexed, so the downgrade only drops them.

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLIENTS = table_name("clients")
PROPOSALS = table_name("project_proposals")
VERSIONS = table_name("proposal_versions")
EVENTS = table_name("proposal_events")
KEEP_IDS = {"sqlite_autoincrement": True}
SOURCE_CHECK = "ck_proposal_events_source"
ACTOR_CHECK = "ck_proposal_events_actor"


def upgrade() -> None:
    with op.batch_alter_table(CLIENTS) as batch:
        batch.add_column(
            sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    clients = sa.table(CLIENTS, sa.column("simulated", sa.Boolean), sa.column("contact_email"))
    op.execute(
        clients.update()
        .where(sa.func.lower(sa.func.trim(clients.c.contact_email)).like("%.example"))
        .values(simulated=True)
    )
    with op.batch_alter_table(EVENTS, table_kwargs=KEEP_IDS) as batch:
        batch.add_column(
            sa.Column("source", sa.String(length=16), nullable=False, server_default="person")
        )
        batch.alter_column("actor_user_id", existing_type=sa.Integer(), nullable=True)
        batch.create_check_constraint(SOURCE_CHECK, "source IN ('person', 'simulated')")
        batch.create_check_constraint(
            ACTOR_CHECK, "actor_user_id IS NOT NULL OR source = 'simulated'"
        )
    with op.batch_alter_table(PROPOSALS, table_kwargs=KEEP_IDS) as batch:
        batch.alter_column("created_by", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table(VERSIONS, table_kwargs=KEEP_IDS) as batch:
        batch.alter_column("submitted_by", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    conn = op.get_bind()
    left = conn.scalar(
        sa.text(f"SELECT COUNT(*) FROM {PROPOSALS} WHERE created_by IS NULL")
    ) + conn.scalar(sa.text(f"SELECT COUNT(*) FROM {VERSIONS} WHERE submitted_by IS NULL"))
    if left:
        raise RuntimeError(
            f"{left} proposal(s) or submitted copies were made by the orchestrator and have no "
            "person; a person must decide what to do with them before this downgrade"
        )
    op.execute(sa.text(f"DELETE FROM {EVENTS} WHERE source = 'simulated'"))
    with op.batch_alter_table(VERSIONS, table_kwargs=KEEP_IDS) as batch:
        batch.alter_column("submitted_by", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table(PROPOSALS, table_kwargs=KEEP_IDS) as batch:
        batch.alter_column("created_by", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table(EVENTS, table_kwargs=KEEP_IDS) as batch:
        batch.drop_constraint(ACTOR_CHECK, type_="check")
        batch.drop_constraint(SOURCE_CHECK, type_="check")
        batch.alter_column("actor_user_id", existing_type=sa.Integer(), nullable=False)
        batch.drop_column("source")
    with op.batch_alter_table(CLIENTS) as batch:
        batch.drop_column("simulated")
