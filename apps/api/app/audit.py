"""Write and read the append-only audit trail.

There is deliberately no update or delete here: a correction is a new row recorded with
`supersedes_id` set to the row it corrects.
"""

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditDecision

_FIELDS = frozenset(AuditDecision.__table__.columns.keys()) - {"id", "created_at"}

# Newest first; the primary key breaks ties so pages are stable.
ORDER_BY = (AuditDecision.created_at.desc(), AuditDecision.id.desc())


def record_decision(db: Session, **fields: Any) -> AuditDecision:
    """Add one audit row and flush it. The caller owns the transaction: nothing is committed
    here, so a rollback removes the row along with the caller's other work."""
    unknown = set(fields) - _FIELDS
    if unknown:
        raise TypeError(f"unknown audit fields: {', '.join(sorted(unknown))}")
    row = AuditDecision(**fields)
    db.add(row)
    db.flush()
    return row


def _filtered(
    stmt: Select,
    *,
    agent: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    status: str | None = None,
    tier: str | None = None,
) -> Select:
    filters = {
        AuditDecision.agent: agent,
        AuditDecision.subject_type: subject_type,
        AuditDecision.subject_id: subject_id,
        AuditDecision.status: status,
        AuditDecision.tier: tier,
    }
    for column, value in filters.items():
        if value is not None:
            stmt = stmt.where(column == value)
    return stmt


def list_decisions(
    db: Session, *, limit: int, offset: int = 0, **filters: str | None
) -> list[AuditDecision]:
    stmt = _filtered(select(AuditDecision), **filters)
    return list(db.scalars(stmt.order_by(*ORDER_BY).limit(limit).offset(offset)))


def count_decisions(db: Session, **filters: str | None) -> int:
    return db.scalar(_filtered(select(func.count(AuditDecision.id)), **filters)) or 0
