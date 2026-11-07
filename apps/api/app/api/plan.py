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
requests) and not `cancelled` (declined at the go-ahead gate, item 5.4): a 409 otherwise. Each
write carries a `version`: the item's for an edit or delete (a DELETE as `?version=`), the plan's
for a PATCH of the plan or an addition. Every write moves the
plan's version on, so the plan's version changes whenever anything in it does. A stale version is
a 409. Each write is one audit row (agent `project`, subject the plan or item, its new version)
with the values before and after.

Deleting a milestone leaves its deliverables without a milestone (they then show in `missing`);
its audit row lists them.

Each milestone, deliverable and estimate line has a `source`: `agent` for the planner agent's
draft, `person` for anything a person added or edited. An edit through these routes marks the
item `person`, so a re-draft never replaces it.

`POST /projects/{id}/plan/confirm` (`project:write`, item 6.6) marks the agent's items `person`
without changing anything else in them: one item (`kind`, `id` and the item's `version`) or, with
neither, every agent item (the plan's `version`). The same planning and version rules apply; it is
one audit row (action `confirm`, subject the item, or the plan for all) listing what it marked.
Confirming an item that is already a person's changes nothing.

`POST /projects/{id}/plan/draft` (`project:write`, item 6.5) runs the planner agent
(`app.agents.planner_agent`) on a project in planning, gated by `AGENT_MODE`: `off` refuses (409,
audited) and calls nothing; `shadow` returns the draft and saves nothing; `enforce` replaces the
agent's own items with the draft and fills the plan's empty fields, in one commit. With
`?dry_run=true` it returns the exact request and a cost ceiling and calls nothing.
"""

import logging
from decimal import ROUND_UP, Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.agents import planner_agent, risk_agent
from app.api.intake import _names
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
from app.audit import record_decision, record_refusal
from app.config import get_settings
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
from app.models.plan import AGENT_SOURCE, DUE_DAY_MAX, PERSON_SOURCE
from app.models.portfolio import CANCELLED
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
    PlanConfirm,
    PlanDraftResult,
    PlanOut,
    PlanUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["plans"])

NO_PLAN = "this project has no plan"
STALE = "This plan has changed since you loaded it. Reload and try again."
MILESTONE_NOT_FOUND = "milestone not found"
DELIVERABLE_NOT_FOUND = "deliverable not found"
LINE_NOT_FOUND = "estimate line not found"
NOT_PLANNING = (
    "the plan can be changed only while the project is in planning; after that, changes go "
    "through change requests"
)
CANCELLED_PROJECT = "this project is cancelled, so its plan can't change"
BAD_OWNER = "owner_user_id must be an active user"
BAD_MILESTONE = "milestone_id must be a milestone of this plan"
PERIOD_NEEDS_DAY = "changing report_period needs a report_due_day that fits it in the same change"
AGENT_OFF = "the planner agent is off (AGENT_MODE=off); a person can write the plan by hand"
AGENT_FAILED = "the planner agent could not finish; the failed run is recorded in the audit trail"
DAY_NEEDS_PERIOD = "report_due_day needs a report_period"
BAD_DUE_DAY = (
    "report_due_day must be 1 to 7 (Monday is 1) for weekly and fortnightly reports, "
    "and 1 to 28 for monthly ones"
)

PLAN_FIELDS = ("scope_in", "scope_out", "contingency_pct", "report_period", "report_due_day")
MILESTONE_FIELDS = (
    "name",
    "target_date",
    "owner_user_id",
    "description",
    "contributors",
    "source",
)
DELIVERABLE_FIELDS = ("name", "description", "milestone_id", "source")
LINE_FIELDS = ("description", "category", "amount", "source")


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
    """The plan, locked until commit on MySQL, if its project is still in planning and not
    cancelled."""
    project = _project(db, project_id)
    if project.status == CANCELLED:
        raise HTTPException(status.HTTP_409_CONFLICT, CANCELLED_PROJECT)
    if project.stage != STAGE:
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_PLANNING)
    return _plan(db, project_id, for_update=True)


def _item[T](db: Session, model: type[T], plan: ProjectPlan, item_id: int, missing_: str) -> T:
    stmt = select(model).where(model.id == item_id, model.plan_id == plan.id)  # type: ignore[attr-defined]
    row = db.scalar(stmt.with_for_update())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, missing_)
    return row


def _save(db: Session) -> None:
    try:
        db.flush()
    except StaleDataError:
        # Another write changed the plan between our read and this update.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE) from None


def _commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE) from None


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
        editable=project.stage == STAGE and project.status != CANCELLED,
    )


# --- The plan


@router.get("/{project_id}/plan", response_model=PlanOut)
def get_plan(project_id: int, _user: Reader, db: Db) -> PlanOut:
    return _plan_out(db, _plan(db, project_id))


def _check_reporting(plan: ProjectPlan, changes: dict[str, Any]) -> None:
    """Clearing the period with no due day clears the due day too (it means nothing without a
    period); a change to a different period needs a due day that fits it."""
    if (
        "report_period" in changes
        and changes["report_period"] is None
        and "report_due_day" not in changes
    ):
        changes["report_due_day"] = None
    period = changes.get("report_period", plan.report_period)
    day = changes.get("report_due_day", plan.report_due_day)
    if period is not None and period != plan.report_period:
        if "report_due_day" not in changes or day is None:
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
    # A person's edit makes the item theirs: a re-draft by the planner agent keeps it.
    row.source = PERSON_SOURCE
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
    row = PlanMilestone(
        plan_id=plan.id, source=PERSON_SOURCE, **body.model_dump(exclude={"version"})
    )
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
    row = PlanDeliverable(
        plan_id=plan.id, source=PERSON_SOURCE, **body.model_dump(exclude={"version"})
    )
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
    row = PlanEstimateLine(
        plan_id=plan.id, source=PERSON_SOURCE, **body.model_dump(exclude={"version"})
    )
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


# --- Confirming the agent's items (item 6.6)

# Each kind a person can confirm: its model, audit subject type, fields and not-found message.
CONFIRMABLE = {
    "milestone": (PlanMilestone, "plan_milestone", MILESTONE_FIELDS, MILESTONE_NOT_FOUND),
    "deliverable": (PlanDeliverable, "plan_deliverable", DELIVERABLE_FIELDS, DELIVERABLE_NOT_FOUND),
    "estimate_line": (PlanEstimateLine, "plan_estimate_line", LINE_FIELDS, LINE_NOT_FOUND),
}


@router.post("/{project_id}/plan/confirm", response_model=PlanOut)
def confirm_plan_items(project_id: int, body: PlanConfirm, user: Writer, db: Db) -> PlanOut:
    plan = _writable_plan(db, project_id)
    if body.kind is not None and body.id is not None:
        model, subject_type, fields, not_found = CONFIRMABLE[body.kind]
        row = _item(db, model, plan, body.id, not_found)
        _check_version(row, body.version)
        if row.source != AGENT_SOURCE:
            return _plan_out(db, plan)
        before = _snapshot(row, fields)
        row.source = PERSON_SOURCE
        _save(db)
        _touch(db, plan)
        _record(
            db,
            plan=plan,
            subject_type=subject_type,
            subject_id=row.id,
            version=row.version,
            action="confirm",
            user=user,
            before=before,
            after=_snapshot(row, fields),
        )
        _commit(db)
        return _plan_out(db, plan)

    _check_version(plan, body.version)
    confirmed: dict[str, list[int]] = {}
    for kind, (model, _subject, _fields, _missing) in CONFIRMABLE.items():
        rows = list(
            db.scalars(
                select(model)
                .where(model.plan_id == plan.id, model.source == AGENT_SOURCE)
                .order_by(model.id)
                .with_for_update()
            )
        )
        for row in rows:
            row.source = PERSON_SOURCE
        if rows:
            confirmed[kind] = [row.id for row in rows]
    if not confirmed:
        return _plan_out(db, plan)
    _save(db)
    _touch(db, plan)
    _record(
        db,
        plan=plan,
        subject_type="project_plan",
        subject_id=plan.id,
        version=plan.version,
        action="confirm",
        user=user,
        before={"source": AGENT_SOURCE},
        after={"source": PERSON_SOURCE},
        confirmed=confirmed,
    )
    _commit(db)
    return _plan_out(db, plan)


# --- The planner agent (item 6.5)


def get_claude_client() -> risk_agent.ClaudeClient:
    """The planner agent's Claude client; tests replace it through `dependency_overrides`."""
    return risk_agent.AnthropicClient(get_settings())


def _refuse_draft(db: Session, project: Project, user: User, refusal: str, detail: str) -> None:
    """Record a refused draft request, commit it, and answer 409."""
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    record_refusal(
        db,
        agent=AUDIT_AGENT,
        subject_type="project_plan" if plan is not None else "project",
        subject_id=str(plan.id if plan is not None else project.id),
        version=plan.version if plan is not None else project.version,
        actor_user_id=user.id,
        output=None,
        action_taken={"action": "draft_plan", "refusal": refusal, "project_id": project.id},
    )
    db.commit()
    raise HTTPException(status.HTTP_409_CONFLICT, detail)


@router.post("/{project_id}/plan/draft", response_model=PlanDraftResult)
def draft_plan(
    project_id: int,
    user: Writer,
    db: Db,
    claude: Annotated[risk_agent.ClaudeClient, Depends(get_claude_client)],
    dry_run: bool = False,
) -> PlanDraftResult:
    settings = get_settings()
    project = _project(db, project_id)
    if project.status == CANCELLED:
        _refuse_draft(db, project, user, "cancelled", CANCELLED_PROJECT)
    if project.stage != STAGE:
        _refuse_draft(db, project, user, "not_planning", NOT_PLANNING)
    if settings.agent_mode == "off":
        _refuse_draft(db, project, user, "agent_off", AGENT_OFF)
    common = {
        "mode": settings.agent_mode,
        "dry_run": dry_run,
        "model": settings.planner_agent_model,
        "prompt_version": planner_agent.PROMPT_VERSION,
    }
    if dry_run:
        plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
        data = planner_agent.request_data(db, project, plan, utcnow().date())
        api_request = planner_agent.build_request(data, settings)
        ceiling = planner_agent.cost_ceiling(api_request, settings)
        return PlanDraftResult(
            **common,
            request=api_request,
            cost_ceiling_usd=ceiling.quantize(Decimal("0.01"), rounding=ROUND_UP),
        )
    try:
        result = planner_agent.run(
            db,
            claude,
            project,
            settings=settings,
            save_it=settings.agent_mode == "enforce",
            actor_user_id=user.id,
        )
    except risk_agent.AgentError:
        logger.exception("planner agent run failed for project %s", project_id)
        db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, AGENT_FAILED) from None
    _commit(db)
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    draft = planner_agent.draft_json(result.checked)
    return PlanDraftResult(
        **common,
        draft={k: v for k, v in draft.items() if k not in ("dropped", "changed")},
        dropped=result.checked.dropped,
        changed=result.checked.changed,
        saved=result.saved.as_dict(),
        plan_version=plan.version if plan is not None else None,
        audit_id=result.audit_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )
