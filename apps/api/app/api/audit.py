"""Read the audit trail. Needs `audit:read`."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import ORDER_BY, decisions_query
from app.authz import Action, require
from app.db import get_db
from app.models import User
from app.schemas.audit import AuditDecisionOut
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page, paginate

router = APIRouter(tags=["audit"], dependencies=[Depends(require(Action.AUDIT_READ))])


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
    actor_user_id: int | None = None,
) -> Page[AuditDecisionOut]:
    stmt = decisions_query(
        agent=agent,
        subject_type=subject_type,
        subject_id=subject_id,
        status=status,
        tier=tier,
        actor_user_id=actor_user_id,
    )
    rows, total = paginate(db, stmt, order_by=ORDER_BY, limit=limit, offset=offset)
    # Each person's display name, so a reader of the log need not read the users list.
    actor_ids = {row.actor_user_id for row in rows if row.actor_user_id is not None}
    names = (
        dict(db.execute(select(User.id, User.display_name).where(User.id.in_(actor_ids))).all())
        if actor_ids
        else {}
    )
    items = []
    for row in rows:
        item = AuditDecisionOut.model_validate(row)
        if row.actor_user_id is not None:
            item.actor_name = names.get(row.actor_user_id)
        items.append(item)
    return Page[AuditDecisionOut](items=items, total=total, limit=limit, offset=offset)
