"""Delivery progress (item 7.2): what a project in delivery has reached, how its deliverables
stand and what it has spent, recorded so later reports read it from records.

- `GET /projects/{id}/delivery` (`project:read`): each milestone with its baseline target, its
  plan date now, the reached date and days late in working days against the baseline target;
  each deliverable with its status; the current spend figure (the newest entry) and every entry,
  newest first and paged; and the current baseline's number and total.
- `PUT .../delivery/milestones/{mid}` (`project:write`): mark a milestone reached on a date (not
  after today, UTC) with an optional note, or clear it. Carries the milestone's `reached_version`.
- `PUT .../delivery/deliverables/{did}` (`project:write`): move a deliverable between
  `not_started`, `in_progress` and `ready_for_acceptance`. Carries its `status_version`. Setting
  `accepted` is refused (422), and so is moving a deliverable that is already accepted (409):
  only the acceptance step sets or leaves it.
- `POST .../delivery/spend` (`project:write`): add a spend entry. Entries are never edited; a
  correction is a new entry whose `supersedes_id` must be the project's newest entry, or it is a
  409 (`stale_spend`). A new entry with no `supersedes_id` needs no version, and none of this
  touches the project's `version`, which guards gate decisions.

Every write needs a project in delivery that isn't cancelled (otherwise a 409). None of them
changes the plan or the baseline: dates, scope and cost still change only by a change request.
Each write is one audit row (agent `delivery`, trigger `human`, the person's id), and so is every
refused attempt, including a role without `project:write`.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import delivery as rules
from app.api.intake import _commit, _names
from app.api.projects import Db, Reader
from app.audit import next_attempt, record_decision, record_refusal
from app.authz import Action, require_audited
from app.baselines import latest_baseline
from app.models import PlanDeliverable, PlanMilestone, Project, ProjectPlan, ProjectSpend, User
from app.models.plan import ACCEPTED
from app.plans import CENT, milestones_of
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page, paginate
from app.schemas.delivery import (
    DeliverableProgressOut,
    DeliverableStatusIn,
    DeliveryOut,
    MilestoneProgressOut,
    MilestoneReachedIn,
    SpendIn,
    SpendOut,
)

router = APIRouter(prefix="/projects", tags=["delivery"])

AUDIT_AGENT = "delivery"
PROJECT_SUBJECT = "project"
MILESTONE_SUBJECT = "plan_milestone"
DELIVERABLE_SUBJECT = "plan_deliverable"
SPEND_SUBJECT = "project_spend"

PROJECT_NOT_FOUND = "project not found"
MILESTONE_NOT_FOUND = "milestone not found"
DELIVERABLE_NOT_FOUND = "deliverable not found"
STALE = "this has changed since you read it; reload it and try again"
FUTURE_DATE = "a milestone can't be reached after today"
ACCEPTED_HERE = "only the acceptance step sets a deliverable to accepted"
ALREADY_ACCEPTED = "this deliverable is accepted; only the acceptance step changes it"
STALE_SPEND = (
    "a correction must name the project's newest spend entry; reload the spend and try again"
)


# --- Loading


def _project(db: Session, project_id: int, *, for_update: bool = False) -> Project:
    stmt = select(Project).where(Project.id == project_id)
    project = db.scalar(stmt.with_for_update() if for_update else stmt)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, PROJECT_NOT_FOUND)
    return project


def _item[T](
    db: Session, model: type[T], project_id: int, item_id: int, missing: str, *, lock: bool
) -> T:
    stmt = (
        select(model)
        .join(ProjectPlan, ProjectPlan.id == model.plan_id)  # type: ignore[attr-defined]
        .where(model.id == item_id, ProjectPlan.project_id == project_id)  # type: ignore[attr-defined]
    )
    row = db.scalar(stmt.with_for_update() if lock else stmt)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, missing)
    return row


def _newest_spend(db: Session, project_id: int) -> ProjectSpend | None:
    return db.scalar(
        select(ProjectSpend)
        .where(ProjectSpend.project_id == project_id)
        .order_by(ProjectSpend.id.desc())
        .limit(1)
    )


# --- Audit


def _milestone_snapshot(milestone: PlanMilestone) -> dict[str, Any]:
    return {
        "name": milestone.name,
        "plan_date": milestone.target_date.isoformat(),
        "reached_on": milestone.reached_on.isoformat() if milestone.reached_on else None,
        "reached_note": milestone.reached_note,
        "reached_version": milestone.reached_version,
    }


def _deliverable_snapshot(deliverable: PlanDeliverable) -> dict[str, Any]:
    return {
        "name": deliverable.name,
        "status": deliverable.status,
        "status_version": deliverable.status_version,
    }


def _spend_snapshot(entry: ProjectSpend) -> dict[str, Any]:
    return {
        "amount": str(Decimal(entry.amount).quantize(CENT, rounding=ROUND_HALF_UP)),
        "as_of": entry.as_of.isoformat(),
        "note": entry.note,
        "supersedes_id": entry.supersedes_id,
    }


def _subject(
    project: Project, item: PlanMilestone | PlanDeliverable | None
) -> tuple[str, int, int, dict[str, Any]]:
    """The audit subject for an attempt: the milestone or deliverable when there is one, else
    the project."""
    if isinstance(item, PlanMilestone):
        return MILESTONE_SUBJECT, item.id, item.reached_version, _milestone_snapshot(item)
    if isinstance(item, PlanDeliverable):
        return DELIVERABLE_SUBJECT, item.id, item.status_version, _deliverable_snapshot(item)
    return (
        PROJECT_SUBJECT,
        project.id,
        project.version,
        {"stage": project.stage, "status": project.status},
    )


def _refuse(
    db: Session,
    user: User,
    project: Project,
    item: PlanMilestone | PlanDeliverable | None,
    *,
    action: str,
    refusal: str,
    code: int,
    detail: str,
    **extra: Any,
) -> HTTPException:
    """Record and commit a refused attempt, and give the error to raise."""
    subject_type, subject_id, version, output = _subject(project, item)
    record_refusal(
        db,
        agent=AUDIT_AGENT,
        subject_type=subject_type,
        subject_id=str(subject_id),
        version=version,
        actor_user_id=user.id,
        output=output,
        action_taken={
            "action": f"{action}_refused",
            "project_id": project.id,
            "refusal": refusal,
            "actor_role": user.role,
            **extra,
        },
    )
    _commit(db)
    return HTTPException(code, detail)


def _record(
    db: Session,
    user: User,
    *,
    subject_type: str,
    subject_id: int,
    version: int,
    before: dict[str, Any] | None,
    output: dict[str, Any],
    action: str,
    project_id: int,
) -> None:
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=subject_type,
        subject_id=str(subject_id),
        version=version,
        attempt=next_attempt(
            db,
            agent=AUDIT_AGENT,
            subject_type=subject_type,
            subject_id=str(subject_id),
            version=version,
        ),
        status="ok",
        actor_user_id=user.id,
        signals=before,
        output=output,
        action_taken={"action": action, "project_id": project_id},
    )


def _refusal_recorder(action: str):
    """The guard's refusal row for a role without `project:write`. An unknown project, milestone
    or deliverable has nothing to name, so it gets the 403 without a row."""

    def record(db: Session, user: User, path_params: dict[str, str]) -> None:
        try:
            project = _project(db, int(path_params["project_id"]))
            item: PlanMilestone | PlanDeliverable | None = None
            if "milestone_id" in path_params:
                item = _item(
                    db,
                    PlanMilestone,
                    project.id,
                    int(path_params["milestone_id"]),
                    MILESTONE_NOT_FOUND,
                    lock=False,
                )
            elif "deliverable_id" in path_params:
                item = _item(
                    db,
                    PlanDeliverable,
                    project.id,
                    int(path_params["deliverable_id"]),
                    DELIVERABLE_NOT_FOUND,
                    lock=False,
                )
        except (KeyError, ValueError, HTTPException):
            return
        subject_type, subject_id, version, output = _subject(project, item)
        record_refusal(
            db,
            agent=AUDIT_AGENT,
            subject_type=subject_type,
            subject_id=str(subject_id),
            version=version,
            actor_user_id=user.id,
            output=output,
            action_taken={
                "action": f"{action}_refused",
                "project_id": project.id,
                "refusal": "no_permission",
                "actor_role": user.role,
            },
        )

    return record


def _writer(action: str):
    return Annotated[
        User, Depends(require_audited(Action.PROJECT_WRITE, _refusal_recorder(action)))
    ]


MilestoneWriter = _writer("milestone_reached")
DeliverableWriter = _writer("deliverable_status")
SpendWriter = _writer("spend")


def _locked_project(db: Session, user: User, project_id: int, action: str) -> Project:
    """The project, locked until commit on MySQL so writes on one project take turns, and
    checked to be in delivery and not cancelled. The guard's read-only transaction ends first,
    so a write that waited sees what the one before it committed."""
    db.rollback()
    project = _project(db, project_id, for_update=True)
    refused = rules.refusal(project)
    if refused is not None:
        raise _refuse(
            db,
            user,
            project,
            None,
            action=action,
            refusal=refused[0],
            code=status.HTTP_409_CONFLICT,
            detail=refused[1],
        )
    return project


# --- Output


def _spend_out(db: Session, rows: list[ProjectSpend]) -> list[SpendOut]:
    people = _names(db, User, {r.created_by for r in rows}, User.display_name)
    return [
        SpendOut.model_validate(row).model_copy(
            update={"created_by_name": people.get(row.created_by)}
        )
        for row in rows
    ]


def _view(db: Session, project: Project, *, limit: int = DEFAULT_LIMIT, offset: int = 0):
    baseline = latest_baseline(db, project.id)
    targets = rules.baseline_targets(baseline)
    on = rules.today()
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    milestones = milestones_of(db, plan.id) if plan else []
    deliverables = (
        list(
            db.scalars(
                select(PlanDeliverable)
                .where(PlanDeliverable.plan_id == plan.id)
                .order_by(PlanDeliverable.id)
            )
        )
        if plan
        else []
    )
    rows, total = paginate(
        db,
        select(ProjectSpend).where(ProjectSpend.project_id == project.id),
        order_by=(ProjectSpend.id.desc(),),
        limit=limit,
        offset=offset,
    )
    newest = _newest_spend(db, project.id)
    return DeliveryOut(
        project_id=project.id,
        project_stage=project.stage,
        project_status=project.status,
        baseline_number=baseline.number if baseline else None,
        baseline_total=baseline.total if baseline else None,
        today=on,
        milestones=[
            MilestoneProgressOut(
                id=m.id,
                name=m.name,
                target_date=targets.get(m.id),
                plan_date=m.target_date,
                reached_on=m.reached_on,
                reached_note=m.reached_note,
                days_late=rules.days_late(targets.get(m.id), m.reached_on, on),
                version=m.reached_version,
            )
            for m in milestones
        ],
        deliverables=[
            DeliverableProgressOut(
                id=d.id,
                name=d.name,
                milestone_id=d.milestone_id,
                status=d.status,
                version=d.status_version,
            )
            for d in deliverables
        ],
        current_spend=_spend_out(db, [newest])[0] if newest else None,
        spend=Page[SpendOut](items=_spend_out(db, rows), total=total, limit=limit, offset=offset),
    )


# --- Routes


@router.get("/{project_id}/delivery", response_model=DeliveryOut)
def get_delivery(
    project_id: int,
    _user: Reader,
    db: Db,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> DeliveryOut:
    return _view(db, _project(db, project_id), limit=limit, offset=offset)


@router.put("/{project_id}/delivery/milestones/{milestone_id}", response_model=DeliveryOut)
def set_milestone_reached(
    project_id: int, milestone_id: int, body: MilestoneReachedIn, user: MilestoneWriter, db: Db
) -> DeliveryOut:
    action = "milestone_reached"
    project = _locked_project(db, user, project_id, action)
    milestone = _item(db, PlanMilestone, project.id, milestone_id, MILESTONE_NOT_FOUND, lock=True)
    if milestone.reached_version != body.version:
        raise _refuse(
            db,
            user,
            project,
            milestone,
            action=action,
            refusal="stale_version",
            code=status.HTTP_409_CONFLICT,
            detail=STALE,
        )
    if body.reached_on is not None and body.reached_on > rules.today():
        raise _refuse(
            db,
            user,
            project,
            milestone,
            action=action,
            refusal="future_date",
            code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=FUTURE_DATE,
            reached_on=body.reached_on.isoformat(),
        )
    before = _milestone_snapshot(milestone)
    if not rules.set_reached(db, milestone, body.version, body.reached_on, body.reached_note):
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE)
    _record(
        db,
        user,
        subject_type=MILESTONE_SUBJECT,
        subject_id=milestone.id,
        version=milestone.reached_version,
        before=before,
        output=_milestone_snapshot(milestone),
        action="milestone_reached" if body.reached_on else "milestone_reached_cleared",
        project_id=project.id,
    )
    _commit(db)
    return _view(db, project)


@router.put("/{project_id}/delivery/deliverables/{deliverable_id}", response_model=DeliveryOut)
def set_deliverable_status(
    project_id: int,
    deliverable_id: int,
    body: DeliverableStatusIn,
    user: DeliverableWriter,
    db: Db,
) -> DeliveryOut:
    action = "deliverable_status"
    project = _locked_project(db, user, project_id, action)
    deliverable = _item(
        db, PlanDeliverable, project.id, deliverable_id, DELIVERABLE_NOT_FOUND, lock=True
    )
    for refusal, refused, code, detail in (
        (
            "accepted_not_allowed",
            body.status == ACCEPTED,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ACCEPTED_HERE,
        ),
        (
            "stale_version",
            deliverable.status_version != body.version,
            status.HTTP_409_CONFLICT,
            STALE,
        ),
        (
            "already_accepted",
            deliverable.status == ACCEPTED,
            status.HTTP_409_CONFLICT,
            ALREADY_ACCEPTED,
        ),
    ):
        if refused:
            raise _refuse(
                db,
                user,
                project,
                deliverable,
                action=action,
                refusal=refusal,
                code=code,
                detail=detail,
                requested_status=body.status,
            )
    before = _deliverable_snapshot(deliverable)
    if not rules.set_status(db, deliverable, body.version, body.status):
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE)
    _record(
        db,
        user,
        subject_type=DELIVERABLE_SUBJECT,
        subject_id=deliverable.id,
        version=deliverable.status_version,
        before=before,
        output=_deliverable_snapshot(deliverable),
        action="deliverable_status_set",
        project_id=project.id,
    )
    _commit(db)
    return _view(db, project)


@router.post("/{project_id}/delivery/spend", response_model=DeliveryOut, status_code=201)
def record_spend(project_id: int, body: SpendIn, user: SpendWriter, db: Db) -> DeliveryOut:
    action = "spend"
    project = _locked_project(db, user, project_id, action)
    newest = _newest_spend(db, project.id)
    if body.supersedes_id is not None and (newest is None or newest.id != body.supersedes_id):
        raise _refuse(
            db,
            user,
            project,
            None,
            action=action,
            refusal="stale_spend",
            code=status.HTTP_409_CONFLICT,
            detail=STALE_SPEND,
            supersedes_id=body.supersedes_id,
            newest_id=newest.id if newest else None,
        )
    entry = ProjectSpend(
        project_id=project.id,
        amount=body.amount,
        as_of=body.as_of,
        note=body.note,
        supersedes_id=body.supersedes_id,
        created_by=user.id,
    )
    db.add(entry)
    db.flush()
    _record(
        db,
        user,
        subject_type=SPEND_SUBJECT,
        subject_id=entry.id,
        version=1,
        before=_spend_snapshot(newest) if newest else None,
        output=_spend_snapshot(entry),
        action="spend_corrected" if body.supersedes_id else "spend_recorded",
        project_id=project.id,
    )
    _commit(db)
    return _view(db, project)
