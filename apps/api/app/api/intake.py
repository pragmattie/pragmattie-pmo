"""Project requests and the queue (item 4.1).

Reading needs `project:read`; creating and changing need `project:write`. Any project manager may
edit, submit or withdraw any request (work gets handed over); the audit row records who acted.

- `GET /requests` is the queue: paged, filtered by status, client and `uses_ai`, sorted by
  submitted date or wanted-by date. Requests without that date come last either way, and the id
  breaks ties so pages are stable.
- `POST /requests` creates a `draft`; `requested_by` is the signed-in person, never the client.
- `PATCH /requests/{id}` edits a draft only. Any other status is a 409.
- `POST /requests/{id}/submit` moves a draft to `submitted`; `POST /requests/{id}/withdraw` moves a
  draft or submitted request to `withdrawn`. `declined` and `withdrawn` are final.
- `GET /requests/{id}/history` is the request's own audit rows, newest first.

No route here sets `approved`, `declined` or returns a request to `draft`: that is the approval
gate's job, and a person's decision there. Every change carries the `version` the client read; a
different stored version, or a parallel write that lands first, is a 409. Each create, edit,
submit and withdraw is one audit row (agent `intake`, subject `project_request`) with the values
before and after and the person's id.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.audit import ORDER_BY, decisions_query, record_decision
from app.authz import Action, require
from app.db import get_db
from app.models import AuditDecision, Client, ProjectRequest, User, utcnow
from app.models.intake import FINAL_STATUSES
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page, paginate
from app.schemas.intake import (
    RequestCreate,
    RequestFields,
    RequestHistoryEntry,
    RequestOut,
    RequestStatus,
    RequestTransition,
    RequestUpdate,
)

router = APIRouter(prefix="/requests", tags=["intake"])

AUDIT_AGENT = "intake"
AUDIT_SUBJECT = "project_request"

NOT_FOUND = "project request not found"
STALE = "this request has changed since you read it; reload it and try again"
BAD_CLIENT = "client_id must be an existing client"
FINAL = "a declined or withdrawn request is final and can't be changed"
NOT_DRAFT_EDIT = "only a draft can be edited; this request must be returned to draft first"
NOT_DRAFT_SUBMIT = "only a draft can be submitted"
NOT_WITHDRAWABLE = "only a draft or submitted request can be withdrawn"

SNAPSHOT_FIELDS = (*RequestFields.model_fields, "requested_by", "status", "submitted_at", "version")

Sort = Literal["submitted_at", "-submitted_at", "wanted_by", "-wanted_by"]


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if isinstance(value, date):
        return value.isoformat()
    return value


def snapshot(request: ProjectRequest) -> dict[str, Any]:
    """The request's values as an audit row records them (money as 2-dp strings)."""
    return {name: _json_value(getattr(request, name)) for name in SNAPSHOT_FIELDS}


def _record_change(
    db: Session, request: ProjectRequest, *, action: str, before: dict | None, user: User
) -> None:
    """One audit row for a change already flushed (so the version is the new one)."""
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=AUDIT_SUBJECT,
        subject_id=str(request.id),
        version=request.version,
        attempt=1,
        status="ok",
        actor_user_id=user.id,
        signals=before,
        output=snapshot(request),
        action_taken={"action": action},
    )


def _request(db: Session, request_id: int, *, for_update: bool = False) -> ProjectRequest:
    stmt = select(ProjectRequest).where(ProjectRequest.id == request_id)
    request = db.scalar(stmt.with_for_update() if for_update else stmt)
    if request is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return request


def _locked(db: Session, request_id: int, version: int) -> ProjectRequest:
    """The request, locked until commit on MySQL so a parallel change waits and then sees the
    new version; a stale version or a final status is a 409."""
    request = _request(db, request_id, for_update=True)
    if version != request.version:
        raise HTTPException(status.HTTP_409_CONFLICT, STALE)
    if request.status in FINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, FINAL)
    return request


def _check_client(db: Session, client_id: int | None) -> None:
    if client_id is not None and db.get(Client, client_id) is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_CLIENT)


def _save(db: Session) -> None:
    try:
        db.flush()
    except StaleDataError:
        # Another write changed the row between our read and this update.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE) from None


def _commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE) from None


def _names(db: Session, model: type, ids: set[int], name_column: Any) -> dict[int, str]:
    if not ids:
        return {}
    return dict(db.execute(select(model.id, name_column).where(model.id.in_(ids))).all())


def _out(db: Session, rows: list[ProjectRequest]) -> list[RequestOut]:
    """Requests with their client's and requester's names (display name, never the email)."""
    clients = _names(db, Client, {r.client_id for r in rows if r.client_id}, Client.name)
    people = _names(db, User, {r.requested_by for r in rows}, User.display_name)
    return [
        RequestOut.model_validate(row).model_copy(
            update={
                "client_name": clients.get(row.client_id) if row.client_id else None,
                "requested_by_name": people.get(row.requested_by),
            }
        )
        for row in rows
    ]


@router.get("", response_model=Page[RequestOut])
def list_requests(
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
    status_: Annotated[RequestStatus | None, Query(alias="status")] = None,
    client_id: int | None = None,
    uses_ai: bool | None = None,
    sort: Sort = "submitted_at",
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[RequestOut]:
    stmt = select(ProjectRequest)
    if status_ is not None:
        stmt = stmt.where(ProjectRequest.status == status_)
    if client_id is not None:
        stmt = stmt.where(ProjectRequest.client_id == client_id)
    if uses_ai is not None:
        stmt = stmt.where(ProjectRequest.uses_ai == uses_ai)
    column = getattr(ProjectRequest, sort.lstrip("-"))
    direction = column.desc() if sort.startswith("-") else column.asc()
    # Requests without the date last, whichever way; the id breaks ties so pages are stable.
    rows, total = paginate(
        db,
        stmt,
        order_by=(column.is_(None), direction, ProjectRequest.id),
        limit=limit,
        offset=offset,
    )
    return Page[RequestOut](items=_out(db, rows), total=total, limit=limit, offset=offset)


@router.post("", response_model=RequestOut, status_code=status.HTTP_201_CREATED)
def create_request(
    body: RequestCreate,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> RequestOut:
    _check_client(db, body.client_id)
    request = ProjectRequest(**body.model_dump(), requested_by=user.id, status="draft")
    db.add(request)
    db.flush()
    _record_change(db, request, action="create", before=None, user=user)
    _commit(db)
    return _out(db, [request])[0]


@router.get("/{request_id}", response_model=RequestOut)
def get_request(
    request_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> RequestOut:
    return _out(db, [_request(db, request_id)])[0]


@router.get("/{request_id}/history", response_model=Page[RequestHistoryEntry])
def request_history(
    request_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[RequestHistoryEntry]:
    request = _request(db, request_id)
    stmt = decisions_query(subject_type=AUDIT_SUBJECT, subject_id=str(request.id))
    rows, total = paginate(db, stmt, order_by=ORDER_BY, limit=limit, offset=offset)
    names = _names(db, User, {r.actor_user_id for r in rows if r.actor_user_id}, User.display_name)
    return Page[RequestHistoryEntry](
        items=[_history_entry(row, names) for row in rows], total=total, limit=limit, offset=offset
    )


def _history_entry(row: AuditDecision, names: dict[int, str]) -> RequestHistoryEntry:
    action = row.action_taken.get("action") if isinstance(row.action_taken, dict) else None
    return RequestHistoryEntry(
        id=row.id,
        version=row.version,
        action=str(action or "change"),
        actor_user_id=row.actor_user_id,
        actor_name=names.get(row.actor_user_id) if row.actor_user_id is not None else None,
        before=row.signals if isinstance(row.signals, dict) else None,
        after=row.output if isinstance(row.output, dict) else None,
        created_at=row.created_at,
    )


@router.patch("/{request_id}", response_model=RequestOut)
def update_request(
    request_id: int,
    body: RequestUpdate,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> RequestOut:
    request = _locked(db, request_id, body.version)
    if request.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_DRAFT_EDIT)
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    current = {name: getattr(request, name) for name in RequestFields.model_fields}
    try:
        fields = RequestFields.model_validate({**current, **changes})
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_context=False)) from None
    if "client_id" in changes:
        _check_client(db, fields.client_id)

    before = snapshot(request)
    for name, value in fields.model_dump().items():
        if getattr(request, name) != value:
            setattr(request, name, value)
    if not db.is_modified(request):
        return _out(db, [request])[0]
    _save(db)
    _record_change(db, request, action="update", before=before, user=user)
    _commit(db)
    return _out(db, [request])[0]


def _move(db: Session, request: ProjectRequest, *, to: str, action: str, user: User) -> RequestOut:
    before = snapshot(request)
    request.status = to
    if to == "submitted":
        request.submitted_at = utcnow()
    _save(db)
    _record_change(db, request, action=action, before=before, user=user)
    _commit(db)
    return _out(db, [request])[0]


@router.post("/{request_id}/submit", response_model=RequestOut)
def submit_request(
    request_id: int,
    body: RequestTransition,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> RequestOut:
    request = _locked(db, request_id, body.version)
    if request.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_DRAFT_SUBMIT)
    return _move(db, request, to="submitted", action="submit", user=user)


@router.post("/{request_id}/withdraw", response_model=RequestOut)
def withdraw_request(
    request_id: int,
    body: RequestTransition,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> RequestOut:
    request = _locked(db, request_id, body.version)
    if request.status not in ("draft", "submitted"):
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_WITHDRAWABLE)
    return _move(db, request, to="withdrawn", action="withdraw", user=user)
