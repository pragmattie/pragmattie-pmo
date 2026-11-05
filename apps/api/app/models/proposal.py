"""A client project's proposal: the offer the client sees (item 5.2).

- `ProjectProposal`: one per client project. The text sections (summary, scope in and out,
  assumptions, terms) are written by the project manager. The price, the estimate lines, the
  milestones and the deliverables are copied from the plan by code (`app.proposals`) when the
  proposal is created or refreshed, never typed; so are the risks the project manager ticked to
  show in the proposal (title and response only, never a score or band). `plan_version_seen` is
  the plan's version at the last refresh.
- `ProposalVersion`: the frozen copy each submit stores, numbered from 1.
- `ProposalEvent`: what a person did with a version: the sponsor approved or returned it, a
  person marked it sent (to whom, on what date, by what means) or recorded the client's answer,
  or someone withdrew it. Marking sent records what a person did outside the product; nothing
  is sent from here.

`version` guards concurrent edits as on the plan: SQLAlchemy adds it to every UPDATE.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    column,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, Money, utcnow
from app.models.intake import LONG_TEXT_MAX

PROPOSALS_TABLE = table_name("project_proposals")
VERSIONS_TABLE = table_name("proposal_versions")
EVENTS_TABLE = table_name("proposal_events")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

STATUSES = ("draft", "submitted", "approved", "sent", "accepted", "declined", "withdrawn")
EVENT_KINDS = ("approve", "return", "sent", "answer", "withdraw")
ANSWERS = ("accepted", "declined")
TEXT_SECTIONS = ("summary", "scope_in", "scope_out", "assumptions", "terms")
SHORT_TEXT_MAX = 200


class ProjectProposal(Base):
    __tablename__ = PROPOSALS_TABLE
    __table_args__ = (
        CheckConstraint(column("status").in_(STATUSES), name="ck_project_proposals_status"),
        UniqueConstraint("project_id", name="uq_project_proposals_project"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_project_proposals_project")
    )
    status: Mapped[str] = mapped_column(String(16), default="draft")
    summary: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    scope_in: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    scope_out: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    assumptions: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    terms: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    # Copied from the plan by code: never written by a client.
    price: Mapped[Decimal] = mapped_column(Money)
    estimate_lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    milestones: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    deliverables: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    risks: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    plan_version_seen: Mapped[int] = mapped_column()
    created_by: Mapped[int] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_project_proposals_created_by"), index=True
    )
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class ProposalVersion(Base):
    __tablename__ = VERSIONS_TABLE
    __table_args__ = (
        UniqueConstraint("proposal_id", "number", name="uq_proposal_versions_number"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROPOSALS_TABLE}.id", name="fk_proposal_versions_proposal")
    )
    number: Mapped[int] = mapped_column()
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    submitted_by: Mapped[int] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_proposal_versions_submitted_by"), index=True
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProposalEvent(Base):
    __tablename__ = EVENTS_TABLE
    __table_args__ = (
        CheckConstraint(column("kind").in_(EVENT_KINDS), name="ck_proposal_events_kind"),
        CheckConstraint(column("answer").in_(ANSWERS), name="ck_proposal_events_answer"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROPOSALS_TABLE}.id", name="fk_proposal_events_proposal"), index=True
    )
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{VERSIONS_TABLE}.id", name="fk_proposal_events_version"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_proposal_events_actor"), index=True
    )
    reason: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    sent_to: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    sent_on: Mapped[date | None] = mapped_column(Date)
    means: Mapped[str | None] = mapped_column(String(SHORT_TEXT_MAX))
    answer: Mapped[str | None] = mapped_column(String(16))
    answered_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
