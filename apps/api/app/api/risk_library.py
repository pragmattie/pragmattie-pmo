"""The starter risk library, read-only: `GET /api/v1/risk-library` (`project:read`).

The templates come from the risk library policy file (`app.policy.load_risk_library`), in the
file's order, so pages are stable. If the file can't be loaded the answer is a 503. Adding
templates to a project is `POST /api/v1/projects/{id}/risks/from-library` in `app.api.risks`.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app import risks
from app.authz import Action, require
from app.policy import PolicyError, RiskLibrary
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page
from app.schemas.risk import RiskTemplateOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["risks"], dependencies=[Depends(require(Action.PROJECT_READ))])

LIBRARY_UNAVAILABLE = "the risk library could not be loaded; try later"


def library() -> RiskLibrary:
    """The risk library, or a 503 when its file can't be loaded."""
    try:
        return risks.risk_library()
    except PolicyError:
        logger.exception("risk library failed to load")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, LIBRARY_UNAVAILABLE) from None


@router.get("/risk-library", response_model=Page[RiskTemplateOut])
def list_templates(
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[RiskTemplateOut]:
    templates = list(library().templates.values())
    return Page[RiskTemplateOut](
        items=[RiskTemplateOut.model_validate(t) for t in templates[offset : offset + limit]],
        total=len(templates),
        limit=limit,
        offset=offset,
    )
