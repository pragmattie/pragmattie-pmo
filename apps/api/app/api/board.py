"""The portfolio board's route (item 6.4).

`GET /board` (`project:read`) is every request and project as a card in a column by stage, with
what each is waiting on from the orchestrator's next steps and a summary, computed fresh from
the existing tables on each read (`app.board`). Filters: `client_id`, `project_manager`,
`waiting` (`person` or `agent`), and `show_stopped` for declined, withdrawn and cancelled items.

It is one view of the whole portfolio, not a list, so it is not paged: every column is in a
stable order (requests, then projects, each by id). It decides and changes nothing.
"""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import board, policy
from app.authz import Action, require
from app.config import get_settings
from app.db import get_db
from app.models import User
from app.risks import risk_policy
from app.schemas.board import (
    BoardOut,
    CardOut,
    ClientChoiceOut,
    ColumnOut,
    SummaryOut,
    WaitingOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/board", tags=["board"])

UNAVAILABLE = (
    "the board isn't available yet: a person needs to apply the autonomy and risk policy files"
)

Reader = Annotated[User, Depends(require(Action.PROJECT_READ))]
Db = Annotated[Session, Depends(get_db)]


def _card(card: board.Card) -> CardOut:
    fields = {k: v for k, v in vars(card).items() if k not in ("waiting", "open_medium_high_risks")}
    return CardOut(
        **fields,
        key=card.key,
        held_by=card.held_by,
        simulated=card.simulated,
        waiting=[WaitingOut(**vars(w)) for w in card.waiting],
    )


@router.get("", response_model=BoardOut)
def get_board(
    _user: Reader,
    db: Db,
    client_id: Annotated[int | None, Query(ge=1)] = None,
    project_manager: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    waiting: Literal["person", "agent"] | None = None,
    show_stopped: bool = False,
) -> BoardOut:
    filters = board.Filters(client_id, project_manager, waiting, show_stopped)
    try:
        found = board.board(
            db,
            policy.get_autonomy(),
            filters,
            agent_mode=get_settings().agent_mode,
            risk_rules=risk_policy(),
        )
    except policy.PolicyError:
        logger.exception("a policy needed by the board failed to load")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None
    summary = found.summary
    return BoardOut(
        columns=[
            ColumnOut(id=column, title=title, cards=[_card(c) for c in found.columns[column]])
            for column, title in board.COLUMNS.items()
        ],
        summary=SummaryOut(**vars(summary)),
        clients=[ClientChoiceOut(id=i, name=n) for i, n in found.clients],
        project_managers=found.project_managers,
    )
