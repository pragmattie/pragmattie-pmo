"""The risk policy's scoring scales, read-only: `GET /api/v1/risk-policy/scales` (`project:read`).

Each probability and impact level from the risk policy file, with its number, label and
definition, so a person scores a risk on labelled levels rather than a bare 1 to 5. An impact
level's definition joins its schedule, cost and quality effects. If the policy can't be loaded the
answer is a 503.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.authz import Action, require
from app.policy import OBJECTIVES, ImpactLevel, PolicyError, RiskPolicy
from app.risks import risk_policy
from app.schemas.risk import RiskScalesOut, ScaleLevelOut

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/risk-policy",
    tags=["risks"],
    dependencies=[Depends(require(Action.PROJECT_READ))],
)

POLICY_UNAVAILABLE = "the risk policy could not be loaded; try later"


def _policy() -> RiskPolicy:
    try:
        return risk_policy()
    except PolicyError:
        logger.exception("risk policy failed to load")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, POLICY_UNAVAILABLE) from None


def _impact_definition(level: ImpactLevel) -> str:
    return " ".join(f"{name.capitalize()}: {getattr(level, name)}" for name in OBJECTIVES)


@router.get("/scales", response_model=RiskScalesOut)
def get_scales() -> RiskScalesOut:
    policy = _policy()
    return RiskScalesOut(
        probability=[
            ScaleLevelOut(level=p.level, label=p.label, definition=p.definition)
            for p in sorted(policy.probability.values(), key=lambda p: p.level)
        ],
        impact=[
            ScaleLevelOut(level=i.level, label=i.label, definition=_impact_definition(i))
            for i in sorted(policy.impact.values(), key=lambda i: i.level)
        ],
    )
