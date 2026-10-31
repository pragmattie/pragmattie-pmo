"""Write and read the append-only audit trail.

There is deliberately no update or delete here: a correction is a new row recorded with
`supersedes_id` set to the row it corrects.
"""

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditDecision
from app.schemas.common import paginate

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


def decisions_query(
    *,
    agent: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    status: str | None = None,
    tier: str | None = None,
) -> Select:
    """Audit rows matching the given filters (unordered; page it with `ORDER_BY`)."""
    filters = {
        AuditDecision.agent: agent,
        AuditDecision.subject_type: subject_type,
        AuditDecision.subject_id: subject_id,
        AuditDecision.status: status,
        AuditDecision.tier: tier,
    }
    stmt = select(AuditDecision)
    for column, value in filters.items():
        if value is not None:
            stmt = stmt.where(column == value)
    return stmt


def list_decisions(
    db: Session, *, limit: int, offset: int = 0, **filters: str | None
) -> list[AuditDecision]:
    rows, _ = paginate(
        db, decisions_query(**filters), order_by=ORDER_BY, limit=limit, offset=offset
    )
    return rows


def count_decisions(db: Session, **filters: str | None) -> int:
    stmt = decisions_query(**filters).subquery()
    return db.scalar(select(func.count()).select_from(stmt)) or 0
