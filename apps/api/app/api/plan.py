"""A project's plan (item 5.1): scope, deliverables, milestones, estimate and reporting plan.

Reading needs `project:read`; every write needs `project:write`. As with the brief, any role
holding `project:write` may edit any project's plan; the audit row records who acted.

- `GET /projects/{id}/plan` is the plan with its computed total (`lines_total` plus
  `contingency_amount`), how the total fits the brief's budget range and what is `missing`.
- `PATCH /projects/{id}/plan` edits scope, contingency and the reporting plan. Changing the
  report period needs a due day that fits the new period in the same write.
- `GET` and `POST /projects/{id}/plan/milestones`, `PATCH` and `DELETE .../{mid}`, and the same
  for `deliverables` and `estimate-lines`, list, add, edit and remove items. Milestones are listed
  by target date, the others oldest first; every list is paged.

Writes work only while the project is in `planning` (afterwards changes go through change
requests): a 409 otherwise. Each write carries a `version`: the item's for an edit or delete (a
DELETE as `?version=`), the plan's for a PATCH of the plan or an addition. Every write moves the
plan's version on, so the plan's version changes whenever anything in it does. A stale version is
a 409. Each write is one audit row (agent `project`, subject the plan or item, its new version)
with the values before and after.

Deleting a milestone leaves its deliverables without a milestone (they then show in `missing`);
its audit row lists them.
"""

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.intake import STALE, _commit, _names, _save
from app.api.projects import (
    AUDIT_AGENT,
    Db,
    Reader,
    Writer,
    _apply,
    _changes,
    _check_user,
    _project,
    _snapshot,
)
from app.audit import record_decision
from app.models import (
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectBrief,
    ProjectPlan,
    User,
)
from app.models.base import utcnow
from app.models.plan import DUE_DAY_MAX
from app.plans import budget_fit, compute_total, missing, subtotal
from app.projects import STAGE
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page, paginate
from app.schemas.plan import (
    DeliverableCreate,
    DeliverableOut,
    DeliverableUpdate,
    EstimateLineCreate,
    EstimateLineOut,
    EstimateLineUpdate,
    MilestoneCreate,
    MilestoneOut,
    MilestoneUpdate,
    PlanOut,
    PlanUpdate,
)

router = APIRouter(prefix="/projects", tags=["plans"])

NO_PLAN = "this project has no plan"
MILESTONE_NOT_FOUND = "milestone not found"
DELIVERABLE_NOT_FOUND = "deliverable not found"
LINE_NOT_FOUND = "estimate line not found"
NOT_PLANNING = (
    "the plan can be changed only while the project is in planning; after that, changes go "
    "through change requests"
)
BAD_OWNER = "owner_user_id must be an active user"
BAD_MILESTONE = "milestone_id must be a milestone of this plan"
PERIOD_NEEDS_DAY = "changing report_period needs a report_due_day that fits it in the same change"
DAY_NEEDS_PERIOD = "report_due_day needs a report_period"
BAD_DUE_DAY = (
    "report_due_day must be 1 to 7 (Monday is 1) for weekly and fortnightly reports, "
    "and 1 to 28 for monthly ones"
)

PLAN_FIELDS = ("scope_in", "scope_out", "contingency_pct", "report_period", "report_due_day")
MILESTONE_FIELDS = ("name", "target_date", "owner_user_id", "description", "contributors")
DELIVERABLE_FIELDS = ("name", "description", "milestone_id")
LINE_FIELDS = ("description", "category", "amount")


def _invalid(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)


def _record(
    db: Session,
    *,
    plan: ProjectPlan,
    subject_type: str,
    subject_id: int,
    version: int,
    action: str,
    user: User,
    before: dict | None,
    after: dict | None,
    **extra: Any,
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
        action_taken={
            "action": action,
            "project_id": plan.project_id,
            "plan_id": plan.id,
            **extra,
        },
    )


def _plan(db: Session, project_id: int, *, for_update: bool = False) -> ProjectPlan:
    _project(db, project_id)
    stmt = select(ProjectPlan).where(ProjectPlan.project_id == project_id)
    plan = db.scalar(stmt.with_for_update() if for_update else stmt)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_PLAN)
    return plan


def _writable_plan(db: Session, project_id: int) -> ProjectPlan:
    """The plan, locked until commit on MySQL, if its project is still in planning."""
    project = _project(db, project_id)
    if project.stage != STAGE:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_PLANNING)
    return _plan(db, project_id, for_update=True)


def _item[T](db: Session, model: type[T], plan: ProjectPlan, item_id: int, missing_: str) -> T:
    stmt = select(model).where(model.id == item_id, model.plan_id == plan.id)  # type: ignore[attr-defined]
    row = db.scalar(stmt.with_for_update())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, missing_)
    return row


def _check_version(row: Any, version: int) -> None:
    if row.version != version:
        raise HTTPException(status.HTTP_409_CONFLICT, STALE)


def _touch(db: Session, plan: ProjectPlan) -> None:
    """Move the plan's version on: something in it changed."""
    plan.updated_at = utcnow()
    _save(db)


def _plan_out(db: Session, plan: ProjectPlan) -> PlanOut:
    project = db.get(Project, plan.project_id)
    brief = db.scalar(select(ProjectBrief).where(ProjectBrief.project_id == plan.project_id))
    amounts = list(
        db.scalars(select(PlanEstimateLine.amount).where(PlanEstimateLine.plan_id == plan.id))
    )
    lines_total = subtotal(amounts)
    total = compute_total(amounts, plan.contingency_pct)
    low = brief.budget_low if brief else None
    high = brief.budget_high if brief else None
    return PlanOut(
        **{name: getattr(plan, name) for name in (*PLAN_FIELDS, "id", "project_id", "version")},
        updated_at=plan.updated_at,
        lines_total=lines_total,
        contingency_amount=total - lines_total,
        total=total,
        budget_low=low,
        budget_high=high,
        budget_fit=budget_fit(total, low, high),
        missing=missing(db, plan),
        editable=project.stage == STAGE,
    )


# --- The plan


@router.get("/{project_id}/plan", response_model=PlanOut)
def get_plan(project_id: int, _user: Reader, db: Db) -> PlanOut:
    return _plan_out(db, _plan(db, project_id))


def _check_reporting(plan: ProjectPlan, changes: dict[str, Any]) -> None:
    period = changes.get("report_period", plan.report_period)
    day = changes.get("report_due_day", plan.report_due_day)
    if "report_period" in changes and changes["report_period"] != plan.report_period:
        if "report_due_day" not in changes or (period is not None and day is None):
            raise _invalid(PERIOD_NEEDS_DAY)
    if day is not None and period is None:
        raise _invalid(DAY_NEEDS_PERIOD)
    if day is not None and day > DUE_DAY_MAX[period]:
        raise _invalid(BAD_DUE_DAY)


@router.patch("/{project_id}/plan", response_model=PlanOut)
def update_plan(project_id: int, body: PlanUpdate, user: Writer, db: Db) -> PlanOut:
    plan = _writable_plan(db, project_id)
    _check_version(plan, body.version)
    changes = _changes(body)
    _check_reporting(plan, changes)
    before = _snapshot(plan, PLAN_FIELDS)
    if not _apply(plan, changes):
        return _plan_out(db, plan)
    _save(db)
    _record(
        db,
        plan=plan,
        subject_type="project_plan",
        subject_id=plan.id,
        version=plan.version,
        action="update",
        user=user,
        before=before,
        after=_snapshot(plan, PLAN_FIELDS),
    )
    _commit(db)
    return _plan_out(db, plan)


def _list[T](
    db: Session, project_id: int, model: type[T], order_by: tuple, limit: int, offset: int
) -> tuple[list[T], int]:
    plan = _plan(db, project_id)
    stmt = select(model).where(model.plan_id == plan.id)  # type: ignore[attr-defined]
    return paginate(db, stmt, order_by=order_by, limit=limit, offset=offset)


def _create(
    db: Session, plan: ProjectPlan, row: Any, subject_type: str, fields: tuple, user: User
) -> None:
    db.add(row)
    db.flush()
    _touch(db, plan)
    _record(
        db,
        plan=plan,
        subject_type=subject_type,
        subject_id=row.id,
        version=row.version,
        action="create",
        user=user,
        before=None,
        after=_snapshot(row, fields),
    )
    _commit(db)


def _update(
    db: Session,
    plan: ProjectPlan,
    row: Any,
    changes: dict[str, Any],
    subject_type: str,
    fields: tuple,
    user: User,
) -> None:
    before = _snapshot(row, fields)
    if not _apply(row, changes):
        return
    _save(db)
    _touch(db, plan)
    _record(
        db,
        plan=plan,
        subject_type=subject_type,
        subject_id=row.id,
        version=row.version,
        action="update",
        user=user,
        before=before,
        after=_snapshot(row, fields),
    )
    _commit(db)


def _delete(
    db: Session,
    plan: ProjectPlan,
    row: Any,
    version: int,
    subject_type: str,
    fields: tuple,
    user: User,
    **extra: Any,
) -> None:
    """Remove an item; its audit row keeps what it was."""
    before = _snapshot(row, fields)
    db.delete(row)
    _save(db)
    _touch(db, plan)
    _record(
        db,
        plan=plan,
        subject_type=subject_type,
        subject_id=row.id,
        version=version + 1,
        action="delete",
        user=user,
        before=before,
        after=None,
        **extra,
    )
    _commit(db)


# --- Milestones


def _milestones_out(db: Session, rows: list[PlanMilestone]) -> list[MilestoneOut]:
    owner_ids = {r.owner_user_id for r in rows if r.owner_user_id is not None}
    owners = {}
    if owner_ids:
        owners = {u.id: u for u in db.scalars(select(User).where(User.id.in_(owner_ids)))}
    out = []
    for row in rows:
        owner = owners.get(row.owner_user_id) if row.owner_user_id is not None else None
        out.append(
            MilestoneOut.model_validate(row).model_copy(
                update={
                    "owner_name": owner.display_name if owner else None,
                    "owner_active": bool(owner and owner.is_active),
                }
            )
        )
    return out


@router.get("/{project_id}/plan/milestones", response_model=Page[MilestoneOut])
def list_milestones(
    project_id: int, _user: Reader, db: Db, limit: Limit = DEFAULT_LIMIT, offset: Offset = 0
) -> Page[MilestoneOut]:
    order = (PlanMilestone.target_date, PlanMilestone.id)
    rows, total = _list(db, project_id, PlanMilestone, order, limit, offset)
    return Page[MilestoneOut](
        items=_milestones_out(db, rows), total=total, limit=limit, offset=offset
    )


@router.post(
    "/{project_id}/plan/milestones",
    response_model=MilestoneOut,
    status_code=status.HTTP_201_CREATED,
)
def create_milestone(project_id: int, body: MilestoneCreate, user: Writer, db: Db) -> MilestoneOut:
    plan = _writable_plan(db, project_id)
    _check_version(plan, body.version)
    _check_user(db, body.owner_user_id, BAD_OWNER)
    row = PlanMilestone(plan_id=plan.id, **body.model_dump(exclude={"version"}))
    _create(db, plan, row, "plan_milestone", MILESTONE_FIELDS, user)
    return _milestones_out(db, [row])[0]


@router.patch("/{project_id}/plan/milestones/{milestone_id}", response_model=MilestoneOut)
def update_milestone(
    project_id: int, milestone_id: int, body: MilestoneUpdate, user: Writer, db: Db
) -> MilestoneOut:
    plan = _writable_plan(db, project_id)
    row = _item(db, PlanMilestone, plan, milestone_id, MILESTONE_NOT_FOUND)
    _check_version(row, body.version)
    changes = _changes(body)
    _check_user(db, changes.get("owner_user_id"), BAD_OWNER)
    _update(db, plan, row, changes, "plan_milestone", MILESTONE_FIELDS, user)
    return _milestones_out(db, [row])[0]


@router.delete(
    "/{project_id}/plan/milestones/{milestone_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_milestone(
    project_id: int,
    milestone_id: int,
    version: Annotated[int, Query(ge=1)],
    user: Writer,
    db: Db,
) -> Response:
    plan = _writable_plan(db, project_id)
    row = _item(db, PlanMilestone, plan, milestone_id, MILESTONE_NOT_FOUND)
    _check_version(row, version)
    delivered = list(
        db.scalars(
            select(PlanDeliverable)
            .where(PlanDeliverable.milestone_id == row.id)
            .order_by(PlanDeliverable.id)
            .with_for_update()
        )
    )
    for deliverable in delivered:
        deliverable.milestone_id = None
    _save(db)
    _delete(
        db,
        plan,
        row,
        version,
        "plan_milestone",
        MILESTONE_FIELDS,
        user,
        unassigned_deliverable_ids=[d.id for d in delivered],
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Deliverables


def _deliverables_out(db: Session, rows: list[PlanDeliverable]) -> list[DeliverableOut]:
    names = _names(
        db, PlanMilestone, {r.milestone_id for r in rows if r.milestone_id}, PlanMilestone.name
    )
    return [
        DeliverableOut.model_validate(r).model_copy(
            update={"milestone_name": names.get(r.milestone_id) if r.milestone_id else None}
        )
        for r in rows
    ]


def _check_milestone(db: Session, plan: ProjectPlan, milestone_id: int | None) -> None:
    if milestone_id is None:
        return
    found = db.scalar(
        select(PlanMilestone.id).where(
            PlanMilestone.id == milestone_id, PlanMilestone.plan_id == plan.id
        )
    )
    if found is None:
        raise _invalid(BAD_MILESTONE)


@router.get("/{project_id}/plan/deliverables", response_model=Page[DeliverableOut])
def list_deliverables(
    project_id: int, _user: Reader, db: Db, limit: Limit = DEFAULT_LIMIT, offset: Offset = 0
) -> Page[DeliverableOut]:
    rows, total = _list(db, project_id, PlanDeliverable, (PlanDeliverable.id,), limit, offset)
    return Page[DeliverableOut](
        items=_deliverables_out(db, rows), total=total, limit=limit, offset=offset
    )


@router.post(
    "/{project_id}/plan/deliverables",
    response_model=DeliverableOut,
    status_code=status.HTTP_201_CREATED,
)
def create_deliverable(
    project_id: int, body: DeliverableCreate, user: Writer, db: Db
) -> DeliverableOut:
    plan = _writable_plan(db, project_id)
    _check_version(plan, body.version)
    _check_milestone(db, plan, body.milestone_id)
    row = PlanDeliverable(plan_id=plan.id, **body.model_dump(exclude={"version"}))
    _create(db, plan, row, "plan_deliverable", DELIVERABLE_FIELDS, user)
    return _deliverables_out(db, [row])[0]


@router.patch("/{project_id}/plan/deliverables/{deliverable_id}", response_model=DeliverableOut)
def update_deliverable(
    project_id: int, deliverable_id: int, body: DeliverableUpdate, user: Writer, db: Db
) -> DeliverableOut:
    plan = _writable_plan(db, project_id)
    row = _item(db, PlanDeliverable, plan, deliverable_id, DELIVERABLE_NOT_FOUND)
    _check_version(row, body.version)
    changes = _changes(body)
    _check_milestone(db, plan, changes.get("milestone_id"))
    _update(db, plan, row, changes, "plan_deliverable", DELIVERABLE_FIELDS, user)
    return _deliverables_out(db, [row])[0]


@router.delete(
    "/{project_id}/plan/deliverables/{deliverable_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_deliverable(
    project_id: int,
    deliverable_id: int,
    version: Annotated[int, Query(ge=1)],
    user: Writer,
    db: Db,
) -> Response:
    plan = _writable_plan(db, project_id)
    row = _item(db, PlanDeliverable, plan, deliverable_id, DELIVERABLE_NOT_FOUND)
    _check_version(row, version)
    _delete(db, plan, row, version, "plan_deliverable", DELIVERABLE_FIELDS, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Estimate lines


@router.get("/{project_id}/plan/estimate-lines", response_model=Page[EstimateLineOut])
def list_estimate_lines(
    project_id: int, _user: Reader, db: Db, limit: Limit = DEFAULT_LIMIT, offset: Offset = 0
) -> Page[EstimateLineOut]:
    rows, total = _list(db, project_id, PlanEstimateLine, (PlanEstimateLine.id,), limit, offset)
    return Page[EstimateLineOut](
        items=[EstimateLineOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{project_id}/plan/estimate-lines",
    response_model=EstimateLineOut,
    status_code=status.HTTP_201_CREATED,
)
def create_estimate_line(
    project_id: int, body: EstimateLineCreate, user: Writer, db: Db
) -> EstimateLineOut:
    plan = _writable_plan(db, project_id)
    _check_version(plan, body.version)
    row = PlanEstimateLine(plan_id=plan.id, **body.model_dump(exclude={"version"}))
    _create(db, plan, row, "plan_estimate_line", LINE_FIELDS, user)
    return EstimateLineOut.model_validate(row)


@router.patch("/{project_id}/plan/estimate-lines/{line_id}", response_model=EstimateLineOut)
def update_estimate_line(
    project_id: int, line_id: int, body: EstimateLineUpdate, user: Writer, db: Db
) -> EstimateLineOut:
    plan = _writable_plan(db, project_id)
    row = _item(db, PlanEstimateLine, plan, line_id, LINE_NOT_FOUND)
    _check_version(row, body.version)
    _update(db, plan, row, _changes(body), "plan_estimate_line", LINE_FIELDS, user)
    return EstimateLineOut.model_validate(row)


@router.delete(
    "/{project_id}/plan/estimate-lines/{line_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_estimate_line(
    project_id: int,
    line_id: int,
    version: Annotated[int, Query(ge=1)],
    user: Writer,
    db: Db,
) -> Response:
    plan = _writable_plan(db, project_id)
    row = _item(db, PlanEstimateLine, plan, line_id, LINE_NOT_FOUND)
    _check_version(row, version)
    _delete(db, plan, row, version, "plan_estimate_line", LINE_FIELDS, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
