"""Stage gates: a person's decision on a request (the approval gate, item 4.4) or on a project (the
go-ahead gate, item 5.4).

A gate, its criteria and who may decide it come from the gates policy file (`app.policy`). Until a
person applies a gates file that defines the gate, both of its routes answer 503.

- `GET /requests/{id}/gate` and `GET /projects/{id}/gate` (`project:read`) show the gate, each
  criterion with its result now (automatic checks from `app.gates`; person criteria unticked) and
  the latest decision. A project's also shows its latest baseline, once there is one.
- `POST /requests/{id}/gate` and `POST /projects/{id}/gate` (`gate:decide`) record a decision.
  Both share one set of rules: a role the gate lists; not the decider's own subject (a request
  they raised, or a project they manage); the subject's `version` (stale is a 409); a reason of at
  least 20 characters; and for `pass`, every criterion met: a failing check or an unticked person
  criterion is a 409 naming them. Person ticks and notes come with the decision and are kept in
  its criteria snapshot; `return` and `decline` don't need them.

Each decision is one `gate_decisions` row (the criteria as they stood, and the SHA-256 of the
gates file) and one audit row in the subject's history. Each refused attempt is one audit row
(`status: rejected`) naming the reason, and changes nothing: a role without `gate:decide` (the
guard records it), a role the gate doesn't list, the caller's own subject, a stale version, a
subject that can't be decided now (for any outcome), and a `pass` with unmet criteria. The route
needs a signed-in person, so no agent can decide a gate. If anything fails while a decision is
saved, the whole transaction rolls back: nothing changes and the decision is not recorded.

The approval gate: only a submitted request is decided, and a declined or withdrawn one is final.
`pass` sets the request to `approved`, `return` sends it back to `draft` for more work, and
`decline` is final. A `pass` names the project manager (`project_manager_id`, an active user with
the `project_manager` role; anyone else is a 422) and turns the request into a project (item 4.6,
`app.projects`): the project, its brief, stakeholders and first risks are created with the
decision, and the decision's audit row records their ids. A request that is already a project is
a refused 409.

The go-ahead gate: only a project in planning is decided, and a cancelled one takes no further
decision. `pass` moves the project to `delivery` and freezes its plan as the next baseline
(`app.baselines`; baseline 1 the first time), whose id the decision's audit row records. `return`
keeps the project in planning, with the reason shown on its latest decision. `decline` sets the
project's status to `cancelled` and leaves its stage at planning.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.intake import (
    AUDIT_SUBJECT,
    FINAL,
    STALE,
    _commit,
    _names,
    _request,
    _save,
    snapshot,
)
from app.audit import next_attempt, record_decision, record_refusal
from app.authz import Action, not_own_submission, require, require_audited
from app.baselines import freeze, latest_baseline
from app.db import get_db
from app.gates import CHECKS_BY_SUBJECT
from app.models import GateDecision, Project, ProjectPlan, ProjectRequest, User
from app.models.intake import FINAL_STATUSES
from app.models.portfolio import CANCELLED
from app.policy import Gate, PolicyError, get_policies
from app.projects import STAGE as PLANNING
from app.projects import create_from_request
from app.roles import Role
from app.schemas.gate import (
    BaselineOut,
    CriterionOut,
    GateDecisionIn,
    GateDecisionOut,
    GateInfo,
    GateOut,
    PersonTick,
    ProjectGateDecisionIn,
    ProjectGateOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/requests", tags=["gates"])
project_router = APIRouter(prefix="/projects", tags=["gates"])

AUDIT_AGENT = "gate"

APPROVAL_GATE = "approval"
SUBJECT_TYPE = "request"
OUTCOME_STATUS = {"pass": "approved", "return": "draft", "decline": "declined"}

GATES_UNAVAILABLE = (
    "the approval gate isn't available yet: a person needs to apply the gates policy file"
)
NOT_SUBMITTED = "only a submitted request can be decided at the approval gate"
UNMET = "this request can't pass the approval gate until every criterion is met"
BAD_TICK = "criteria may answer only this gate's person criteria"
BAD_PROJECT_MANAGER = "project_manager_id must be an active user with the project_manager role"
ALREADY_A_PROJECT = "this request is already a project"
NOT_CREATED = "the project could not be created, so nothing was saved and no decision recorded"

GO_AHEAD_GATE = "go_ahead"
PROJECT_SUBJECT = "project"
GO_AHEAD_UNAVAILABLE = (
    "the go-ahead gate isn't available yet: a person needs to apply the gates policy file"
)
PROJECT_NOT_FOUND = "project not found"
PROJECT_STALE = "this project has changed since you read it; reload it and try again"
PROJECT_CANCELLED = "this project is cancelled and takes no further decisions or changes"
NOT_IN_PLANNING = "the go-ahead gate is decided once, while the project is in planning"
OWN_PROJECT = "you cannot decide the gate of a project you manage"
PROJECT_UNMET = "this project can't pass the go-ahead gate until every criterion is met"
NOT_SAVED = "the decision could not be saved, so nothing changed and no decision was recorded"


# --- What each kind of subject brings to the shared rules


class RequestSubject:
    """A project request at the approval gate."""

    kind = SUBJECT_TYPE
    gate_id = APPROVAL_GATE
    audit_subject = AUDIT_SUBJECT
    unavailable = GATES_UNAVAILABLE
    unmet = UNMET
    failed = NOT_CREATED

    def __init__(self, row: ProjectRequest) -> None:
        self.row = row
        self.manager: User | None = None
        self.created = None

    @staticmethod
    def load(db: Session, subject_id: int, *, for_update: bool = False) -> "RequestSubject":
        return RequestSubject(_request(db, subject_id, for_update=for_update))

    def snapshot(self) -> dict[str, Any]:
        return snapshot(self.row)

    def refuse_own(self, user: User) -> str | None:
        """The refusal's name when `user` may not decide this subject as their own."""
        return "own_submission" if self.row.requested_by == user.id else None

    def own_error(self, user: User) -> None:
        not_own_submission(user.id, self.row.requested_by)

    def refusals(self, version: int) -> list[tuple[str, bool, str]]:
        return [
            ("stale_version", version != self.row.version, STALE),
            ("final_status", self.row.status in FINAL_STATUSES, FINAL),
            ("not_submitted", self.row.status != "submitted", NOT_SUBMITTED),
        ]

    def before_pass(self, db: Session, body: Any) -> None:
        """Inputs a pass needs, checked before its criteria (a 422 is not a refusal)."""
        self.manager = _project_manager(db, body.project_manager_id)

    def pass_refusals(self) -> list[tuple[str, bool, str]]:
        return [("already_a_project", self.row.project_id is not None, ALREADY_A_PROJECT)]

    def apply(self, db: Session, user: User, outcome: str) -> None:
        if outcome == "pass":
            self.created = create_from_request(db, self.row, user, self.manager)
            self.row.project_id = self.created.project.id
        self.row.status = OUTCOME_STATUS[outcome]

    def after_decision(self, db: Session, decision: GateDecision) -> dict[str, Any]:
        return {"created": self.created.ids()} if self.created is not None else {}


class ProjectSubject:
    """A project at the go-ahead gate."""

    kind = PROJECT_SUBJECT
    gate_id = GO_AHEAD_GATE
    audit_subject = PROJECT_SUBJECT
    unavailable = GO_AHEAD_UNAVAILABLE
    unmet = PROJECT_UNMET
    failed = NOT_SAVED

    def __init__(self, row: Project) -> None:
        self.row = row

    @staticmethod
    def load(db: Session, subject_id: int, *, for_update: bool = False) -> "ProjectSubject":
        stmt = select(Project).where(Project.id == subject_id)
        project = db.scalar(stmt.with_for_update() if for_update else stmt)
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, PROJECT_NOT_FOUND)
        return ProjectSubject(project)

    def snapshot(self) -> dict[str, Any]:
        fields = ("name", "stage", "status", "sponsor_user_id", "pm_user_id", "version")
        return {name: getattr(self.row, name) for name in fields}

    def refuse_own(self, user: User) -> str | None:
        return "own_project" if self.row.pm_user_id == user.id else None

    def own_error(self, user: User) -> None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, OWN_PROJECT)

    def refusals(self, version: int) -> list[tuple[str, bool, str]]:
        return [
            ("stale_version", version != self.row.version, PROJECT_STALE),
            ("cancelled", self.row.status == CANCELLED, PROJECT_CANCELLED),
            ("not_in_planning", self.row.stage != PLANNING, NOT_IN_PLANNING),
        ]

    def before_pass(self, db: Session, body: Any) -> None:
        return None

    def pass_refusals(self) -> list[tuple[str, bool, str]]:
        return []

    def apply(self, db: Session, user: User, outcome: str) -> None:
        if outcome == "pass":
            self.row.stage = "delivery"
        elif outcome == "decline":
            self.row.status = CANCELLED

    def after_decision(self, db: Session, decision: GateDecision) -> dict[str, Any]:
        """A pass freezes the plan as the next baseline, made by this decision."""
        if decision.outcome != "pass":
            return {}
        plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == self.row.id))
        if plan is None:  # plan_complete is unmet without one, so a pass never gets here
            raise RuntimeError("a project passed the go-ahead gate without a plan")
        baseline = freeze(db, plan, decision.id)
        return {"baseline_id": baseline.id, "baseline_number": baseline.number}


Subject = RequestSubject | ProjectSubject


# --- The shared rules


def _gate(subject: type[Subject]) -> tuple[Gate, str]:
    """The subject's gate and the gates file's digest, or 503 while it isn't defined."""
    try:
        gates = get_policies().gates
    except PolicyError:
        logger.exception("gates policy failed to load")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, subject.unavailable) from None
    gate = gates.gates.get(subject.gate_id)
    if gate is None or gate.applies_to != subject.kind:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, subject.unavailable)
    return gate, gates.digest


def _locked(subject: type[Subject], db: Session, subject_id: int) -> Subject:
    """The subject, locked until commit on MySQL so a parallel decision waits and then sees the
    new version. The guard's read-only transaction is ended first: on MySQL a plain read keeps
    the snapshot its transaction began with, so without this the loser of a race would miss the
    winner's audit row once the lock is released and pick the same `attempt` (a 500)."""
    db.rollback()
    return subject.load(db, subject_id, for_update=True)


def _record_refused(
    db: Session,
    subject: Subject,
    user: User,
    *,
    refusal: str,
    outcome: str | None,
    unmet: list[dict[str, Any]] | None = None,
) -> None:
    """One audit row for a refused gate decision; the subject stays as it is."""
    record_refusal(
        db,
        agent=AUDIT_AGENT,
        subject_type=subject.audit_subject,
        subject_id=str(subject.row.id),
        version=subject.row.version,
        actor_user_id=user.id,
        output=subject.snapshot(),
        action_taken={
            "action": "gate_refused",
            "gate": subject.gate_id,
            "outcome": outcome,
            "refusal": refusal,
            "actor_role": user.role,
            **({"unmet": unmet} if unmet is not None else {}),
        },
    )


def _refusal_recorder(subject: type[Subject], param: str):
    """The guard's refusal row for a role without `gate:decide`. An unknown subject has nothing
    to name, so it gets the 403 without a row."""

    def record(db: Session, user: User, path_params: dict[str, str]) -> None:
        try:
            found = subject.load(db, int(path_params[param]))
        except (KeyError, ValueError, HTTPException):
            return
        _record_refused(db, found, user, refusal="no_permission", outcome=None)

    return record


def _evaluate(
    db: Session, gate: Gate, subject: Subject, ticks: dict[str, PersonTick]
) -> list[CriterionOut]:
    """Each criterion's result now: automatic checks run; person criteria take the given tick,
    and are unticked (`met` null) when no decision is being made."""
    checks = CHECKS_BY_SUBJECT[subject.kind]
    results = []
    for criterion in gate.criteria:
        if criterion.check is not None:
            result = checks[criterion.check](db, subject.row)
            results.append(
                CriterionOut(
                    id=criterion.id,
                    text=criterion.text,
                    kind="check",
                    check=criterion.check,
                    met=result.met,
                    detail=result.detail,
                )
            )
        else:
            tick = ticks.get(criterion.id)
            results.append(
                CriterionOut(
                    id=criterion.id,
                    text=criterion.text,
                    kind="person",
                    check=None,
                    met=tick.met if tick is not None else None,
                    note=tick.note if tick is not None else None,
                )
            )
    return results


def _ticks(gate: Gate, answers: list[PersonTick]) -> dict[str, PersonTick]:
    """The person ticks by criterion id; one naming anything else is a 422. A person criterion
    left out is unticked."""
    person = {c.id for c in gate.criteria if c.person}
    unknown = sorted({a.id for a in answers} - person)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{BAD_TICK}; not one: {', '.join(unknown)}"
        )
    ticks = {a.id: a for a in answers}
    for criterion_id in person - set(ticks):
        ticks[criterion_id] = PersonTick(id=criterion_id, met=False)
    return ticks


def _project_manager(db: Session, user_id: int | None) -> User:
    manager = db.get(User, user_id) if user_id is not None else None
    if manager is None or not manager.is_active or manager.role != Role.PROJECT_MANAGER:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, BAD_PROJECT_MANAGER)
    return manager


def _latest(db: Session, subject: Subject) -> GateDecisionOut | None:
    stmt = (
        select(GateDecision)
        .where(
            GateDecision.subject_type == subject.kind,
            GateDecision.subject_id == subject.row.id,
            GateDecision.gate_id == subject.gate_id,
        )
        .order_by(GateDecision.id.desc())
        .limit(1)
    )
    latest = db.scalar(stmt)
    if latest is None:
        return None
    names = _names(db, User, {latest.decided_by}, User.display_name)
    return GateDecisionOut(
        id=latest.id,
        gate_id=latest.gate_id,
        outcome=latest.outcome,
        decided_by=latest.decided_by,
        decided_by_name=names.get(latest.decided_by),
        reason=latest.reason,
        criteria=[CriterionOut.model_validate(c) for c in latest.criteria],
        policy_version=latest.policy_version,
        created_at=latest.created_at,
    )


def _common_out(
    db: Session, gate: Gate, digest: str, subject: Subject, criteria: list[CriterionOut]
) -> dict[str, Any]:
    return {
        "gate": GateInfo(
            id=gate.id,
            name=gate.name,
            from_stage=gate.from_stage,
            to_stage=gate.to_stage,
            applies_to=gate.applies_to,
            deciders=list(gate.deciders),
        ),
        "criteria": criteria,
        "checks_met": all(c.met for c in criteria if c.kind == "check"),
        "policy_version": digest,
        "latest_decision": _latest(db, subject),
    }


def _refuse(
    db: Session,
    subject: Subject,
    user: User,
    *,
    refusal: str,
    outcome: str | None,
    code: int,
    detail: Any,
    unmet: list[dict[str, Any]] | None = None,
) -> HTTPException:
    """Record and commit a refused attempt, and give the error to raise."""
    _record_refused(db, subject, user, refusal=refusal, outcome=outcome, unmet=unmet)
    _commit(db)
    return HTTPException(code, detail)


def _decide(
    db: Session,
    subject: Subject,
    gate: Gate,
    digest: str,
    body: ProjectGateDecisionIn,
    user: User,
) -> list[CriterionOut]:
    """Apply the shared rules to a decision on a locked subject, and record it; returns the
    criteria as they stood."""
    if user.role not in gate.deciders:
        raise _refuse(
            db,
            subject,
            user,
            refusal="role_not_a_decider",
            outcome=body.outcome,
            code=status.HTTP_403_FORBIDDEN,
            detail=f"the {gate.name.lower()} is decided only by: {', '.join(gate.deciders)}",
        )
    own = subject.refuse_own(user)
    if own is not None:
        _record_refused(db, subject, user, refusal=own, outcome=body.outcome)
        _commit(db)
        subject.own_error(user)
    for refusal, refused, message in subject.refusals(body.version):
        if refused:
            raise _refuse(
                db,
                subject,
                user,
                refusal=refusal,
                outcome=body.outcome,
                code=status.HTTP_409_CONFLICT,
                detail=message,
            )

    criteria = _evaluate(db, gate, subject, _ticks(gate, body.criteria))
    recorded = [c.model_dump() for c in criteria]
    if body.outcome == "pass":
        subject.before_pass(db, body)
        unmet = [c for c in criteria if not c.met]
        if unmet:
            named = [{"id": c.id, "text": c.text, "detail": c.detail} for c in unmet]
            raise _refuse(
                db,
                subject,
                user,
                refusal="unmet_criteria",
                outcome="pass",
                code=status.HTTP_409_CONFLICT,
                detail={"message": subject.unmet, "unmet": named},
                unmet=named,
            )
        for refusal, refused, message in subject.pass_refusals():
            if refused:
                raise _refuse(
                    db,
                    subject,
                    user,
                    refusal=refusal,
                    outcome="pass",
                    code=status.HTTP_409_CONFLICT,
                    detail=message,
                )

    before = subject.snapshot()
    subject_id = subject.row.id
    try:
        subject.apply(db, user, body.outcome)
        _save(db)
        decision = GateDecision(
            gate_id=gate.id,
            subject_type=subject.kind,
            subject_id=subject_id,
            outcome=body.outcome,
            decided_by=user.id,
            reason=body.reason,
            criteria=recorded,
            policy_version=digest,
        )
        db.add(decision)
        db.flush()
        made = subject.after_decision(db, decision)
        version = subject.row.version
        record_decision(
            db,
            agent=AUDIT_AGENT,
            trigger="human",
            subject_type=subject.audit_subject,
            subject_id=str(subject_id),
            version=version,
            attempt=next_attempt(
                db,
                agent=AUDIT_AGENT,
                subject_type=subject.audit_subject,
                subject_id=str(subject_id),
                version=version,
            ),
            status="ok",
            actor_user_id=user.id,
            signals=before,
            output=subject.snapshot(),
            action_taken={
                "action": f"gate_{body.outcome}",
                "gate": gate.id,
                "decision_id": decision.id,
                "outcome": body.outcome,
                "reason": body.reason,
                "criteria": recorded,
                "policy_version": digest,
                **made,
            },
        )
    except HTTPException:
        raise
    except Exception:
        # Nothing was committed: roll back the decision and everything made with it.
        db.rollback()
        logger.exception("saving a %s decision on %s %s failed", gate.id, subject.kind, subject_id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, subject.failed) from None
    _commit(db)
    return criteria


# --- The approval gate: a request


def _request_out(
    db: Session, gate: Gate, digest: str, subject: RequestSubject, criteria: list[CriterionOut]
) -> GateOut:
    request = subject.row
    return GateOut(
        **_common_out(db, gate, digest, subject, criteria),
        request_id=request.id,
        request_version=request.version,
        request_status=request.status,
        requested_by=request.requested_by,
        project_id=request.project_id,
    )


@router.get("/{request_id}/gate", response_model=GateOut)
def get_gate(
    request_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> GateOut:
    gate, digest = _gate(RequestSubject)
    subject = RequestSubject.load(db, request_id)
    return _request_out(db, gate, digest, subject, _evaluate(db, gate, subject, {}))


@router.post("/{request_id}/gate", response_model=GateOut)
def decide_gate(
    request_id: int,
    body: GateDecisionIn,
    user: Annotated[
        User,
        Depends(
            require_audited(Action.GATE_DECIDE, _refusal_recorder(RequestSubject, "request_id"))
        ),
    ],
    db: Annotated[Session, Depends(get_db)],
) -> GateOut:
    gate, digest = _gate(RequestSubject)
    subject = _locked(RequestSubject, db, request_id)
    criteria = _decide(db, subject, gate, digest, body, user)
    return _request_out(db, gate, digest, subject, criteria)


# --- The go-ahead gate: a project


def _project_out(
    db: Session, gate: Gate, digest: str, subject: ProjectSubject, criteria: list[CriterionOut]
) -> ProjectGateOut:
    project = subject.row
    baseline = latest_baseline(db, project.id)
    return ProjectGateOut(
        **_common_out(db, gate, digest, subject, criteria),
        project_id=project.id,
        project_version=project.version,
        project_stage=project.stage,
        project_status=project.status,
        pm_user_id=project.pm_user_id,
        baseline=BaselineOut.model_validate(baseline) if baseline is not None else None,
    )


@project_router.get("/{project_id}/gate", response_model=ProjectGateOut)
def get_project_gate(
    project_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectGateOut:
    gate, digest = _gate(ProjectSubject)
    subject = ProjectSubject.load(db, project_id)
    return _project_out(db, gate, digest, subject, _evaluate(db, gate, subject, {}))


@project_router.post("/{project_id}/gate", response_model=ProjectGateOut)
def decide_project_gate(
    project_id: int,
    body: ProjectGateDecisionIn,
    user: Annotated[
        User,
        Depends(
            require_audited(Action.GATE_DECIDE, _refusal_recorder(ProjectSubject, "project_id"))
        ),
    ],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectGateOut:
    gate, digest = _gate(ProjectSubject)
    subject = _locked(ProjectSubject, db, project_id)
    criteria = _decide(db, subject, gate, digest, body, user)
    return _project_out(db, gate, digest, subject, criteria)
