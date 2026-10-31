"""Read the audit trail. Role restriction arrives with permissions in item 2.2."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.audit import ORDER_BY, decisions_query
from app.db import get_db
from app.schemas.audit import AuditDecisionOut
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page, paginate

router = APIRouter(tags=["audit"])


@router.get("/audit", response_model=Page[AuditDecisionOut])
def get_audit(
    db: Annotated[Session, Depends(get_db)],
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
    agent: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    status: str | None = None,
    tier: str | None = None,
) -> Page[AuditDecisionOut]:
    stmt = decisions_query(
        agent=agent, subject_type=subject_type, subject_id=subject_id, status=status, tier=tier
    )
    rows, total = paginate(db, stmt, order_by=ORDER_BY, limit=limit, offset=offset)
    return Page[AuditDecisionOut](
        items=[AuditDecisionOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
