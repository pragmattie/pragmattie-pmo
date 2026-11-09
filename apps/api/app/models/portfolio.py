"""Clients and their projects: the portfolio the rest of the product hangs off.

`seed_key` marks a row made by `python -m app.seed`; rows people create leave it empty. That is
how `--reset` clears only seeded rows. A project with no client is internal to the practice.

A project's `version` guards gate decisions (item 5.4): SQLAlchemy adds it to every UPDATE
(`version_id_col`), so a decision that lost a race fails. A project declined at the go-ahead gate
is `cancelled`, and takes no further plan, proposal or gate writes.

A client is `simulated` (item 6.8) exactly when its contact email ends in `.example`: the
simulated client (`app.simulated_client`) answers only its proposals.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    column,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.migrations_filter import table_name
from app.models.base import Base, utcnow

CLIENTS_TABLE = table_name("clients")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

# Working value sets until the stage gates (milestone P4) and status reports define them.
STAGES = ("intake", "planning", "delivery", "closure", "closed")
STATUSES = ("on_track", "at_risk", "off_track", "on_hold", "complete", "cancelled")
CANCELLED = "cancelled"
# The reserved demo domain: an address in it never reaches a real person.
DEMO_DOMAIN = ".example"


def is_demo_email(email: str | None) -> bool:
    """Whether `email` is in the reserved demo domain, so its client is simulated (item 6.8)."""
    return bool(email) and email.strip().lower().endswith(DEMO_DOMAIN)


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
    # Derived from `contact_email`, never set directly: see `_derive_simulated` (item 6.8).
    simulated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @validates("contact_email")
    def _derive_simulated(self, _key: str, email: str | None) -> str | None:
        """A client is simulated exactly when its contact email is in the demo domain; setting
        the email (on create or on any change) sets the flag again."""
        self.simulated = is_demo_email(email)
        return email


class Project(Base):
    __tablename__ = PROJECTS_TABLE
    __table_args__ = (
        UniqueConstraint("seed_key", name="uq_projects_seed_key"),
        Index("ix_projects_created_at", "created_at"),
        CheckConstraint(column("status").in_(STATUSES), name="ck_projects_status"),
        # Audit rows name a project by id, and a demo reset makes the seeded projects again, so
        # an id is never reused. MySQL never reuses one; SQLite needs AUTOINCREMENT for that.
        {"sqlite_autoincrement": True},
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
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __mapper_args__ = {"version_id_col": version}


# The stages in which the orchestrator works on a project and its risks, proposals and changes.
LIVE_STAGES = ("planning", "delivery")


def is_live(project: Project | None) -> bool:
    """Whether the project is live: not cancelled, and in planning or delivery. The orchestrator
    and the simulated client (item 6.8) both act only on a live project."""
    return project is not None and project.status != CANCELLED and project.stage in LIVE_STAGES
