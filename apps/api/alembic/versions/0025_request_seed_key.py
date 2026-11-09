"""A seed key on project requests, so the demo seed can find its own requests again (item 6.9);
project ids never reused.

- `project_requests.seed_key`: set only on a request `python -m app.seed` made, unique, as on
  clients, projects and risks.
- `projects` is rebuilt on SQLite with AUTOINCREMENT: a reset deletes the seeded projects and
  makes them again, and audit rows (an agent's run, for instance) name a project by id, so a new
  project must never take an old one's id. MySQL ignores the option and never reuses an id anyway.

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUESTS = table_name("project_requests")
PROJECTS = table_name("projects")
KEEP_IDS = {"sqlite_autoincrement": True}
UNIQUE = "uq_project_requests_seed_key"


def upgrade() -> None:
    with op.batch_alter_table(REQUESTS, table_kwargs=KEEP_IDS) as batch:
        batch.add_column(sa.Column("seed_key", sa.String(length=64), nullable=True))
        batch.create_unique_constraint(UNIQUE, ["seed_key"])
    # Rebuild only on SQLite: MySQL never reuses an id, and the option means nothing there.
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(PROJECTS, table_kwargs=KEEP_IDS, recreate="always"):
            pass


def downgrade() -> None:
    # The unique key is not a foreign-key index, so it can go before its column on MySQL. The
    # projects table keeps AUTOINCREMENT: never reusing an id is harmless.
    with op.batch_alter_table(REQUESTS, table_kwargs=KEEP_IDS) as batch:
        batch.drop_constraint(UNIQUE, type_="unique")
        batch.drop_column("seed_key")
