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
- `GET` and `PUT /requests/{id}/value-case` read and replace the request's value case (item 4.2).
  It is part of the request: it changes only while the request is a draft, the PUT carries the
  request's `version` and bumps it, and each change is an audit row with action `value_case` in
  the request's history. Saving it as a person confirms it; a PUT that changes nothing on a value
  case a person has already confirmed writes nothing. A draft is submitted only once its value case
  is complete and confirmed; otherwise the 409 lists what is missing.
- `GET` and `PUT /requests/{id}/readiness` read and replace the readiness check of a request that
  uses AI (item 4.3, R11); both are a 404 for a request that doesn't. It follows the value case's
  rules (draft only, the request's `version`, audit action `readiness`, a person's save confirms
  it). Every yellow area needs an action with an owner (an active user) and a due date. A request
  that uses AI is submitted only once its check is complete and confirmed; a red result does not
  stop submission (the approval gate deals with it).

`POST /requests/{id}/draft` (item 4.5) runs the intake agent (`app.agents.intake_agent`) on a
draft request, gated by `AGENT_MODE` like the risk agent: `off` refuses (409) and calls nothing;
`shadow` returns the proposal and saves nothing; `enforce` saves the value case, and the
readiness check for a request that uses AI, as unconfirmed `source = agent` drafts, replacing
only a section no person has confirmed. With `?dry_run=true` it returns the exact request and a
cost ceiling and calls nothing. A failed run is audited and answered with a 502; it saves nothing.
An agent's draft never counts until a person saves it, which confirms it.

No route here sets `approved`, `declined` or returns a request to `draft`: that is the approval
gate's job, and a person's decision there. Every change carries the `version` the client read; a
different stored version, or a parallel write that lands first, is a 409. Each create, edit,
submit and withdraw is one audit row (agent `intake`, subject `project_request`) with the values
before and after and the person's id.
"""

import logging
from datetime import date
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.agents import intake_agent, risk_agent
from app.audit import ORDER_BY, decisions_query, record_decision
from app.authz import Action, require
from app.config import get_settings
from app.db import get_db
from app.models import (
    AuditDecision,
    Client,
    ProjectRequest,
    ReadinessAction,
    ReadinessCheck,
    User,
    ValueCase,
    ValueCaseBenefit,
    utcnow,
)
from app.models.intake import FINAL_STATUSES, READINESS_AREAS
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page, paginate
from app.schemas.intake import (
    DraftResult,
    ReadinessIn,
    ReadinessOut,
    RequestCreate,
    RequestFields,
    RequestHistoryEntry,
    RequestOut,
    RequestStatus,
    RequestTransition,
    RequestUpdate,
    ValueCaseIn,
    ValueCaseOut,
)

logger = logging.getLogger(__name__)

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
NO_VALUE_CASE = "this request has no value case yet"
NOT_DRAFT_VALUE_CASE = "the value case can be changed only while its request is a draft"
INCOMPLETE = (
    "this request can't be submitted until its value case, and its readiness check if it uses AI,"
    " are complete and confirmed"
)
MISSING_VALUE_CASE = "a value case"
MISSING_BENEFIT = "a benefit with a measure and a target"
MISSING_COST = "a cost estimate: low, high and how it was estimated"
MISSING_CONFIRMATION = "a person's confirmation of the value case an agent drafted"
NOT_AI = "this request doesn't use AI, so it has no readiness check"
NO_READINESS = "this request has no readiness check yet"
NOT_DRAFT_READINESS = "the readiness check can be changed only while its request is a draft"
BAD_OWNER = "every action's owner must be an active user"
MISSING_READINESS = "a readiness check"
MISSING_RATING = "a rating and a note for each readiness area: problem, approach and data"
NOT_DRAFT_AGENT = "the intake agent drafts only for a draft request"
AGENT_OFF = "the intake agent is off (AGENT_MODE=off); a person can write the value case by hand"
AGENT_FAILED = "the intake agent could not finish; the failed run is recorded in the audit trail"
MISSING_ACTION = "an action with an owner and a due date for each yellow readiness area"
MISSING_ACTION_DETAILS = "an owner and a due date for every readiness action"
MISSING_READINESS_CONFIRMATION = "a person's confirmation of the readiness check an agent drafted"

SNAPSHOT_FIELDS = (*RequestFields.model_fields, "requested_by", "status", "submitted_at", "version")
VALUE_CASE_FIELDS = ("cost_low", "cost_high", "cost_method", "strategic_fit", "cost_of_not_doing")
BENEFIT_FIELDS = ("description", "measure", "current_value", "target", "target_date")
CONFIRMATION_FIELDS = ("source", "confirmed_by", "confirmed_at")
ACTION_FIELDS = ("description", "owner_id", "due_date")

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


def _value_case_content(fields: dict[str, Any], benefits: list[dict[str, Any]]) -> dict[str, Any]:
    """What a person writes in a value case, as an audit row records it."""
    return {
        **{name: _json_value(fields[name]) for name in VALUE_CASE_FIELDS},
        "benefits": [{name: _json_value(b[name]) for name in BENEFIT_FIELDS} for b in benefits],
    }


def value_case_snapshot(value_case: ValueCase) -> dict[str, Any]:
    """The value case's content and who confirmed it, as an audit row records it."""
    fields = {name: getattr(value_case, name) for name in VALUE_CASE_FIELDS}
    benefits = [{name: getattr(b, name) for name in BENEFIT_FIELDS} for b in value_case.benefits]
    confirmation = {name: _json_value(getattr(value_case, name)) for name in CONFIRMATION_FIELDS}
    return {**_value_case_content(fields, benefits), **confirmation}


def _record_change(
    db: Session,
    request: ProjectRequest,
    *,
    action: str,
    before: dict | None,
    user: User,
    after: dict | None = None,
) -> None:
    """One audit row for a change already flushed (so the version is the new one). `after`
    defaults to the request's own snapshot."""
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
        output=snapshot(request) if after is None else after,
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
    return _check_version(_request(db, request_id, for_update=True), version)


def _check_version(request: ProjectRequest, version: int) -> ProjectRequest:
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
    missing = value_case_gaps(value_case_of(db, request.id))
    if request.uses_ai:
        # Without `uses_ai` no check is needed, whatever rows a past setting left behind.
        missing += readiness_gaps(readiness_of(db, request.id))
    if missing:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": INCOMPLETE, "missing": missing})
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


# --- The value case (item 4.2)


def value_case_of(db: Session, request_id: int) -> ValueCase | None:
    return db.scalar(select(ValueCase).where(ValueCase.request_id == request_id))


def value_case_gaps(value_case: ValueCase | None) -> list[str]:
    """What a request still needs before it can be submitted; empty when nothing is missing."""
    if value_case is None:
        return [MISSING_VALUE_CASE]
    missing = []
    if not any(b.measure and b.target for b in value_case.benefits):
        missing.append(MISSING_BENEFIT)
    if value_case.cost_low is None or value_case.cost_high is None or not value_case.cost_method:
        missing.append(MISSING_COST)
    if value_case.confirmed_by is None:
        missing.append(MISSING_CONFIRMATION)
    return missing


def _value_case_out(db: Session, request: ProjectRequest, value_case: ValueCase) -> ValueCaseOut:
    names = _names(db, User, {value_case.confirmed_by} - {None}, User.display_name)
    data = {
        name: getattr(value_case, name)
        for name in ValueCaseOut.model_fields
        if name not in ("request_version", "confirmed_by_name")
    }
    data["request_version"] = request.version
    data["confirmed_by_name"] = names.get(value_case.confirmed_by)
    return ValueCaseOut.model_validate(data, from_attributes=True)


@router.get("/{request_id}/value-case", response_model=ValueCaseOut)
def get_value_case(
    request_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> ValueCaseOut:
    request = _request(db, request_id)
    value_case = value_case_of(db, request.id)
    if value_case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_VALUE_CASE)
    return _value_case_out(db, request, value_case)


@router.put("/{request_id}/value-case", response_model=ValueCaseOut)
def put_value_case(
    request_id: int,
    body: ValueCaseIn,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> ValueCaseOut:
    request = _locked(db, request_id, body.version)
    if request.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_DRAFT_VALUE_CASE)
    fields = body.model_dump(exclude={"version"})
    benefits = fields.pop("benefits")
    value_case = value_case_of(db, request.id)
    before = None if value_case is None else value_case_snapshot(value_case)
    if (
        value_case is not None
        and value_case.confirmed_by is not None
        and _value_case_content(fields, benefits) == _value_case_content(before, before["benefits"])
    ):
        # Nothing changes and a person has already confirmed it: no new version, no audit row.
        return _value_case_out(db, request, value_case)

    if value_case is None:
        value_case = ValueCase(request_id=request.id, source="person")
        db.add(value_case)
    for name, value in fields.items():
        setattr(value_case, name, value)
    # Update benefits in place by position, so the (value case, position) key never clashes.
    for position, benefit in enumerate(benefits):
        if position < len(value_case.benefits):
            for name, value in benefit.items():
                setattr(value_case.benefits[position], name, value)
        else:
            value_case.benefits.append(ValueCaseBenefit(position=position, **benefit))
    del value_case.benefits[len(benefits) :]
    # A person saving it confirms it, whoever drafted it (R1).
    value_case.confirmed_by = user.id
    value_case.confirmed_at = utcnow()
    # The value case is part of the request: touching the request bumps its version.
    request.updated_at = utcnow()
    _save(db)
    _record_change(
        db,
        request,
        action="value_case",
        before=before,
        after=value_case_snapshot(value_case),
        user=user,
    )
    _commit(db)
    return _value_case_out(db, request, value_case)


# --- The readiness check (item 4.3)


def readiness_of(db: Session, request_id: int) -> ReadinessCheck | None:
    return db.scalar(select(ReadinessCheck).where(ReadinessCheck.request_id == request_id))


def _needs_ai(request: ProjectRequest) -> ProjectRequest:
    """A request without `uses_ai` has no readiness check. A row left from when it did is kept,
    hidden, and shows again if `uses_ai` is turned back on."""
    if not request.uses_ai:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_AI)
    return request


def readiness_gaps(check: ReadinessCheck | None) -> list[str]:
    """What a request that uses AI still needs from its check; empty when nothing is missing.
    A red result is not a gap: the approval gate deals with it."""
    if check is None:
        return [MISSING_READINESS]
    missing = []
    if any(
        getattr(check, f"{area}_rating") is None or not getattr(check, f"{area}_note")
        for area in READINESS_AREAS
    ):
        missing.append(MISSING_RATING)
    if any(
        rating == "yellow" and not check.area_actions(area)
        for area, rating in check.ratings().items()
    ):
        missing.append(MISSING_ACTION)
    if any(a.owner_id is None or a.due_date is None for a in check.actions):
        # Only an agent's draft leaves these empty; a person's save sets both.
        missing.append(MISSING_ACTION_DETAILS)
    if check.confirmed_by is None:
        missing.append(MISSING_READINESS_CONFIRMATION)
    return missing


def _readiness_content(areas: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """What a person writes in a readiness check, as an audit row records it."""
    return {
        area: {
            "rating": areas[area]["rating"],
            "note": areas[area]["note"],
            "actions": [
                {name: _json_value(a[name]) for name in ACTION_FIELDS}
                for a in areas[area]["actions"]
            ],
        }
        for area in READINESS_AREAS
    }


def readiness_snapshot(check: ReadinessCheck) -> dict[str, Any]:
    """The check's content, its overall result and who confirmed it, as an audit row records it."""
    areas = {
        area: {
            "rating": getattr(check, f"{area}_rating"),
            "note": getattr(check, f"{area}_note"),
            "actions": [
                {name: getattr(a, name) for name in ACTION_FIELDS} for a in check.area_actions(area)
            ],
        }
        for area in READINESS_AREAS
    }
    confirmation = {name: _json_value(getattr(check, name)) for name in CONFIRMATION_FIELDS}
    return {**_readiness_content(areas), "overall": check.overall, **confirmation}


def _readiness_out(db: Session, request: ProjectRequest, check: ReadinessCheck) -> ReadinessOut:
    ids = ({a.owner_id for a in check.actions} | {check.confirmed_by}) - {None}
    names = _names(db, User, ids, User.display_name)
    areas = {
        area: {
            "rating": getattr(check, f"{area}_rating"),
            "note": getattr(check, f"{area}_note"),
            "actions": [
                {
                    "description": a.description,
                    "owner_id": a.owner_id,
                    "owner_name": names.get(a.owner_id) if a.owner_id else None,
                    "due_date": a.due_date,
                }
                for a in check.area_actions(area)
            ],
        }
        for area in READINESS_AREAS
    }
    return ReadinessOut(
        request_id=request.id,
        request_version=request.version,
        **areas,
        overall=check.overall,
        source=check.source,
        confirmed_by=check.confirmed_by,
        confirmed_by_name=names.get(check.confirmed_by) if check.confirmed_by else None,
        confirmed_at=check.confirmed_at,
        created_at=check.created_at,
        updated_at=check.updated_at,
    )


def _check_owners(db: Session, areas: dict[str, dict[str, Any]]) -> None:
    owners = {a["owner_id"] for area in areas.values() for a in area["actions"]}
    if not owners:
        return
    stmt = select(User.id).where(User.id.in_(owners), User.is_active.is_(True))
    if set(db.scalars(stmt).all()) != owners:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_OWNER)


@router.get("/{request_id}/readiness", response_model=ReadinessOut)
def get_readiness(
    request_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> ReadinessOut:
    request = _needs_ai(_request(db, request_id))
    check = readiness_of(db, request.id)
    if check is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_READINESS)
    return _readiness_out(db, request, check)


@router.put("/{request_id}/readiness", response_model=ReadinessOut)
def put_readiness(
    request_id: int,
    body: ReadinessIn,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> ReadinessOut:
    request = _check_version(_needs_ai(_request(db, request_id, for_update=True)), body.version)
    if request.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_DRAFT_READINESS)
    areas = body.model_dump(exclude={"version"})
    _check_owners(db, areas)
    check = readiness_of(db, request.id)
    before = None if check is None else readiness_snapshot(check)
    if (
        check is not None
        and check.confirmed_by is not None
        and _readiness_content(areas) == _readiness_content(before)
    ):
        # Nothing changes and a person has already confirmed it: no new version, no audit row.
        return _readiness_out(db, request, check)

    if check is None:
        check = ReadinessCheck(request_id=request.id, source="person")
        db.add(check)
    # Update actions in place by (area, position), so that key never clashes on flush.
    stored = {(a.area, a.position): a for a in check.actions}
    for area, fields in areas.items():
        setattr(check, f"{area}_rating", fields["rating"])
        setattr(check, f"{area}_note", fields["note"])
        for position, action in enumerate(fields["actions"]):
            row = stored.pop((area, position), None)
            if row is None:
                check.actions.append(ReadinessAction(area=area, position=position, **action))
            else:
                for name, value in action.items():
                    setattr(row, name, value)
    for row in stored.values():
        check.actions.remove(row)
    # A person saving it confirms it, whoever drafted it (R1, R11).
    check.confirmed_by = user.id
    check.confirmed_at = utcnow()
    # The check is part of the request: touching the request bumps its version.
    request.updated_at = utcnow()
    _save(db)
    _record_change(
        db,
        request,
        action="readiness",
        before=before,
        after=readiness_snapshot(check),
        user=user,
    )
    _commit(db)
    return _readiness_out(db, request, check)


# --- The intake agent (item 4.5)


def get_claude_client() -> risk_agent.ClaudeClient:
    """The intake agent's Claude client; tests replace it through `dependency_overrides`."""
    return risk_agent.AnthropicClient(get_settings())


@router.post("/{request_id}/draft", response_model=DraftResult)
def draft_request(
    request_id: int,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
    claude: Annotated[risk_agent.ClaudeClient, Depends(get_claude_client)],
    dry_run: bool = False,
) -> DraftResult:
    settings = get_settings()
    if settings.agent_mode == "off":
        raise HTTPException(status.HTTP_409_CONFLICT, AGENT_OFF)
    request = _request(db, request_id)
    if request.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_DRAFT_AGENT)
    common = {
        "mode": settings.agent_mode,
        "dry_run": dry_run,
        "model": settings.intake_agent_model,
        "prompt_version": intake_agent.PROMPT_VERSION,
    }
    if dry_run:
        api_request = intake_agent.build_request(intake_agent.request_data(db, request), settings)
        ceiling = intake_agent.cost_ceiling(api_request, settings)
        return DraftResult(
            **common,
            request=api_request,
            cost_ceiling_usd=ceiling.quantize(Decimal("0.01"), rounding=ROUND_UP),
        )
    try:
        result = intake_agent.run(
            db,
            claude,
            request,
            settings=settings,
            save_it=settings.agent_mode == "enforce",
            actor_user_id=user.id,
        )
    except risk_agent.AgentError:
        logger.exception("intake agent run failed for request %s", request_id)
        _commit(db)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, AGENT_FAILED) from None
    _commit(db)
    checked = result.checked
    proposal = intake_agent.proposal_json(checked)
    return DraftResult(
        **common,
        value_case=proposal["value_case"],
        readiness=proposal["readiness"],
        dropped=checked.dropped,
        adjusted=checked.adjusted,
        saved=result.saved,
        skipped=result.skipped,
        request_version=request.version,
        audit_id=result.audit_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )
