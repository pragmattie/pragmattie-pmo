"""Write and read the append-only audit trail.

There is deliberately no update or delete here: a correction is a new row recorded with
`supersedes_id` set to the row it corrects.
"""

from typing import Any

from sqlalchemy import Select, and_, func, not_, select
from sqlalchemy.orm import Session

from app.models import AuditDecision, User
from app.schemas.common import paginate

_FIELDS = frozenset(AuditDecision.__table__.columns.keys()) - {"id", "created_at"}

# Sign-in and sign-out rows: the `auth` agent's rows about a login. Its other rows (a role change,
# an unlock) are decisions and stay in view when sign-ins are hidden.
SIGN_IN_AGENT = "auth"
SIGN_IN_SUBJECT = "login"

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


def next_attempt(
    db: Session, *, agent: str, subject_type: str, subject_id: str, version: int
) -> int:
    """The next free `attempt` for a subject's version: 1 when it has no row yet."""
    stmt = select(func.max(AuditDecision.attempt)).where(
        AuditDecision.agent == agent,
        AuditDecision.subject_type == subject_type,
        AuditDecision.subject_id == subject_id,
        AuditDecision.version == version,
    )
    return (db.scalar(stmt) or 0) + 1


def record_refusal(
    db: Session,
    *,
    agent: str,
    subject_type: str,
    subject_id: str,
    version: int,
    actor_user_id: int,
    output: Any,
    action_taken: dict[str, Any],
) -> AuditDecision:
    """The audit row for a person's refused decision (`status: rejected`). Nothing about the
    subject changed, so `output` is the subject as it stays. Several refusals of one version each
    get their own `attempt`. The caller commits it before answering with the refusal."""
    return record_decision(
        db,
        agent=agent,
        trigger="human",
        subject_type=subject_type,
        subject_id=subject_id,
        version=version,
        attempt=next_attempt(
            db, agent=agent, subject_type=subject_type, subject_id=subject_id, version=version
        ),
        status="rejected",
        actor_user_id=actor_user_id,
        output=output,
        action_taken=action_taken,
    )


def decisions_query(
    *,
    agent: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    status: str | None = None,
    tier: str | None = None,
    actor_user_id: int | None = None,
    hide_sign_ins: bool = False,
) -> Select:
    """Audit rows matching the given filters (unordered; page it with `ORDER_BY`)."""
    filters = {
        AuditDecision.agent: agent,
        AuditDecision.subject_type: subject_type,
        AuditDecision.subject_id: subject_id,
        AuditDecision.status: status,
        AuditDecision.tier: tier,
        AuditDecision.actor_user_id: actor_user_id,
    }
    stmt = select(AuditDecision)
    for column, value in filters.items():
        if value is not None:
            stmt = stmt.where(column == value)
    if hide_sign_ins:
        stmt = stmt.where(
            not_(
                and_(
                    AuditDecision.agent == SIGN_IN_AGENT,
                    AuditDecision.subject_type == SIGN_IN_SUBJECT,
                )
            )
        )
    return stmt


def facets(db: Session) -> dict[str, list]:
    """The values present in the log, for the audit page's filter lists: each distinct agent,
    subject type and status, sorted, and every person who took a decision (deactivated people
    included), by name."""

    def distinct(column) -> list[str]:
        return list(db.scalars(select(column).distinct().order_by(column)).all())

    people = db.execute(
        select(User.id, User.display_name)
        .where(User.id.in_(select(AuditDecision.actor_user_id).distinct()))
        .order_by(User.display_name, User.id)
    ).all()
    return {
        "agents": distinct(AuditDecision.agent),
        "subject_types": distinct(AuditDecision.subject_type),
        "statuses": distinct(AuditDecision.status),
        "people": [{"id": id_, "display_name": name} for id_, name in people],
    }


def list_decisions(
    db: Session, *, limit: int, offset: int = 0, **filters: str | int | bool | None
) -> list[AuditDecision]:
    rows, _ = paginate(
        db, decisions_query(**filters), order_by=ORDER_BY, limit=limit, offset=offset
    )
    return rows


def count_decisions(db: Session, **filters: str | int | bool | None) -> int:
    stmt = decisions_query(**filters).subquery()
    return db.scalar(select(func.count()).select_from(stmt)) or 0
