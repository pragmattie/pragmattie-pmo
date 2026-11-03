"""The approval gate: a person's decision on a submitted project request (item 4.4).

The gate, its criteria and who may decide it come from the gates policy file (`app.policy`).
Until a person applies a gates file that defines the `approval` gate, both routes answer 503.

- `GET /requests/{id}/gate` (`project:read`) shows the gate, each criterion with its result now
  (automatic checks from `app.gates`; person criteria unticked) and the latest decision.
- `POST /requests/{id}/gate` (`gate:decide`) records a decision: `pass` sets the request to
  `approved`, `return` sends it back to `draft` for more work, and `decline` is final. It needs a
  role the gate lists, a request the caller didn't raise themselves (separation of duties), a
  submitted request, the request's `version` (stale is a 409) and a reason of at least 20
  characters. `pass` needs every criterion met: a failing check or an unticked person criterion is
  a 409 naming them. Person ticks and notes come with the decision and are kept in its criteria
  snapshot; `return` and `decline` don't need them.

Each decision is one `gate_decisions` row (the criteria as they stood, and the SHA-256 of the
gates file) and one audit row in the request's history. Each refused attempt is one audit row
(`status: rejected`) naming the reason, and changes nothing: a role without `gate:decide` (the
guard records it), a role the gate doesn't list, the caller's own submission, a stale version, a
request in a final status, a request that isn't submitted (each for any outcome), and a `pass`
with unmet criteria. The route needs a signed-in person, so no agent can decide a gate.

A `pass` names the project manager (`project_manager_id`, an active user with the
`project_manager` role; anyone else is a 422) and turns the request into a project (item 4.6,
`app.projects`): the project, its brief, stakeholders and first risks are created in the same
transaction as the decision, and the decision's audit row records their ids. A request that is
already a project is a refused 409. If anything fails, the whole transaction rolls back: nothing
is created and the decision is not recorded.
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
from app.db import get_db
from app.gates import CHECKS
from app.models import GateDecision, ProjectRequest, User
from app.models.intake import FINAL_STATUSES
from app.policy import Gate, PolicyError, get_policies
from app.projects import create_from_request
from app.roles import Role
from app.schemas.gate import (
    CriterionOut,
    GateDecisionIn,
    GateDecisionOut,
    GateInfo,
    GateOut,
    PersonTick,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/requests", tags=["gates"])

APPROVAL_GATE = "approval"
AUDIT_AGENT = "gate"
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


def _gate() -> tuple[Gate, str]:
    """The approval gate and the gates file's digest, or 503 while it isn't defined."""
    try:
        gates = get_policies().gates
    except PolicyError:
        logger.exception("gates policy failed to load")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, GATES_UNAVAILABLE) from None
    gate = gates.gates.get(APPROVAL_GATE)
    if gate is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, GATES_UNAVAILABLE)
    return gate, gates.digest


def _record_refused(
    db: Session,
    request: ProjectRequest,
    user: User,
    *,
    refusal: str,
    outcome: str | None,
    unmet: list[dict[str, Any]] | None = None,
) -> None:
    """One audit row for a refused gate decision; the request stays as it is."""
    record_refusal(
        db,
        agent=AUDIT_AGENT,
        subject_type=AUDIT_SUBJECT,
        subject_id=str(request.id),
        version=request.version,
        actor_user_id=user.id,
        output=snapshot(request),
        action_taken={
            "action": "gate_refused",
            "gate": APPROVAL_GATE,
            "outcome": outcome,
            "refusal": refusal,
            "actor_role": user.role,
            **({"unmet": unmet} if unmet is not None else {}),
        },
    )


def _refuse_without_permission(db: Session, user: User, path_params: dict[str, str]) -> None:
    """The guard's refusal row for a role without `gate:decide`. An unknown request has nothing
    to name, so it gets the 403 without a row."""
    try:
        request = db.get(ProjectRequest, int(path_params["request_id"]))
    except (KeyError, ValueError):
        return
    if request is not None:
        _record_refused(db, request, user, refusal="no_permission", outcome=None)


def _evaluate(
    db: Session, gate: Gate, request: ProjectRequest, ticks: dict[str, PersonTick]
) -> list[CriterionOut]:
    """Each criterion's result now: automatic checks run; person criteria take the given tick,
    and are unticked (`met` null) when no decision is being made."""
    results = []
    for criterion in gate.criteria:
        if criterion.check is not None:
            result = CHECKS[criterion.check](db, request)
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


def _latest(db: Session, request_id: int) -> GateDecision | None:
    stmt = (
        select(GateDecision)
        .where(
            GateDecision.subject_type == SUBJECT_TYPE,
            GateDecision.subject_id == request_id,
            GateDecision.gate_id == APPROVAL_GATE,
        )
        .order_by(GateDecision.id.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def _gate_out(
    db: Session, gate: Gate, digest: str, request: ProjectRequest, criteria: list[CriterionOut]
) -> GateOut:
    latest = _latest(db, request.id)
    decision = None
    if latest is not None:
        names = _names(db, User, {latest.decided_by}, User.display_name)
        decision = GateDecisionOut(
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
    return GateOut(
        gate=GateInfo(
            id=gate.id,
            name=gate.name,
            from_stage=gate.from_stage,
            to_stage=gate.to_stage,
            applies_to=gate.applies_to,
            deciders=list(gate.deciders),
        ),
        request_id=request.id,
        request_version=request.version,
        request_status=request.status,
        requested_by=request.requested_by,
        project_id=request.project_id,
        criteria=criteria,
        checks_met=all(c.met for c in criteria if c.kind == "check"),
        policy_version=digest,
        latest_decision=decision,
    )


@router.get("/{request_id}/gate", response_model=GateOut)
def get_gate(
    request_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> GateOut:
    gate, digest = _gate()
    request = _request(db, request_id)
    return _gate_out(db, gate, digest, request, _evaluate(db, gate, request, {}))


@router.post("/{request_id}/gate", response_model=GateOut)
def decide_gate(
    request_id: int,
    body: GateDecisionIn,
    user: Annotated[User, Depends(require_audited(Action.GATE_DECIDE, _refuse_without_permission))],
    db: Annotated[Session, Depends(get_db)],
) -> GateOut:
    gate, digest = _gate()
    # Locked until commit on MySQL, so a parallel change waits and then sees the new version.
    request = _request(db, request_id, for_update=True)
    if user.role not in gate.deciders:
        _record_refused(db, request, user, refusal="role_not_a_decider", outcome=body.outcome)
        _commit(db)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"the {gate.name.lower()} is decided only by: {', '.join(gate.deciders)}",
        )
    if request.requested_by == user.id:
        _record_refused(db, request, user, refusal="own_submission", outcome=body.outcome)
        _commit(db)
        not_own_submission(user.id, request.requested_by)
    for refusal, refused, message in (
        ("stale_version", body.version != request.version, STALE),
        ("final_status", request.status in FINAL_STATUSES, FINAL),
        ("not_submitted", request.status != "submitted", NOT_SUBMITTED),
    ):
        if refused:
            _record_refused(db, request, user, refusal=refusal, outcome=body.outcome)
            _commit(db)
            raise HTTPException(status.HTTP_409_CONFLICT, message)

    criteria = _evaluate(db, gate, request, _ticks(gate, body.criteria))
    recorded = [c.model_dump() for c in criteria]
    manager = None
    if body.outcome == "pass":
        manager = _project_manager(db, body.project_manager_id)
        unmet = [c for c in criteria if not c.met]
        if unmet:
            named = [{"id": c.id, "text": c.text, "detail": c.detail} for c in unmet]
            _record_refused(
                db, request, user, refusal="unmet_criteria", outcome="pass", unmet=named
            )
            _commit(db)
            raise HTTPException(status.HTTP_409_CONFLICT, {"message": UNMET, "unmet": named})
        if request.project_id is not None:
            _record_refused(db, request, user, refusal="already_a_project", outcome="pass")
            _commit(db)
            raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_A_PROJECT)

    before = snapshot(request)
    created = None
    if manager is not None:
        try:
            created = create_from_request(db, request, user, manager)
        except Exception:
            # Nothing was committed: roll back the project and everything made with it.
            db.rollback()
            logger.exception("creating the project for request %s failed", request.id)
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, NOT_CREATED) from None
        request.project_id = created.project.id
    request.status = OUTCOME_STATUS[body.outcome]
    _save(db)
    decision = GateDecision(
        gate_id=gate.id,
        subject_type=SUBJECT_TYPE,
        subject_id=request.id,
        outcome=body.outcome,
        decided_by=user.id,
        reason=body.reason,
        criteria=recorded,
        policy_version=digest,
    )
    db.add(decision)
    db.flush()
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=AUDIT_SUBJECT,
        subject_id=str(request.id),
        version=request.version,
        attempt=next_attempt(
            db,
            agent=AUDIT_AGENT,
            subject_type=AUDIT_SUBJECT,
            subject_id=str(request.id),
            version=request.version,
        ),
        status="ok",
        actor_user_id=user.id,
        signals=before,
        output=snapshot(request),
        action_taken={
            "action": f"gate_{body.outcome}",
            "gate": gate.id,
            "decision_id": decision.id,
            "outcome": body.outcome,
            "reason": body.reason,
            "criteria": recorded,
            "policy_version": digest,
            **({"created": created.ids()} if created is not None else {}),
        },
    )
    _commit(db)
    return _gate_out(db, gate, digest, request, criteria)
