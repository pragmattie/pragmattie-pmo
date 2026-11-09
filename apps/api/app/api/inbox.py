"""The decision inbox's routes (item 6.3).

- `GET /inbox` (`project:read`) is the signed-in person's items, oldest first, paged: their role's
  items, narrowed to the projects that name them where a project names someone for that role.
  Each one carries the facts it rests on and a recommendation written by code (`app.inbox`).
- `GET /inbox/count` (`project:read`) is how many there are, for the navigation badge.

Neither decides anything: each item is decided through its step's existing route. An admin or
auditor holds `project:read` but decides nothing, so their inbox is empty, except that a proposal
decision every approver contributed to goes to the admin, to make a non-contributor the decider
(item 6.10).
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import inbox, policy
from app.authz import Action, require
from app.config import get_settings
from app.db import get_db
from app.models import User
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page
from app.schemas.inbox import FactOut, InboxCountOut, InboxEntryOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inbox", tags=["inbox"])

UNAVAILABLE = (
    "the inbox isn't available yet: a person needs to apply the autonomy and risk policy files"
)

Reader = Annotated[User, Depends(require(Action.PROJECT_READ))]
Db = Annotated[Session, Depends(get_db)]


def _entries(db: Session, user: User) -> list[inbox.Entry]:
    try:
        return inbox.entries(db, user, policy.get_autonomy(), agent_mode=get_settings().agent_mode)
    except policy.PolicyError:
        logger.exception("a policy needed by the inbox failed to load")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None


def _out(entry: inbox.Entry) -> InboxEntryOut:
    fields = {k: v for k, v in vars(entry).items() if k != "facts"}
    return InboxEntryOut(**fields, facts=[FactOut(**vars(f)) for f in entry.facts])


@router.get("", response_model=Page[InboxEntryOut])
def list_inbox(
    user: Reader, db: Db, limit: Limit = DEFAULT_LIMIT, offset: Offset = 0
) -> Page[InboxEntryOut]:
    found = _entries(db, user)
    page = inbox.with_case(db, found[offset : offset + limit])
    return Page[InboxEntryOut](
        items=[_out(e) for e in page], total=len(found), limit=limit, offset=offset
    )


@router.get("/count", response_model=InboxCountOut)
def count_inbox(user: Reader, db: Db) -> InboxCountOut:
    return InboxCountOut(count=len(_entries(db, user)))
