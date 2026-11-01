"""Manage users. Only `user:admin` (the admin role) may change a role.

Nobody can change their own role, and no change may leave the system without an active admin.
Each change is one audit row, written in the same transaction as the change itself: agent
`auth`, trigger `human`, subject the target user, actor the admin, and the old and new roles.
Setting a role a user already has changes nothing and writes no row.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import user_out
from app.audit import record_decision
from app.authz import Action, Role, require
from app.db import get_db
from app.models import AuditDecision, User
from app.schemas.auth import UserOut
from app.schemas.users import RoleChangeIn

router = APIRouter(prefix="/users", tags=["users"])

AUDIT_AGENT = "auth"
AUDIT_SUBJECT = "user"
OWN_ROLE = "you cannot change your own role"
LAST_ADMIN = "this change would leave no active admin"


def _next_attempt(db: Session, subject_id: str) -> int:
    latest = db.scalar(
        select(func.max(AuditDecision.attempt)).where(
            AuditDecision.agent == AUDIT_AGENT,
            AuditDecision.subject_type == AUDIT_SUBJECT,
            AuditDecision.subject_id == subject_id,
            AuditDecision.version == 1,
        )
    )
    return (latest or 0) + 1


def _active_admin_ids(db: Session) -> set[int]:
    """Every active admin. On MySQL the rows stay locked until commit, so when two admins demote
    each other at once, the second waits, sees the first change and is refused."""
    stmt = select(User.id).where(User.role == Role.ADMIN, User.is_active.is_(True))
    return set(db.scalars(stmt.with_for_update()).all())


@router.patch("/{user_id}/role", response_model=UserOut)
def change_role(
    user_id: int,
    body: RoleChangeIn,
    admin: Annotated[User, Depends(require(Action.USER_ADMIN))],
    db: Annotated[Session, Depends(get_db)],
) -> UserOut:
    if user_id == admin.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, OWN_ROLE)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    old, new = target.role, body.role.value
    if old == new:
        return user_out(target)
    if old == Role.ADMIN and not _active_admin_ids(db) - {target.id}:
        raise HTTPException(status.HTTP_409_CONFLICT, LAST_ADMIN)

    target.role = new
    subject_id = str(target.id)
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=AUDIT_SUBJECT,
        subject_id=subject_id,
        version=1,
        attempt=_next_attempt(db, subject_id),
        status="ok",
        actor_user_id=admin.id,
        action_taken={"action": "role_change", "from": old, "to": new},
    )
    try:
        db.commit()
    except IntegrityError:
        # Another change to the same user took this attempt number first.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "the user changed; try again") from None
    return user_out(target)
