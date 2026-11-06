"""The orchestrator's routes (item 6.2).

- `POST /orchestrator/run` runs one pass on demand. Admin only (`orchestrator:run`); it does what
  `python -m app.orchestrator once` does, under the same mode, caps and lease.
- `POST /orchestrator/actions/{action_id}/hold` lets a person hold a pending action, so it never
  runs and its step goes to their inbox (R2). Only the step's `who` role may hold it, with a
  reason of at least 10 characters; every refusal by a signed-in person is an audit row.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import orchestrator, policy
from app.agents import risk_agent
from app.audit import record_refusal
from app.authz import Action, require, require_any_audited
from app.config import get_settings
from app.db import get_db
from app.models import OrchestratorAction, User
from app.schemas.orchestrator import ActionOut, HoldIn, HoldOut, OutcomeOut, PassOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])

UNAVAILABLE = (
    "the autonomy policy isn't available yet: a person needs to apply the autonomy policy file"
)
NOT_FOUND = "orchestrator action not found"
NOT_PENDING = "only a pending action can be held"
WRONG_ROLE = "only the {who} may hold this step"
SHORT_REASON = f"a hold needs a reason of at least {orchestrator.MIN_HOLD_REASON} characters"
CHANGED = "the action changed while you were holding it; reload it and try again"


def get_claude_client() -> risk_agent.ClaudeClient:
    """The orchestrator's Claude client; tests replace it through `dependency_overrides`."""
    return risk_agent.AnthropicClient(get_settings())


@router.post("/run", response_model=PassOut)
def run(
    _user: Annotated[User, Depends(require(Action.ORCHESTRATOR_RUN))],
    db: Annotated[Session, Depends(get_db)],
    claude: Annotated[risk_agent.ClaudeClient, Depends(get_claude_client)],
) -> PassOut:
    try:
        report = orchestrator.run_pass(db, get_settings(), claude)
    except policy.PolicyError:
        logger.exception("autonomy policy failed to load")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None
    return PassOut(
        mode=report.mode,
        ran=report.ran,
        busy=report.busy,
        steps=report.steps,
        agent_calls=report.agent_calls,
        outcomes=[OutcomeOut(**vars(o)) for o in report.outcomes],
    )


def _subject(db: Session, action_id: int) -> tuple[str, str]:
    """The audit subject for a hold on `action_id`: the step's subject, or the action itself
    when there is no such action."""
    action = db.get(OrchestratorAction, action_id)
    if action is None:
        return "orchestrator_action", str(action_id)
    return action.subject_type, action.subject_id


def _refuse(db: Session, user: User, action_id: int, refusal: str, **extra: Any) -> None:
    """Record a refused hold and commit it, so the refusal survives the error response."""
    subject_type, subject_id = _subject(db, action_id)
    record_refusal(
        db,
        agent=orchestrator.ORCHESTRATOR,
        subject_type=subject_type,
        subject_id=subject_id,
        version=1,
        actor_user_id=user.id,
        output=None,
        action_taken={
            "action": "hold_refused",
            "refusal": refusal,
            "action_id": action_id,
            "actor_role": user.role,
            **extra,
        },
    )
    db.commit()


def _record_forbidden(db: Session, user: User, params: dict[str, str]) -> None:
    try:
        action_id = int(params.get("action_id", ""))
    except ValueError:
        return
    _refuse(db, user, action_id, "forbidden")


# Any role that can be a step's `who` (it holds the step's permission, `app.autonomy`); the
# route then checks it is this step's `who`.
WHO_ACTIONS = frozenset(
    {Action.PROJECT_WRITE, Action.GATE_DECIDE, Action.PROPOSAL_APPROVE, Action.RISK_ACCEPT}
)
Holder = Annotated[User, Depends(require_any_audited(WHO_ACTIONS, _record_forbidden))]


@router.post("/actions/{action_id}/hold", response_model=HoldOut)
def hold_action(
    action_id: int, body: HoldIn, user: Holder, db: Annotated[Session, Depends(get_db)]
) -> HoldOut:
    action = db.get(OrchestratorAction, action_id, with_for_update=True, populate_existing=True)
    if action is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if user.role != action.who:
        db.rollback()
        _refuse(db, user, action_id, "wrong_role", who=action.who)
        raise HTTPException(status.HTTP_403_FORBIDDEN, WRONG_ROLE.format(who=action.who))
    reason = body.reason.strip()
    if len(reason) < orchestrator.MIN_HOLD_REASON:
        db.rollback()
        _refuse(db, user, action_id, "short_reason")
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, SHORT_REASON)
    if action.status != "pending":
        db.rollback()
        _refuse(db, user, action_id, "not_pending", status=action.status)
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_PENDING)
    item = orchestrator.hold(db, action, user_id=user.id, role=user.role, reason=reason)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, CHANGED) from None
    return HoldOut(action=ActionOut.model_validate(action), inbox_item_id=item.id)
