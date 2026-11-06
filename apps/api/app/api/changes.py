"""Change requests (item 5.5): once a project is in delivery, its scope, dates and cost change only
through a change request that a person decides.

- `GET /projects/{id}/changes` (`project:read`) is the change log, newest first and paged: each
  change with its computed effect, its decider, who decided, the reason and the baseline it
  produced. It also says whether a client proposal is out of date against the plan.
- `POST /projects/{id}/changes` (`project:write`) raises a draft; `PATCH .../{cid}` edits a draft;
  `POST .../{cid}/submit` submits it. Each of the three measures the change against the project's
  current baseline and stores the effect and the decider (`app.changes`); a client never sends
  them. Submitting a change that is already submitted measures it again (resubmitting).
- `POST .../{cid}/withdraw` (`project:write`): its raiser or the project's project manager
  withdraws a draft or submitted change.
- `POST .../{cid}/decide` approves or rejects a submitted change with a reason of at least 20
  characters. A `sponsor` change is decided under the go-ahead gate's rules
  (`app.api.gates.go_ahead_decider_refusal`: `gate:decide`, a role the gate lists, not the
  project's own project manager). A `project_manager` change is decided only by the project's own
  project manager, with `project:write`. Nobody decides a change they raised. A change measured
  against an older baseline than the current one is a 409: resubmit it.

Approving applies the change to the plan and freezes the next baseline (2, 3 and so on) with the
change request's id, in one transaction with the decision and its audit row, which records the
new baseline's id. The result must still be a valid plan, or the approval is a 409 naming the
problem and nothing changes. Rejecting changes nothing but the change request. A client proposal
is not changed: its checker flags it against the new plan until it is refreshed and re-approved.

All writes need a project in delivery that isn't cancelled (otherwise a 409), and carry the
change's `version` (stale is a 409). Approved, rejected and withdrawn are final. Each write is one
audit row (agent `change`), and so is every refused attempt, 403 or 409. The policy comes from the
changes policy file (`app.policy.get_changes`); until a person applies it, every change route
answers 503.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app import changes as change_rules
from app import policy
from app.api.gates import go_ahead_decider_refusal
from app.api.intake import _commit, _names
from app.api.projects import Db, Reader
from app.audit import next_attempt, record_decision, record_refusal
from app.authz import Action, can, require_any_audited, require_audited
from app.baselines import freeze, latest_baseline
from app.models import ChangeRequest, Project, ProjectBaseline, ProjectPlan, ProjectProposal, User
from app.models.base import utcnow
from app.models.change import FINAL_STATUSES
from app.models.portfolio import CANCELLED
from app.policy import ChangesPolicy, PolicyError
from app.proposals import check, plan_facts, values_of
from app.schemas.change import (
    ChangeCreate,
    ChangeDecisionIn,
    ChangeOut,
    ChangesPage,
    ChangeUpdate,
    ChangeWrite,
)
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, paginate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["changes"])

AUDIT_AGENT = "change"
SUBJECT = "change_request"
PROJECT_SUBJECT = "project"
DELIVERY = "delivery"
SPONSOR_LEVEL = "sponsor"

UNAVAILABLE = (
    "change requests aren't available yet: a person needs to apply the changes policy file"
)
PROJECT_NOT_FOUND = "project not found"
NOT_FOUND = "change request not found"
NOT_IN_DELIVERY = (
    "change requests are for a project in delivery; while it is in planning, edit the plan"
)
CANCELLED_PROJECT = "this project is cancelled and takes no further changes"
NO_BASELINE = "this project has no baseline to measure a change against"
STALE = "this change request has changed since you read it; reload it and try again"
FINAL = "an approved, rejected or withdrawn change request is final"
NOT_DRAFT = "only a draft change request can be edited"
NOT_SUBMITTED = "only a submitted change request can be decided"
NOT_YOURS_TO_WITHDRAW = "only the change's raiser or the project's project manager can withdraw it"
OWN_CHANGE = "you cannot decide a change request you raised"
NOT_THE_PROJECT_MANAGER = (
    "this change is decided by the project's project manager, and you are not that person"
)
NEWER_BASELINE = (
    "a newer baseline has been made since this change was measured; resubmit it so its effect "
    "and decider are measured again"
)
NOT_SAVED = "the decision could not be saved, so nothing changed and no decision was recorded"

OUTCOME_STATUS = {"approve": "approved", "reject": "rejected"}


# --- Loading


def _policy() -> ChangesPolicy:
    try:
        return policy.get_changes()
    except PolicyError:
        logger.exception("changes policy failed to load")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None


def _project(db: Session, project_id: int, *, for_update: bool = False) -> Project:
    stmt = select(Project).where(Project.id == project_id)
    project = db.scalar(stmt.with_for_update() if for_update else stmt)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, PROJECT_NOT_FOUND)
    return project


def _change(
    db: Session, project_id: int, change_id: int, *, for_update: bool = False
) -> ChangeRequest:
    stmt = select(ChangeRequest).where(
        ChangeRequest.id == change_id, ChangeRequest.project_id == project_id
    )
    change = db.scalar(stmt.with_for_update() if for_update else stmt)
    if change is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return change


def _locked(db: Session, project_id: int, change_id: int) -> tuple[Project, ChangeRequest]:
    """The project and its change request, locked until commit on MySQL so writes on one project
    take turns. The guard's read-only transaction ends first, so a write that waited sees what
    the one before it committed (as the gate routes do)."""
    db.rollback()
    project = _project(db, project_id, for_update=True)
    return project, _change(db, project_id, change_id, for_update=True)


# --- Audit


def _snapshot(change: ChangeRequest) -> dict[str, Any]:
    return {
        "project_id": change.project_id,
        "title": change.title,
        "status": change.status,
        "decider": change.decider,
        "measured_baseline_number": change.measured_baseline_number,
        "effect": change.effect,
        "version": change.version,
    }


def _refuse(
    db: Session,
    user: User,
    *,
    project: Project,
    change: ChangeRequest | None,
    action: str,
    refusal: str,
    code: int,
    detail: str,
) -> HTTPException:
    """Record and commit a refused attempt on a change (or, before one exists, on its project),
    and give the error to raise."""
    if change is not None:
        subject_type, subject_id, version = SUBJECT, change.id, change.version
        output: dict[str, Any] = _snapshot(change)
    else:
        subject_type, subject_id, version = PROJECT_SUBJECT, project.id, project.version
        output = {"stage": project.stage, "status": project.status}
    record_refusal(
        db,
        agent=AUDIT_AGENT,
        subject_type=subject_type,
        subject_id=str(subject_id),
        version=version,
        actor_user_id=user.id,
        output=output,
        action_taken={
            "action": f"change_{action}_refused",
            "project_id": project.id,
            "refusal": refusal,
            "actor_role": user.role,
        },
    )
    _commit(db)
    return HTTPException(code, detail)


def _record(
    db: Session,
    user: User,
    change: ChangeRequest,
    *,
    action: str,
    before: dict[str, Any] | None,
    **extra: Any,
) -> None:
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=SUBJECT,
        subject_id=str(change.id),
        version=change.version,
        attempt=next_attempt(
            db,
            agent=AUDIT_AGENT,
            subject_type=SUBJECT,
            subject_id=str(change.id),
            version=change.version,
        ),
        status="ok",
        actor_user_id=user.id,
        signals=before,
        output=_snapshot(change),
        action_taken={"action": action, "project_id": change.project_id, **extra},
    )


def _refusal_recorder(action: str):
    """The guard's refusal row for a role without the route's action. An unknown project or
    change has nothing to name, so it gets the 403 without a row."""

    def record(db: Session, user: User, path_params: dict[str, str]) -> None:
        try:
            project = _project(db, int(path_params["project_id"]))
            change = (
                _change(db, project.id, int(path_params["change_id"]))
                if "change_id" in path_params
                else None
            )
        except (KeyError, ValueError, HTTPException):
            return
        record_refusal(
            db,
            agent=AUDIT_AGENT,
            subject_type=SUBJECT if change else PROJECT_SUBJECT,
            subject_id=str(change.id if change else project.id),
            version=change.version if change else project.version,
            actor_user_id=user.id,
            output=_snapshot(change) if change else {"stage": project.stage},
            action_taken={
                "action": f"change_{action}_refused",
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


Raiser = _writer("raise")
Editor = _writer("edit")
Submitter = _writer("submit")
Withdrawer = _writer("withdraw")
Decider = Annotated[
    User,
    Depends(
        require_any_audited(
            frozenset({Action.GATE_DECIDE, Action.PROJECT_WRITE}), _refusal_recorder("decide")
        )
    ),
]


# --- The rules


def _project_refusal(project: Project) -> tuple[str, str] | None:
    if project.status == CANCELLED:
        return "cancelled", CANCELLED_PROJECT
    if project.stage != DELIVERY:
        return "not_in_delivery", NOT_IN_DELIVERY
    return None


def _check_project(
    db: Session, user: User, project: Project, change: ChangeRequest | None, action: str
) -> None:
    refused = _project_refusal(project)
    if refused is not None:
        raise _refuse(
            db,
            user,
            project=project,
            change=change,
            action=action,
            refusal=refused[0],
            code=status.HTTP_409_CONFLICT,
            detail=refused[1],
        )


def _check_version(
    db: Session, user: User, project: Project, change: ChangeRequest, version: int, action: str
) -> None:
    if change.version != version:
        raise _refuse(
            db,
            user,
            project=project,
            change=change,
            action=action,
            refusal="stale_version",
            code=status.HTTP_409_CONFLICT,
            detail=STALE,
        )
    if change.status in FINAL_STATUSES:
        raise _refuse(
            db,
            user,
            project=project,
            change=change,
            action=action,
            refusal="final_status",
            code=status.HTTP_409_CONFLICT,
            detail=FINAL,
        )


def _measure(
    db: Session, rules: ChangesPolicy, project: Project, raised_by: int, changes: dict[str, Any]
) -> tuple[dict[str, Any], str, int]:
    """The change's effect and decider against the current baseline, and that baseline's number.
    Naming an item the baseline doesn't have is a 422."""
    baseline = latest_baseline(db, project.id)
    if baseline is None:  # a project in delivery passed the go-ahead gate, which made one
        raise HTTPException(status.HTTP_409_CONFLICT, NO_BASELINE)
    problems = change_rules.unknown_items(baseline.plan, changes)
    if problems:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "; ".join(problems))
    effect, decider = change_rules.compute_effect(
        baseline.plan,
        changes,
        rules,
        baseline_number=baseline.number,
        has_project_manager=project.pm_user_id is not None,
        raised_by_project_manager=raised_by == project.pm_user_id,
    )
    return effect, decider, baseline.number


def _decider_refusal(user: User, project: Project, change: ChangeRequest) -> tuple[str, str] | None:
    """Why `user` may not decide `change`, or None when they may."""
    if change.raised_by == user.id:
        return "own_change", OWN_CHANGE
    if change.decider == SPONSOR_LEVEL:
        return go_ahead_decider_refusal(user, project)
    if not can(user.role, Action.PROJECT_WRITE) or project.pm_user_id != user.id:
        return "not_the_project_manager", NOT_THE_PROJECT_MANAGER
    return None


# --- Output


def _out(db: Session, rows: list[ChangeRequest]) -> list[ChangeOut]:
    people = _names(
        db,
        User,
        {r.raised_by for r in rows} | {r.decided_by for r in rows if r.decided_by},
        User.display_name,
    )
    ids = [r.id for r in rows]
    made = (
        dict(
            db.execute(
                select(ProjectBaseline.change_request_id, ProjectBaseline.number).where(
                    ProjectBaseline.change_request_id.in_(ids)
                )
            ).all()
        )
        if ids
        else {}
    )
    return [
        ChangeOut.model_validate(row).model_copy(
            update={
                "raised_by_name": people.get(row.raised_by),
                "decided_by_name": people.get(row.decided_by) if row.decided_by else None,
                "baseline_number": made.get(row.id),
            }
        )
        for row in rows
    ]


def _one(db: Session, change: ChangeRequest) -> ChangeOut:
    return _out(db, [change])[0]


def _proposal_out_of_date(db: Session, project_id: int) -> bool:
    proposal = db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == project_id))
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project_id))
    if proposal is None or plan is None:
        return False
    return bool(check(values_of(proposal), plan_facts(db, plan)))


# --- Routes


@router.get("/{project_id}/changes", response_model=ChangesPage)
def list_changes(
    project_id: int,
    _user: Reader,
    db: Db,
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> ChangesPage:
    _policy()
    project = _project(db, project_id)
    rows, total = paginate(
        db,
        select(ChangeRequest).where(ChangeRequest.project_id == project_id),
        order_by=(ChangeRequest.id.desc(),),
        limit=limit,
        offset=offset,
    )
    baseline = latest_baseline(db, project_id)
    return ChangesPage(
        items=_out(db, rows),
        total=total,
        limit=limit,
        offset=offset,
        project_stage=project.stage,
        project_status=project.status,
        pm_user_id=project.pm_user_id,
        current_baseline_number=baseline.number if baseline else None,
        proposal_out_of_date=_proposal_out_of_date(db, project_id),
    )


@router.post("/{project_id}/changes", response_model=ChangeOut, status_code=201)
def raise_change(project_id: int, body: ChangeCreate, user: Raiser, db: Db) -> ChangeOut:
    rules = _policy()
    db.rollback()
    project = _project(db, project_id, for_update=True)
    _check_project(db, user, project, None, "raise")
    changes = body.changes.model_dump(mode="json")
    effect, decider, number = _measure(db, rules, project, user.id, changes)
    change = ChangeRequest(
        project_id=project.id,
        title=body.title,
        reason=body.reason,
        changes=changes,
        effect=effect,
        decider=decider,
        measured_baseline_number=number,
        status="draft",
        raised_by=user.id,
    )
    db.add(change)
    db.flush()
    _record(db, user, change, action="change_raised", before=None, reason=body.reason)
    _commit(db)
    return _one(db, change)


def _save(db: Session) -> None:
    try:
        db.flush()
    except StaleDataError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE) from None


@router.patch("/{project_id}/changes/{change_id}", response_model=ChangeOut)
def edit_change(
    project_id: int, change_id: int, body: ChangeUpdate, user: Editor, db: Db
) -> ChangeOut:
    rules = _policy()
    project, change = _locked(db, project_id, change_id)
    _check_project(db, user, project, change, "edit")
    _check_version(db, user, project, change, body.version, "edit")
    if change.status != "draft":
        raise _refuse(
            db,
            user,
            project=project,
            change=change,
            action="edit",
            refusal="not_draft",
            code=status.HTTP_409_CONFLICT,
            detail=NOT_DRAFT,
        )
    before = _snapshot(change)
    sent = body.model_fields_set
    if "title" in sent and body.title is not None:
        change.title = body.title
    if "reason" in sent and body.reason is not None:
        change.reason = body.reason
    if "changes" in sent and body.changes is not None:
        change.changes = body.changes.model_dump(mode="json")
    change.effect, change.decider, change.measured_baseline_number = _measure(
        db, rules, project, change.raised_by, change.changes
    )
    change.updated_at = utcnow()
    _save(db)
    _record(db, user, change, action="change_edited", before=before, changes=change.changes)
    _commit(db)
    return _one(db, change)


@router.post("/{project_id}/changes/{change_id}/submit", response_model=ChangeOut)
def submit_change(
    project_id: int, change_id: int, body: ChangeWrite, user: Submitter, db: Db
) -> ChangeOut:
    rules = _policy()
    project, change = _locked(db, project_id, change_id)
    _check_project(db, user, project, change, "submit")
    _check_version(db, user, project, change, body.version, "submit")
    before = _snapshot(change)
    resubmitted = change.status == "submitted"
    change.effect, change.decider, change.measured_baseline_number = _measure(
        db, rules, project, change.raised_by, change.changes
    )
    change.status = "submitted"
    change.submitted_at = utcnow()
    _save(db)
    _record(
        db,
        user,
        change,
        action="change_resubmitted" if resubmitted else "change_submitted",
        before=before,
    )
    _commit(db)
    return _one(db, change)


@router.post("/{project_id}/changes/{change_id}/withdraw", response_model=ChangeOut)
def withdraw_change(
    project_id: int, change_id: int, body: ChangeWrite, user: Withdrawer, db: Db
) -> ChangeOut:
    _policy()
    project, change = _locked(db, project_id, change_id)
    if user.id not in (change.raised_by, project.pm_user_id):
        raise _refuse(
            db,
            user,
            project=project,
            change=change,
            action="withdraw",
            refusal="not_raiser_or_project_manager",
            code=status.HTTP_403_FORBIDDEN,
            detail=NOT_YOURS_TO_WITHDRAW,
        )
    _check_project(db, user, project, change, "withdraw")
    _check_version(db, user, project, change, body.version, "withdraw")
    before = _snapshot(change)
    change.status = "withdrawn"
    _save(db)
    _record(db, user, change, action="change_withdrawn", before=before)
    _commit(db)
    return _one(db, change)


@router.post("/{project_id}/changes/{change_id}/decide", response_model=ChangeOut)
def decide_change(
    project_id: int, change_id: int, body: ChangeDecisionIn, user: Decider, db: Db
) -> ChangeOut:
    _policy()
    project, change = _locked(db, project_id, change_id)
    refused = _decider_refusal(user, project, change)
    if refused is not None:
        raise _refuse(
            db,
            user,
            project=project,
            change=change,
            action="decide",
            refusal=refused[0],
            code=status.HTTP_403_FORBIDDEN,
            detail=refused[1],
        )
    _check_project(db, user, project, change, "decide")
    _check_version(db, user, project, change, body.version, "decide")
    baseline = latest_baseline(db, project.id)
    for refusal, refuse, message in (
        ("not_submitted", change.status != "submitted", NOT_SUBMITTED),
        (
            "newer_baseline",
            baseline is None or baseline.number != change.measured_baseline_number,
            NEWER_BASELINE,
        ),
    ):
        if refuse:
            raise _refuse(
                db,
                user,
                project=project,
                change=change,
                action="decide",
                refusal=refusal,
                code=status.HTTP_409_CONFLICT,
                detail=message,
            )

    before = _snapshot(change)
    made: dict[str, Any] = {}
    try:
        if body.outcome == "approve":
            plan = db.scalar(
                select(ProjectPlan).where(ProjectPlan.project_id == project.id).with_for_update()
            )
            if plan is None:
                raise change_rules.ChangeConflict("this project has no plan to change")
            change_rules.apply(db, plan, baseline.plan, change.changes)  # type: ignore[union-attr]
            plan.updated_at = utcnow()  # the plan's version moves on: something in it changed
            db.flush()
            new = freeze(db, plan, change_request_id=change.id)
            made = {"baseline_id": new.id, "baseline_number": new.number}
        change.status = OUTCOME_STATUS[body.outcome]
        change.decided_by = user.id
        change.decision_reason = body.reason
        change.decided_at = utcnow()
        db.flush()
        _record(
            db,
            user,
            change,
            action=f"change_{OUTCOME_STATUS[body.outcome]}",
            before=before,
            outcome=body.outcome,
            reason=body.reason,
            decider_level=change.decider,
            **made,
        )
    except change_rules.ChangeConflict as conflict:
        db.rollback()
        project, change = _locked(db, project_id, change_id)
        raise _refuse(
            db,
            user,
            project=project,
            change=change,
            action="decide",
            refusal="invalid_plan",
            code=status.HTTP_409_CONFLICT,
            detail=str(conflict),
        ) from None
    except Exception:
        # Nothing was committed: roll back the plan, the baseline and the decision together.
        db.rollback()
        logger.exception("deciding change request %s failed", change_id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, NOT_SAVED) from None
    _commit(db)
    return _one(db, change)
