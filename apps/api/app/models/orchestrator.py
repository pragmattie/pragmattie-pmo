"""The orchestrator's own records (item 6.2).

- `OrchestratorAction`: a step the orchestrator has taken on: an agent step (with its tries) or a
  window step waiting for its due time. Status `pending`, then `done`, `held` (a person held the
  window) or `cancelled` (the step is no longer needed, or it went to a person). Who held it,
  why, and when and why it closed are kept on the row.
- `InboxItem`: a step waiting on a person, for the inbox (item 6.3). Never deleted: `open`, then
  `done` or `cancelled`, with when and why it closed. `handoff` marks an item opened only because
  the step's agent couldn't act; it is cancelled when the agent can act again.
- `OrchestratorLease`: one row per lock name. A pass takes the `pass` lease before it starts and
  gives up when another pass holds an unexpired one, so two passes never overlap.

MySQL has no partial unique index, so "at most one pending action, and one open inbox item, per
step and subject" is a nullable unique `open_key`, set only while the row is open and cleared
when it closes (NULLs never clash).
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    column,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, utcnow
from app.models.intake import LONG_TEXT_MAX

ACTIONS_TABLE = table_name("orchestrator_actions")
INBOX_TABLE = table_name("inbox_items")
LEASES_TABLE = table_name("orchestrator_leases")
USERS_TABLE = table_name("users")

ACTION_STATUSES = ("pending", "done", "held", "cancelled")
ACTION_MODES = ("agent", "window")
INBOX_STATUSES = ("open", "done", "cancelled")
STEP_MAX = 64
SUBJECT_TYPE_MAX = 32
SUBJECT_ID_MAX = 64
KEY_MAX = 200
REASON_MAX = 500


def open_key(step: str, subject_type: str, subject_id: str) -> str:
    """The unique key an open row holds: one per step and subject."""
    return f"{step}:{subject_type}:{subject_id}"


class OrchestratorAction(Base):
    __tablename__ = ACTIONS_TABLE
    __table_args__ = (
        CheckConstraint(
            column("status").in_(ACTION_STATUSES), name="ck_orchestrator_actions_status"
        ),
        CheckConstraint(column("mode").in_(ACTION_MODES), name="ck_orchestrator_actions_mode"),
        UniqueConstraint("open_key", name="uq_orchestrator_actions_open_key"),
        Index("ix_orchestrator_actions_subject", "subject_type", "subject_id"),
        Index("ix_orchestrator_actions_status", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    step: Mapped[str] = mapped_column(String(STEP_MAX))
    subject_type: Mapped[str] = mapped_column(String(SUBJECT_TYPE_MAX))
    subject_id: Mapped[str] = mapped_column(String(SUBJECT_ID_MAX))
    mode: Mapped[str] = mapped_column(String(16))
    agent: Mapped[str | None] = mapped_column(String(64))
    who: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(REASON_MAX))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # When a window step may run; for an agent step, when it was first found.
    due_at: Mapped[datetime] = mapped_column(DateTime)
    # Failed tries so far, and when the next one may run.
    attempts: Mapped[int] = mapped_column(default=0)
    next_try_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(REASON_MAX))
    # Why a ready step is still waiting (a cap), so the board can say so.
    wait_reason: Mapped[str | None] = mapped_column(String(REASON_MAX))
    held_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_orchestrator_actions_held_by"), index=True
    )
    hold_reason: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    held_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_reason: Mapped[str | None] = mapped_column(String(REASON_MAX))
    policy_digest: Mapped[str] = mapped_column(String(64))
    open_key: Mapped[str | None] = mapped_column(String(KEY_MAX))
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class InboxItem(Base):
    __tablename__ = INBOX_TABLE
    __table_args__ = (
        CheckConstraint(column("status").in_(INBOX_STATUSES), name="ck_inbox_items_status"),
        UniqueConstraint("open_key", name="uq_inbox_items_open_key"),
        Index("ix_inbox_items_subject", "subject_type", "subject_id"),
        Index("ix_inbox_items_who_status", "who", "status"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    step: Mapped[str] = mapped_column(String(STEP_MAX))
    subject_type: Mapped[str] = mapped_column(String(SUBJECT_TYPE_MAX))
    subject_id: Mapped[str] = mapped_column(String(SUBJECT_ID_MAX))
    # The role the step waits on.
    who: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(REASON_MAX))
    status: Mapped[str] = mapped_column(String(16), default="open")
    # Opened only because the step's agent couldn't act (switched off, not built yet, or
    # AGENT_MODE not `enforce`): a later pass cancels it once the agent can act again.
    handoff: Mapped[bool] = mapped_column(default=False)
    # The action it came from, when a held or failed action made it.
    action_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{ACTIONS_TABLE}.id", name="fk_inbox_items_action"), index=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_reason: Mapped[str | None] = mapped_column(String(REASON_MAX))
    policy_digest: Mapped[str] = mapped_column(String(64))
    open_key: Mapped[str | None] = mapped_column(String(KEY_MAX))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OrchestratorLease(Base):
    __tablename__ = LEASES_TABLE

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    holder: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
