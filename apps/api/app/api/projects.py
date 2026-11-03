"""A project and the records planning starts from: its brief, stakeholders and assumptions
(item 4.6).

Reading needs `project:read`; changing needs `project:write`. As with requests, any role holding
`project:write` may edit any project's records (work gets handed over); the audit row records who
acted. Sponsors have no `project:write`, so they read only.

- `GET /projects/{id}` is the project, with its client's name and the request it came from.
- `GET` and `PATCH /projects/{id}/brief` read and edit the brief made when the request passed the
  approval gate. A PATCH that changes nothing writes nothing.
- `GET` and `POST /projects/{id}/stakeholders`, and `PATCH` and `DELETE
  /projects/{id}/stakeholders/{sid}`, list, add, edit and remove stakeholders. A stakeholder's
  `user_id`, if set, must be an active user.
- The same four for `/projects/{id}/assumptions`. Each assumption's owner must be an active user.

Lists are paged, oldest first by id. Every edit and delete carries the `version` the client read
(a DELETE as `?version=`); a stale one, or a parallel write that lands first, is a 409. Each
change is one audit row (agent `project`, subject the brief, stakeholder or assumption, its new
version) with the values before and after, the project's id and the person's id.
"""

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.intake import STALE, _commit, _json_value, _names, _save
from app.audit import record_decision
from app.authz import Action, require
from app.db import get_db
from app.models import (
    Client,
    Project,
    ProjectAssumption,
    ProjectBrief,
    ProjectRequest,
    ProjectStakeholder,
    User,
)
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page, paginate
from app.schemas.planning import (
    AssumptionCreate,
    AssumptionOut,
    AssumptionUpdate,
    BriefOut,
    BriefUpdate,
    ProjectDetailOut,
    StakeholderCreate,
    StakeholderOut,
    StakeholderUpdate,
)

router = APIRouter(prefix="/projects", tags=["projects"])

AUDIT_AGENT = "project"
NOT_FOUND = "project not found"
NO_BRIEF = "this project has no brief"
STAKEHOLDER_NOT_FOUND = "stakeholder not found"
ASSUMPTION_NOT_FOUND = "assumption not found"
BAD_USER = "user_id must be an active user"
BAD_OWNER = "owner_user_id must be an active user"
BAD_BUDGET = "budget_low must not be above budget_high"

BRIEF_FIELDS = (
    "purpose",
    "objectives",
    "scope_in",
    "scope_out",
    "wanted_by",
    "budget_low",
    "budget_high",
    "decision_rights",
)
STAKEHOLDER_FIELDS = ("name", "user_id", "role_or_interest", "needs_to_hear", "how_often")
ASSUMPTION_FIELDS = ("text", "owner_user_id", "confirm_by", "status")

Reader = Annotated[User, Depends(require(Action.PROJECT_READ))]
Writer = Annotated[User, Depends(require(Action.PROJECT_WRITE))]
Db = Annotated[Session, Depends(get_db)]


def _snapshot(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: _json_value(getattr(row, name)) for name in (*fields, "version")}


def _record(
    db: Session,
    *,
    subject_type: str,
    subject_id: int,
    version: int,
    project_id: int,
    action: str,
    user: User,
    before: dict | None,
    after: dict | None,
) -> None:
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=subject_type,
        subject_id=str(subject_id),
        version=version,
        attempt=1,
        status="ok",
        actor_user_id=user.id,
        signals=before,
        output=after,
        action_taken={"action": action, "project_id": project_id},
    )


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return project


def _row[T](db: Session, model: type[T], project_id: int, row_id: int, missing: str) -> T:
    """A project's stakeholder or assumption, locked until commit on MySQL; 404 if it isn't
    the project's."""
    _project(db, project_id)
    stmt = select(model).where(model.id == row_id, model.project_id == project_id)  # type: ignore[attr-defined]
    row = db.scalar(stmt.with_for_update())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, missing)
    return row


def _check_version(row: Any, version: int) -> None:
    if row.version != version:
        raise HTTPException(status.HTTP_409_CONFLICT, STALE)


def _check_user(db: Session, user_id: int | None, message: str) -> None:
    if user_id is None:
        return
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)


def _changes(body: BaseModel) -> dict[str, Any]:
    """The fields a PATCH sends, as the model stores them."""
    return body.model_dump(exclude={"version"}, exclude_unset=True)


def _apply(row: Any, changes: dict[str, Any]) -> bool:
    """Set each changed field; True when anything differs from what is stored."""
    changed = False
    for name, value in changes.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    return changed


# --- The project


@router.get("/{project_id}", response_model=ProjectDetailOut)
def get_project(project_id: int, _user: Reader, db: Db) -> ProjectDetailOut:
    project = _project(db, project_id)
    client = db.get(Client, project.client_id) if project.client_id else None
    request_id = db.scalar(select(ProjectRequest.id).where(ProjectRequest.project_id == project.id))
    return ProjectDetailOut.model_validate(project).model_copy(
        update={"client_name": client.name if client else None, "request_id": request_id}
    )


# --- The brief


def _brief(db: Session, project_id: int, *, for_update: bool = False) -> ProjectBrief:
    _project(db, project_id)
    stmt = select(ProjectBrief).where(ProjectBrief.project_id == project_id)
    brief = db.scalar(stmt.with_for_update() if for_update else stmt)
    if brief is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_BRIEF)
    return brief


def _brief_out(db: Session, brief: ProjectBrief) -> BriefOut:
    project = db.get(Project, brief.project_id)
    return BriefOut.model_validate(
        {
            **{
                name: getattr(brief, name) for name in BriefOut.model_fields if hasattr(brief, name)
            },
            "sponsor": project.sponsor,
            "sponsor_user_id": project.sponsor_user_id,
            "pm": project.pm,
            "pm_user_id": project.pm_user_id,
        }
    )


@router.get("/{project_id}/brief", response_model=BriefOut)
def get_brief(project_id: int, _user: Reader, db: Db) -> BriefOut:
    return _brief_out(db, _brief(db, project_id))


@router.patch("/{project_id}/brief", response_model=BriefOut)
def update_brief(project_id: int, body: BriefUpdate, user: Writer, db: Db) -> BriefOut:
    brief = _brief(db, project_id, for_update=True)
    _check_version(brief, body.version)
    changes = _changes(body)
    if "decision_rights" in changes:
        changes["decision_rights"] = [dict(r) for r in changes["decision_rights"]]
    low = changes.get("budget_low", brief.budget_low)
    high = changes.get("budget_high", brief.budget_high)
    if low is not None and high is not None and Decimal(low) > Decimal(high):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_BUDGET)
    before = _snapshot(brief, BRIEF_FIELDS)
    if not _apply(brief, changes):
        return _brief_out(db, brief)
    _save(db)
    _record(
        db,
        subject_type="project_brief",
        subject_id=brief.id,
        version=brief.version,
        project_id=project_id,
        action="update",
        user=user,
        before=before,
        after=_snapshot(brief, BRIEF_FIELDS),
    )
    _commit(db)
    return _brief_out(db, brief)


# --- Stakeholders


@router.get("/{project_id}/stakeholders", response_model=Page[StakeholderOut])
def list_stakeholders(
    project_id: int,
    _user: Reader,
    db: Db,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[StakeholderOut]:
    _project(db, project_id)
    stmt = select(ProjectStakeholder).where(ProjectStakeholder.project_id == project_id)
    rows, total = paginate(db, stmt, order_by=(ProjectStakeholder.id,), limit=limit, offset=offset)
    return Page[StakeholderOut](
        items=[StakeholderOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{project_id}/stakeholders",
    response_model=StakeholderOut,
    status_code=status.HTTP_201_CREATED,
)
def create_stakeholder(
    project_id: int, body: StakeholderCreate, user: Writer, db: Db
) -> StakeholderOut:
    _project(db, project_id)
    _check_user(db, body.user_id, BAD_USER)
    row = ProjectStakeholder(project_id=project_id, **body.model_dump())
    db.add(row)
    db.flush()
    _record(
        db,
        subject_type="project_stakeholder",
        subject_id=row.id,
        version=row.version,
        project_id=project_id,
        action="create",
        user=user,
        before=None,
        after=_snapshot(row, STAKEHOLDER_FIELDS),
    )
    _commit(db)
    return StakeholderOut.model_validate(row)


@router.patch("/{project_id}/stakeholders/{stakeholder_id}", response_model=StakeholderOut)
def update_stakeholder(
    project_id: int, stakeholder_id: int, body: StakeholderUpdate, user: Writer, db: Db
) -> StakeholderOut:
    row = _row(db, ProjectStakeholder, project_id, stakeholder_id, STAKEHOLDER_NOT_FOUND)
    _check_version(row, body.version)
    changes = _changes(body)
    _check_user(db, changes.get("user_id"), BAD_USER)
    before = _snapshot(row, STAKEHOLDER_FIELDS)
    if not _apply(row, changes):
        return StakeholderOut.model_validate(row)
    _save(db)
    _record(
        db,
        subject_type="project_stakeholder",
        subject_id=row.id,
        version=row.version,
        project_id=project_id,
        action="update",
        user=user,
        before=before,
        after=_snapshot(row, STAKEHOLDER_FIELDS),
    )
    _commit(db)
    return StakeholderOut.model_validate(row)


@router.delete(
    "/{project_id}/stakeholders/{stakeholder_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_stakeholder(
    project_id: int,
    stakeholder_id: int,
    version: Annotated[int, Query(ge=1)],
    user: Writer,
    db: Db,
) -> Response:
    row = _row(db, ProjectStakeholder, project_id, stakeholder_id, STAKEHOLDER_NOT_FOUND)
    _delete(db, row, version, "project_stakeholder", STAKEHOLDER_FIELDS, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _delete(
    db: Session, row: Any, version: int, subject_type: str, fields: tuple[str, ...], user: User
) -> None:
    """Remove a stakeholder or assumption; its audit row keeps what it was."""
    _check_version(row, version)
    before = _snapshot(row, fields)
    db.delete(row)
    _save(db)
    _record(
        db,
        subject_type=subject_type,
        subject_id=row.id,
        version=version + 1,
        project_id=row.project_id,
        action="delete",
        user=user,
        before=before,
        after=None,
    )
    _commit(db)


# --- Assumptions


def _assumptions_out(db: Session, rows: list[ProjectAssumption]) -> list[AssumptionOut]:
    owners = _names(db, User, {r.owner_user_id for r in rows}, User.display_name)
    return [
        AssumptionOut.model_validate(r).model_copy(
            update={"owner_name": owners.get(r.owner_user_id)}
        )
        for r in rows
    ]


@router.get("/{project_id}/assumptions", response_model=Page[AssumptionOut])
def list_assumptions(
    project_id: int,
    _user: Reader,
    db: Db,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[AssumptionOut]:
    _project(db, project_id)
    stmt = select(ProjectAssumption).where(ProjectAssumption.project_id == project_id)
    rows, total = paginate(db, stmt, order_by=(ProjectAssumption.id,), limit=limit, offset=offset)
    return Page[AssumptionOut](
        items=_assumptions_out(db, rows), total=total, limit=limit, offset=offset
    )


@router.post(
    "/{project_id}/assumptions",
    response_model=AssumptionOut,
    status_code=status.HTTP_201_CREATED,
)
def create_assumption(
    project_id: int, body: AssumptionCreate, user: Writer, db: Db
) -> AssumptionOut:
    _project(db, project_id)
    _check_user(db, body.owner_user_id, BAD_OWNER)
    row = ProjectAssumption(project_id=project_id, **body.model_dump())
    db.add(row)
    db.flush()
    _record(
        db,
        subject_type="project_assumption",
        subject_id=row.id,
        version=row.version,
        project_id=project_id,
        action="create",
        user=user,
        before=None,
        after=_snapshot(row, ASSUMPTION_FIELDS),
    )
    _commit(db)
    return _assumptions_out(db, [row])[0]


@router.patch("/{project_id}/assumptions/{assumption_id}", response_model=AssumptionOut)
def update_assumption(
    project_id: int, assumption_id: int, body: AssumptionUpdate, user: Writer, db: Db
) -> AssumptionOut:
    row = _row(db, ProjectAssumption, project_id, assumption_id, ASSUMPTION_NOT_FOUND)
    _check_version(row, body.version)
    changes = _changes(body)
    _check_user(db, changes.get("owner_user_id"), BAD_OWNER)
    before = _snapshot(row, ASSUMPTION_FIELDS)
    if not _apply(row, changes):
        return _assumptions_out(db, [row])[0]
    _save(db)
    _record(
        db,
        subject_type="project_assumption",
        subject_id=row.id,
        version=row.version,
        project_id=project_id,
        action="update",
        user=user,
        before=before,
        after=_snapshot(row, ASSUMPTION_FIELDS),
    )
    _commit(db)
    return _assumptions_out(db, [row])[0]


@router.delete("/{project_id}/assumptions/{assumption_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_assumption(
    project_id: int,
    assumption_id: int,
    version: Annotated[int, Query(ge=1)],
    user: Writer,
    db: Db,
) -> Response:
    row = _row(db, ProjectAssumption, project_id, assumption_id, ASSUMPTION_NOT_FOUND)
    _delete(db, row, version, "project_assumption", ASSUMPTION_FIELDS, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
