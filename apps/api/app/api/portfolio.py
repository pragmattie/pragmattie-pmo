"""Read-only lists of clients and projects. Both need `project:read`."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authz import Action, require
from app.db import get_db
from app.models import Client, Project
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page, paginate
from app.schemas.portfolio import ClientOut, ProjectOut

router = APIRouter(tags=["portfolio"], dependencies=[Depends(require(Action.PROJECT_READ))])


@router.get("/clients", response_model=Page[ClientOut])
def list_clients(
    db: Annotated[Session, Depends(get_db)], limit: Limit = DEFAULT_LIMIT, offset: Offset = 0
) -> Page[ClientOut]:
    # By name; the primary key breaks ties so pages are stable.
    rows, total = paginate(
        db, select(Client), order_by=(Client.name, Client.id), limit=limit, offset=offset
    )
    return Page[ClientOut](
        items=[ClientOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/projects", response_model=Page[ProjectOut])
def list_projects(
    db: Annotated[Session, Depends(get_db)],
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[ProjectOut]:
    rows, total = paginate(
        db, select(Project), order_by=(Project.name, Project.id), limit=limit, offset=offset
    )
    return Page[ProjectOut](
        items=[ProjectOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
