"""Clients and their projects: the portfolio the rest of the product hangs off.

`seed_key` marks a row made by `python -m app.seed`; rows people create leave it empty. That is
how `--reset` clears only seeded rows. A project with no client is internal to the practice.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, utcnow

CLIENTS_TABLE = table_name("clients")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

# Working value sets until the stage gates (milestone P4) and status reports define them.
STAGES = ("intake", "planning", "delivery", "closure", "closed")
STATUSES = ("on_track", "at_risk", "off_track", "on_hold", "complete")


class Client(Base):
    __tablename__ = CLIENTS_TABLE
    __table_args__ = (UniqueConstraint("seed_key", name="uq_clients_seed_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    industry: Mapped[str | None] = mapped_column(String(100))
    website: Mapped[str | None] = mapped_column(String(255))
    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    seed_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Project(Base):
    __tablename__ = PROJECTS_TABLE
    __table_args__ = (
        UniqueConstraint("seed_key", name="uq_projects_seed_key"),
        Index("ix_projects_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{CLIENTS_TABLE}.id", name="fk_projects_client"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    stage: Mapped[str] = mapped_column(String(32))
    sponsor: Mapped[str] = mapped_column(String(200))
    sponsor_email: Mapped[str | None] = mapped_column(String(255))
    pm: Mapped[str] = mapped_column(String(200))
    pm_email: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    # The users behind `sponsor` and `pm`, for a project made from a request (item 4.6). Seeded
    # and older projects name people only as text.
    sponsor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_projects_sponsor_user"), index=True
    )
    pm_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_projects_pm_user"), index=True
    )
    seed_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
