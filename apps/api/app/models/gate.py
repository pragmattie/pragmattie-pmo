"""Gate decisions: a person's pass, return or decline at a stage gate (item 4.4).

One row per decision, never changed afterwards. `criteria` keeps each criterion's result and note
as they stood when the person decided, and `policy_version` is the SHA-256 of the gates file the
rules came from, so a decision shows exactly what it was measured against.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String, column
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, utcnow
from app.models.intake import LONG_TEXT_MAX

GATE_DECISIONS_TABLE = table_name("gate_decisions")
USERS_TABLE = table_name("users")

OUTCOMES = ("pass", "return", "decline")
SUBJECT_TYPES = ("request", "project")
MIN_REASON = 20
NOTE_MAX = 500


class GateDecision(Base):
    __tablename__ = GATE_DECISIONS_TABLE
    __table_args__ = (
        CheckConstraint(column("outcome").in_(OUTCOMES), name="ck_gate_decisions_outcome"),
        CheckConstraint(
            column("subject_type").in_(SUBJECT_TYPES), name="ck_gate_decisions_subject_type"
        ),
        Index("ix_gate_decisions_subject", "subject_type", "subject_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    gate_id: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[int]
    outcome: Mapped[str] = mapped_column(String(16))
    decided_by: Mapped[int] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_gate_decisions_decided_by"), index=True
    )
    reason: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    criteria: Mapped[Any] = mapped_column(JSON)
    policy_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
