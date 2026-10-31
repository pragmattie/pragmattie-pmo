"""The append-only audit table: one row per agent or human decision.

Rows are never updated or deleted. A correction is a new row whose `supersedes_id` points at the
row it corrects. `trigger` is a MySQL reserved word: SQLAlchemy quotes it, hand-written SQL must.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    column,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnElement

from app.migrations_filter import table_name
from app.models.base import Base, Money, utcnow

AUDIT_TABLE = table_name("pmo_audit")

TRIGGERS = ("agent", "human", "schedule", "trial")
STATUSES = ("ok", "error", "rejected", "skipped")


def _one_of(name: str, values: tuple[str, ...]) -> ColumnElement[bool]:
    """A CHECK expression; each dialect quotes the column (so `trigger` works on MySQL)."""
    return column(name).in_(values)


class AuditDecision(Base):
    __tablename__ = AUDIT_TABLE
    __table_args__ = (
        UniqueConstraint(
            "agent", "subject_type", "subject_id", "version", "attempt", name="uq_pmo_audit_attempt"
        ),
        CheckConstraint(_one_of("trigger", TRIGGERS), name="ck_pmo_audit_trigger"),
        CheckConstraint(_one_of("status", STATUSES), name="ck_pmo_audit_status"),
        Index("ix_pmo_audit_subject", "subject_type", "subject_id"),
        Index("ix_pmo_audit_status", "status"),
        Index("ix_pmo_audit_tier", "tier"),
        Index("ix_pmo_audit_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(64))
    trigger: Mapped[str] = mapped_column(String(16))
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(default=1)
    attempt: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(16))
    tier: Mapped[str | None] = mapped_column(String(8))
    signals: Mapped[Any | None] = mapped_column(JSON)
    output: Mapped[Any | None] = mapped_column(JSON)
    action_taken: Mapped[Any | None] = mapped_column(JSON)
    human_override: Mapped[Any | None] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    error: Mapped[str | None] = mapped_column(Text)
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{AUDIT_TABLE}.id", name="fk_pmo_audit_supersedes"), index=True
    )
    # The users table arrives in milestone P2; the foreign key is added then.
    actor_user_id: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
