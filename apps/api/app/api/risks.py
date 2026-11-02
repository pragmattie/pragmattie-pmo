"""A project's risk register: list, create, read and change risks.

Reading needs `project:read`; creating and changing need `project:write`. There is no DELETE: a
risk is closed with a PATCH that sets `status` to `closed`. On these routes a person may only open
or close a risk; proposing, accepting and rejecting come with item 3.4.

`POST .../risks/from-library` adds starter templates (`app.api.risk_library`) as `proposed`
risks from a person, unscored, one audit row each; an unknown template id adds nothing (422).

`GET .../risks/heatmap` counts open risks by probability and impact, with totals per band, for
the risk register screen; `GET .../risks/{id}/history` is the risk's own audit rows, newest
first. Both need only `project:read`, so the people who work from the register can see how each
risk changed without the wider `audit:read`.

Score and band come from the risk policy, never from the client. If the policy can't be loaded,
writes are refused with a 503 rather than saving a risk unscored. A PATCH carries the `version` the
client read: a different stored version, or a parallel write that lands first, is a 409.
"""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.api.risk_library import library
from app.audit import ORDER_BY, decisions_query
from app.authz import Action, require
from app.db import get_db
from app.models import AuditDecision, Project, Risk, User
from app.policy import SCALE_LEVELS, PolicyError, RiskPolicy, score_risk
from app.risks import (
    AUDIT_AGENT,
    AUDIT_SUBJECT,
    apply_score,
    record_risk_change,
    risk_from_template,
    risk_policy,
    snapshot,
)
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page, paginate
from app.schemas.risk import (
    BandTotal,
    Category,
    EditableStatus,
    FromLibrary,
    HeatCell,
    Kind,
    RiskCreate,
    RiskFields,
    RiskHeatmap,
    RiskHistoryEntry,
    RiskOut,
    RiskUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/risks", tags=["risks"])

POLICY_UNAVAILABLE = "the risk policy could not be loaded, so risks cannot be saved; try later"
STALE = "this risk has changed since you read it; reload it and try again"
BAD_OWNER = "owner_user_id must be an existing, active user"

ListStatus = Literal["proposed", "open", "accepted", "closed", "rejected"]


def _policy() -> RiskPolicy:
    try:
        return risk_policy()
    except PolicyError:
        logger.exception("risk policy failed to load")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, POLICY_UNAVAILABLE) from None


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project


def _risk(db: Session, project_id: int, risk_id: int, *, for_update: bool = False) -> Risk:
    _project(db, project_id)
    stmt = select(Risk).where(Risk.id == risk_id, Risk.project_id == project_id)
    risk = db.scalar(stmt.with_for_update() if for_update else stmt)
    if risk is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "risk not found")
    return risk


def _check_owner(db: Session, owner_user_id: int | None) -> None:
    if owner_user_id is None:
        return
    owner = db.get(User, owner_user_id)
    if owner is None or not owner.is_active:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_OWNER)


def _commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE) from None


@router.get("", response_model=Page[RiskOut])
def list_risks(
    project_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
    status_: Annotated[ListStatus | None, Query(alias="status")] = None,
    band: Annotated[str | None, Query(max_length=32)] = None,
    category: Category | None = None,
    kind: Kind | None = None,
    sort: Literal["-score", "score"] = "-score",
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[RiskOut]:
    _project(db, project_id)
    stmt = select(Risk).where(Risk.project_id == project_id)
    for col, value in ((Risk.status, status_), (Risk.band, band), (Risk.category, category)):
        if value is not None:
            stmt = stmt.where(col == value)
    if kind is not None:
        stmt = stmt.where(Risk.kind == kind)
    # Unscored risks last either way; the primary key breaks ties so pages are stable.
    score = Risk.score.desc() if sort == "-score" else Risk.score.asc()
    rows, total = paginate(
        db,
        stmt,
        order_by=(Risk.score.is_(None), score, Risk.id),
        limit=limit,
        offset=offset,
    )
    return Page[RiskOut](
        items=[RiskOut.model_validate(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.post("", response_model=RiskOut, status_code=status.HTTP_201_CREATED)
def create_risk(
    project_id: int,
    body: RiskCreate,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> RiskOut:
    policy = _policy()
    _project(db, project_id)
    _check_owner(db, body.owner_user_id)
    risk = Risk(project_id=project_id, status="open", source="person", **body.model_dump())
    apply_score(risk, policy)
    db.add(risk)
    db.flush()
    record_risk_change(db, risk, action="create", before=None, actor_user_id=user.id)
    _commit(db)
    return RiskOut.model_validate(risk)


@router.post("/from-library", response_model=list[RiskOut], status_code=status.HTTP_201_CREATED)
def add_from_library(
    project_id: int,
    body: FromLibrary,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> list[RiskOut]:
    templates = library().templates
    _project(db, project_id)
    unknown = [template_id for template_id in body.template_ids if template_id not in templates]
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"not in the risk library: {', '.join(unknown)}",
        )
    added = []
    for template_id in body.template_ids:
        risk = risk_from_template(project_id, templates[template_id])
        db.add(risk)
        db.flush()
        record_risk_change(
            db,
            risk,
            action="create",
            before=None,
            actor_user_id=user.id,
            details={"template_id": template_id},
        )
        added.append(risk)
    _commit(db)
    return [RiskOut.model_validate(risk) for risk in added]


@router.get("/heatmap", response_model=RiskHeatmap)
def heatmap(
    project_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
    kind: Kind | None = None,
) -> RiskHeatmap:
    policy = _policy()
    _project(db, project_id)
    stmt = select(Risk.probability, Risk.impact, func.count()).where(
        Risk.project_id == project_id, Risk.status == "open"
    )
    if kind is not None:
        stmt = stmt.where(Risk.kind == kind)
    counts = {
        (probability, impact): count
        for probability, impact, count in db.execute(stmt.group_by(Risk.probability, Risk.impact))
    }
    cells = []
    band_counts = {band.name: 0 for band in policy.priority_bands}
    for probability in SCALE_LEVELS:
        for impact in SCALE_LEVELS:
            # The cell's band from the current policy, so the map matches a re-scored register.
            _, band = score_risk(policy, probability, impact)
            count = counts.get((probability, impact), 0)
            band_counts[band] += count
            cells.append(HeatCell(probability=probability, impact=impact, band=band, count=count))
    return RiskHeatmap(
        kind=kind,
        cells=cells,
        bands=[BandTotal(band=name, count=count) for name, count in band_counts.items()],
        unscored=counts.get((None, None), 0),
    )


@router.get("/{risk_id}/history", response_model=Page[RiskHistoryEntry])
def risk_history(
    project_id: int,
    risk_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[RiskHistoryEntry]:
    risk = _risk(db, project_id, risk_id)
    stmt = decisions_query(agent=AUDIT_AGENT, subject_type=AUDIT_SUBJECT, subject_id=str(risk.id))
    rows, total = paginate(db, stmt, order_by=ORDER_BY, limit=limit, offset=offset)
    actor_ids = {row.actor_user_id for row in rows if row.actor_user_id is not None}
    names = (
        dict(db.execute(select(User.id, User.display_name).where(User.id.in_(actor_ids))).all())
        if actor_ids
        else {}
    )
    return Page[RiskHistoryEntry](
        items=[_history_entry(row, names) for row in rows], total=total, limit=limit, offset=offset
    )


def _history_entry(row: AuditDecision, names: dict[int, str]) -> RiskHistoryEntry:
    action = row.action_taken.get("action") if isinstance(row.action_taken, dict) else None
    return RiskHistoryEntry(
        id=row.id,
        version=row.version,
        action=str(action or "change"),
        actor_user_id=row.actor_user_id,
        actor_name=names.get(row.actor_user_id) if row.actor_user_id is not None else None,
        before=row.signals if isinstance(row.signals, dict) else None,
        after=row.output if isinstance(row.output, dict) else None,
        created_at=row.created_at,
    )


@router.get("/{risk_id}", response_model=RiskOut)
def get_risk(
    project_id: int,
    risk_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> RiskOut:
    return RiskOut.model_validate(_risk(db, project_id, risk_id))


@router.patch("/{risk_id}", response_model=RiskOut)
def update_risk(
    project_id: int,
    risk_id: int,
    body: RiskUpdate,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> RiskOut:
    policy = _policy()
    # Locked until commit on MySQL, so a parallel edit waits and then sees the new version.
    risk = _risk(db, project_id, risk_id, for_update=True)
    if body.version != risk.version:
        raise HTTPException(status.HTTP_409_CONFLICT, STALE)

    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    new_status: EditableStatus | None = changes.pop("status", risk.status)
    if new_status is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "status must be open or closed")
    current = {name: getattr(risk, name) for name in RiskFields.model_fields}
    try:
        fields = RiskFields.model_validate({**current, **changes})
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_context=False)) from None
    if "owner_user_id" in changes:
        _check_owner(db, fields.owner_user_id)

    before = snapshot(risk)
    for name, value in {**fields.model_dump(), "status": new_status}.items():
        if getattr(risk, name) != value:
            setattr(risk, name, value)
    apply_score(risk, policy)
    if snapshot(risk) == before:
        return RiskOut.model_validate(risk)

    try:
        db.flush()
    except StaleDataError:
        # Another write changed the row between our read and this update.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE) from None
    closing = before["status"] != "closed" and risk.status == "closed"
    record_risk_change(
        db, risk, action="close" if closing else "update", before=before, actor_user_id=user.id
    )
    _commit(db)
    return RiskOut.model_validate(risk)
