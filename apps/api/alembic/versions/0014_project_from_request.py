"""A project made from a request: its sponsor and project manager as users, and the request's link.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.migrations_filter import table_name

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECTS = table_name("projects")
REQUESTS = table_name("project_requests")
USERS = table_name("users")
KEEP_IDS = {"sqlite_autoincrement": True}


def upgrade() -> None:
    # Batch mode, so SQLite (which can't add a foreign key in place) rebuilds the table.
    with op.batch_alter_table(PROJECTS) as batch:
        batch.add_column(sa.Column("sponsor_user_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pm_user_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_projects_sponsor_user", USERS, ["sponsor_user_id"], ["id"])
        batch.create_foreign_key("fk_projects_pm_user", USERS, ["pm_user_id"], ["id"])
        batch.create_index(f"ix_{PROJECTS}_sponsor_user_id", ["sponsor_user_id"])
        batch.create_index(f"ix_{PROJECTS}_pm_user_id", ["pm_user_id"])
    # Request ids are never reused (audit rows name them), so a rebuild keeps AUTOINCREMENT.
    with op.batch_alter_table(REQUESTS, table_kwargs=KEEP_IDS) as batch:
        batch.add_column(sa.Column("project_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_project_requests_project", PROJECTS, ["project_id"], ["id"])
        batch.create_unique_constraint("uq_project_requests_project", ["project_id"])


def downgrade() -> None:
    # Drop each foreign key, then its column (which takes the column's index with it on MySQL).
    with op.batch_alter_table(REQUESTS, table_kwargs=KEEP_IDS) as batch:
        batch.drop_constraint("fk_project_requests_project", type_="foreignkey")
        batch.drop_constraint("uq_project_requests_project", type_="unique")
        batch.drop_column("project_id")
    with op.batch_alter_table(PROJECTS) as batch:
        batch.drop_constraint("fk_projects_pm_user", type_="foreignkey")
        batch.drop_constraint("fk_projects_sponsor_user", type_="foreignkey")
        batch.drop_index(f"ix_{PROJECTS}_pm_user_id")
        batch.drop_index(f"ix_{PROJECTS}_sponsor_user_id")
        batch.drop_column("pm_user_id")
        batch.drop_column("sponsor_user_id")
