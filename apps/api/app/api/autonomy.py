"""Serve the autonomy policy read-only (item 6.1). Needs `project:read`.

For each step: its mode as the file sets it and its effective mode after the agents' off switches,
its agent, who decides or may hold it, and its window, so the inbox and the board can show
"agent", "agent, 15-minute window" or "person". Also each agent's off switch and the file's
digest. One small document, so it is returned whole rather than paged. Until a person applies the
policy file, the route answers 503.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app import autonomy
from app.authz import Action, require
from app.policy import PolicyError
from app.schemas.autonomy import AgentSwitchOut, AutonomyOut, StepOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["autonomy"], dependencies=[Depends(require(Action.PROJECT_READ))])

UNAVAILABLE = (
    "the autonomy policy isn't available yet: a person needs to apply the autonomy policy file"
)


@router.get("/autonomy", response_model=AutonomyOut)
def read_autonomy() -> AutonomyOut:
    try:
        policy = autonomy.get_autonomy()
    except PolicyError:
        logger.exception("autonomy policy failed to load")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None
    return AutonomyOut(
        digest=policy.digest,
        steps=[
            StepOut(
                id=step.id,
                mode=step.mode,
                effective_mode=autonomy.effective_mode(policy, step.id),
                agent=step.agent,
                who=step.who,
                window_minutes=step.window_minutes,
                person_only=step.id in autonomy.FLOOR_STEPS,
            )
            for step in policy.steps.values()
        ],
        agents=[AgentSwitchOut(name=name, enabled=on) for name, on in policy.enabled.items()],
    )
