"""A client project's proposal: the offer the client sees, built from the plan (item 5.2).

A project with a client has at most one proposal; a project with no client has none (404).

- `GET /projects/{id}/proposal` (`project:read`) is the proposal with the checker's flags
  (`app.proposals.check`), run on every read and every save, and its versions' numbers.
- `POST /projects/{id}/proposal` (`project:write`) creates it from the plan: the price, estimate
  lines, milestones, deliverables and the risks ticked to show are copied by code, and scope in and
  out are prefilled from the plan. A second one is a 409.
- `PATCH /projects/{id}/proposal` (`project:write`) edits the text sections, and
  `POST .../refresh` copies the plan's facts again. Either is an edit: on a submitted or approved
  proposal it sends it back to draft, and the approval then shows as for an earlier version. A
  sent, answered or withdrawn proposal can't change (409).
- `POST .../submit` (`project:write`) sends a draft to the sponsor and stores a frozen copy. It is
  refused (409, with the flags) while the checker has any flag open.
- `POST .../approve` and `.../return` (`proposal:approve`, a reason of at least 20 characters)
  are the sponsor's decision on a submitted proposal. Returning sends it back to draft. Nobody
  decides on a copy they contributed to (item 6.10): they submitted it, or they are in the
  `touched_by` of one of its sections, having written, edited or reviewed it at any point, even
  if someone touched it after them (`app.authz.not_own_submission`, 403, audited as
  `own_submission`).
- `POST .../sent` (`project:write`) records that a person sent the approved copy: to whom, on what
  date, by what means. Nothing is sent from here. Like submit, it is refused (409, with the flags)
  while the checker has any flag open, for example after the plan changed. `POST .../answer`
  (`project:write`) records the client's answer (accepted or declined), the day it came and a note.
  The steps go in order: sent only from `approved`, the answer only from `sent`; anything else is a
  409, audited. Admin has no `project:write`, so admin can do neither.
- `POST .../withdraw` (`project:write`, a reason of at least 20 characters) withdraws a proposal
  from draft, submitted, approved or sent. An accepted or declined one can't be withdrawn, and a
  withdrawn one is already final (409, audited).
- `GET .../versions` (`project:read`) lists the frozen copies, newest first, each with what
  people did with it.
- `POST .../draft` (`project:write`, item 5.3) runs the proposal agent
  (`app.agents.proposal_agent`) on a draft proposal, gated by `AGENT_MODE`: `off` refuses (409,
  audited) and calls nothing; `shadow` returns the draft and saves nothing; `enforce` saves each
  section that is empty or still an unreviewed agent draft as an unreviewed agent draft, and keeps
  every section a person edited or confirmed. With `?dry_run=true` it returns the exact request
  and a cost ceiling and calls nothing. The checker's flags come back with the draft.
- `POST .../sections/{section}/confirm` (`project:write`) is a person accepting an agent's draft
  section as it is, in `draft` only: who and when are recorded. A PATCH that changes a section
  marks it a person's. Submit is refused (409, naming the sections) while any section is still an
  unreviewed agent draft.

Every write carries the proposal's `version`; a stale one is a 409. Each change is one audit row
(agent `proposal`, trigger `human`, the person's id). Every refused attempt, edits included (a
role without the right, the caller's own submission, a stale version, a wrong status, open flags
or a second create), is one audit row too (`status: rejected`), and changes nothing. A refusal
before the project has a proposal is recorded against the project (subject type `project`).
A cancelled project (declined at the go-ahead gate, item 5.4) takes no proposal writes: each one is
a refused 409, recorded. These routes need a signed-in person, so no agent can approve or mark
sent, and every event a route records has its person (`source = person`).

Item 6.8: the submit and answer rules live in `app.proposals` (`submit_refusal`, `submit`,
`answer_refusal`, `answer`), shared with the orchestrator, which may submit a proposal once every
section is reviewed, and the simulated client, which answers a sent proposal to a client whose
contact email is in the demo domain. Their rows say so: no person, `trigger = orchestrator`, and
the answer's event `source = simulated`. `client_simulated` on the proposal says the client is
simulated.
"""

import logging
from decimal import ROUND_UP, Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app import proposals as rules
from app.agents import proposal_agent, risk_agent
from app.api.intake import _commit, _names, _save
from app.api.projects import Db, Reader, _project
from app.audit import record_refusal
from app.authz import Action, not_own_submission, require_audited
from app.config import get_settings
from app.models import (
    Client,
    Project,
    ProjectPlan,
    ProjectProposal,
    ProposalEvent,
    ProposalVersion,
    User,
)
from app.models.base import utcnow
from app.models.portfolio import CANCELLED
from app.models.proposal import TEXT_SECTIONS
from app.proposals import (
    NOT_DRAFT,
    NOT_SENT,
    Refusal,
    check,
    latest_version,
    mark_section,
    plan_facts,
    refresh_from_plan,
    snapshot,
    unreviewed_sections,
    values_of,
)
from app.schemas.common import DEFAULT_LIMIT, Limit, Offset, Page, paginate
from app.schemas.proposal import (
    EventOut,
    ProposalAnswer,
    ProposalDecision,
    ProposalDraftResult,
    ProposalOut,
    ProposalSent,
    ProposalUpdate,
    ProposalWrite,
    Section,
    SectionState,
    VersionOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["proposals"])

AUDIT_AGENT = "proposal"
SUBJECT_TYPE = "proposal"
# A refusal before the project has a proposal names the project instead.
PROJECT_SUBJECT = "project"

NO_CLIENT = "this project has no client, so it has no proposal"
NO_PROPOSAL = "this project has no proposal yet"
NO_PLAN = "this project has no plan to build a proposal from"
EXISTS = "this project already has a proposal"
STALE = "This proposal has changed since you loaded it. Reload and try again."
FINAL = "a proposal that has been sent, answered or withdrawn can't change"
SENT_HAS_FLAGS = "the proposal can't be marked sent while the checker has flags open"
NOT_SUBMITTED = "only a submitted proposal can be approved or returned"
# Why a contributor to the copy may not decide it (item 6.10).
OWN = "you cannot approve or return this proposal: you submitted this copy"
REVIEWED = (
    "you cannot approve or return this proposal: "
    "you wrote, edited or reviewed a section of this copy"
)
NOT_APPROVED = "only an approved proposal can be marked sent"
CANT_WITHDRAW = "this proposal has already been answered or withdrawn"
AGENT_OFF = "the proposal agent is off (AGENT_MODE=off); a person can write the proposal by hand"
NOT_DRAFT_AGENT = "the proposal agent drafts only for a draft proposal"
AGENT_FAILED = "the proposal agent could not finish; the failed run is recorded in the audit trail"
CONFIRM_NOT_DRAFT = "a section can be confirmed only on a draft proposal"
NOT_AGENT_DRAFT = "this section is not an agent draft waiting for review"
CANCELLED_PROJECT = "this project is cancelled, so its proposal can't change"

EDITABLE = ("draft", "submitted", "approved")
WITHDRAWABLE = ("draft", "submitted", "approved", "sent")
APPROVED_ONWARDS = ("approved", "sent", "accepted", "declined")


# --- Loading


def _client_project(db: Session, project_id: int) -> tuple[Project, Client]:
    project = _project(db, project_id)
    client = db.get(Client, project.client_id) if project.client_id else None
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_CLIENT)
    return project, client


def _plan(db: Session, project_id: int) -> ProjectPlan:
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project_id))
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_PLAN)
    return plan


def _find(db: Session, project_id: int, *, for_update: bool = False) -> ProjectProposal | None:
    stmt = select(ProjectProposal).where(ProjectProposal.project_id == project_id)
    return db.scalar(stmt.with_for_update() if for_update else stmt)


def _proposal(db: Session, project_id: int, *, for_update: bool = False) -> ProjectProposal:
    _client_project(db, project_id)
    proposal = _find(db, project_id, for_update=for_update)
    if proposal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_PROPOSAL)
    return proposal


def _latest_approval(db: Session, proposal_id: int) -> ProposalEvent | None:
    return db.scalar(
        select(ProposalEvent)
        .where(ProposalEvent.proposal_id == proposal_id, ProposalEvent.kind == "approve")
        .order_by(ProposalEvent.id.desc())
        .limit(1)
    )


# --- Output


def _section_states(proposal: ProjectProposal) -> dict[str, SectionState]:
    sources = proposal.section_sources or {}
    return {
        name: SectionState(
            **(sources.get(name) or {}),
            unreviewed=name in unreviewed_sections(proposal),
        )
        for name in TEXT_SECTIONS
    }


def _contributor_names(contributors: list[int], names: dict[Any, str]) -> list[str]:
    return [names.get(user_id) or f"User {user_id}" for user_id in contributors]


def _proposal_out(db: Session, proposal: ProjectProposal) -> ProposalOut:
    project, client = _client_project(db, proposal.project_id)
    plan = _plan(db, proposal.project_id)
    facts = plan_facts(db, plan)
    values = values_of(proposal)
    latest = latest_version(db, proposal.id)
    approval = _latest_approval(db, proposal.id)
    approved = db.get(ProposalVersion, approval.version_id) if approval else None
    contributors = list(latest.contributors or []) if latest is not None else []
    people = set(contributors) | ({latest.submitted_by} if latest is not None else set())
    names = _names(db, User, people - {None}, User.display_name)
    submitter = names.get(latest.submitted_by) if latest is not None else None
    return ProposalOut(
        id=proposal.id,
        project_id=project.id,
        project_name=project.name,
        client_name=client.name,
        client_simulated=client.simulated,
        status=proposal.status,
        **values,
        plan_version_seen=proposal.plan_version_seen,
        plan_changed=plan.version != proposal.plan_version_seen,
        flags=[flag.as_dict() for flag in check(values, facts)],
        sections=_section_states(proposal),
        unreviewed_sections=unreviewed_sections(proposal),
        latest_version=latest.number if latest else None,
        submitted_by=latest.submitted_by if latest else None,
        submitted_by_name=submitter,
        contributors=contributors,
        contributor_names=_contributor_names(contributors, names),
        approved_version=approved.number if approved else None,
        approval_current=bool(
            approved and latest and approved.id == latest.id and proposal.status in APPROVED_ONWARDS
        ),
        version=proposal.version,
        updated_at=proposal.updated_at,
    )


# --- Audit


def _record(
    db: Session,
    proposal: ProjectProposal,
    user: User,
    *,
    action: str,
    before: dict[str, Any] | None,
    **extra: Any,
) -> None:
    rules.record(
        db,
        proposal,
        actor_user_id=user.id,
        trigger="human",
        action=action,
        before=before,
        **extra,
    )


def _refuse(
    db: Session,
    proposal: ProjectProposal,
    user: User,
    *,
    action: str,
    refusal: str,
    code: int,
    detail: Any,
    **extra: Any,
) -> HTTPException:
    """Record a refused attempt, commit the row, and give the error to raise."""
    record_refusal(
        db,
        agent=AUDIT_AGENT,
        subject_type=SUBJECT_TYPE,
        subject_id=str(proposal.id),
        version=proposal.version,
        actor_user_id=user.id,
        output=snapshot(proposal),
        action_taken={
            "action": f"{action}_refused",
            "refusal": refusal,
            "actor_role": user.role,
            "project_id": proposal.project_id,
            **extra,
        },
    )
    db.commit()
    return HTTPException(code, detail)


def _refusal_recorder(action: str):
    """The guard's refusal row for a role without the right. Before the project has a proposal
    (a refused create), the row names the project; an unknown project gets the 403 without one."""

    def record(db: Session, user: User, path_params: dict[str, str]) -> None:
        try:
            project_id = int(path_params["project_id"])
        except (KeyError, ValueError):
            return
        proposal = _find(db, project_id)
        if proposal is not None:
            subject = (SUBJECT_TYPE, str(proposal.id), proposal.version, snapshot(proposal))
        elif db.get(Project, project_id) is not None:
            subject = (PROJECT_SUBJECT, str(project_id), 1, None)
        else:
            return
        subject_type, subject_id, version, output = subject
        record_refusal(
            db,
            agent=AUDIT_AGENT,
            subject_type=subject_type,
            subject_id=subject_id,
            version=version,
            actor_user_id=user.id,
            output=output,
            action_taken={
                "action": f"{action}_refused",
                "refusal": "no_permission",
                "actor_role": user.role,
                "project_id": project_id,
            },
        )

    return record


def _refuse_if_cancelled(db: Session, proposal: ProjectProposal, user: User, action: str) -> None:
    """A cancelled project (declined at the go-ahead gate, item 5.4) takes no proposal writes."""
    if db.get(Project, proposal.project_id).status == CANCELLED:
        raise _refuse(
            db,
            proposal,
            user,
            action=action,
            refusal="project_cancelled",
            code=409,
            detail=CANCELLED_PROJECT,
        )


def _person(action: Action, name: str):
    return Annotated[User, Depends(require_audited(action, _refusal_recorder(name)))]


Creator = _person(Action.PROJECT_WRITE, "create")
Editor = _person(Action.PROJECT_WRITE, "update")
Refresher = _person(Action.PROJECT_WRITE, "refresh")
Submitter = _person(Action.PROJECT_WRITE, "submit")
Approver = _person(Action.PROPOSAL_APPROVE, "approve")
Returner = _person(Action.PROPOSAL_APPROVE, "return")
SentRecorder = _person(Action.PROJECT_WRITE, "sent")
AnswerRecorder = _person(Action.PROJECT_WRITE, "answer")
Withdrawer = _person(Action.PROJECT_WRITE, "withdraw")
Drafter = _person(Action.PROJECT_WRITE, "draft")
Confirmer = _person(Action.PROJECT_WRITE, "confirm")


# --- The proposal


@router.get("/{project_id}/proposal", response_model=ProposalOut)
def get_proposal(project_id: int, _user: Reader, db: Db) -> ProposalOut:
    return _proposal_out(db, _proposal(db, project_id))


@router.post(
    "/{project_id}/proposal", response_model=ProposalOut, status_code=status.HTTP_201_CREATED
)
def create_proposal(project_id: int, user: Creator, db: Db) -> ProposalOut:
    project, _ = _client_project(db, project_id)
    if project.status == CANCELLED:
        record_refusal(
            db,
            agent=AUDIT_AGENT,
            subject_type=PROJECT_SUBJECT,
            subject_id=str(project_id),
            version=project.version,
            actor_user_id=user.id,
            output=None,
            action_taken={
                "action": "create_refused",
                "refusal": "project_cancelled",
                "actor_role": user.role,
                "project_id": project_id,
            },
        )
        db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, CANCELLED_PROJECT)
    plan = _plan(db, project_id)
    existing = _find(db, project_id)
    if existing is not None:
        raise _refuse(
            db, existing, user, action="create", refusal="exists", code=409, detail=EXISTS
        )
    proposal = rules.new_proposal(db, plan, user.id)
    _record(db, proposal, user, action="create", before=None)
    _commit(db)
    return _proposal_out(db, proposal)


def _editable(
    db: Session, project_id: int, version: int, user: User, action: str
) -> ProjectProposal:
    """The proposal, locked, if it is at `version` and can still change; a stale version or a
    sent, answered or withdrawn proposal is a refusal, recorded."""
    proposal = _proposal(db, project_id, for_update=True)
    _refuse_if_cancelled(db, proposal, user, action)
    if proposal.version != version:
        raise _refuse(db, proposal, user, action=action, refusal="stale", code=409, detail=STALE)
    if proposal.status not in EDITABLE:
        raise _refuse(
            db,
            proposal,
            user,
            action=action,
            refusal="wrong_status",
            code=409,
            detail=FINAL,
            status=proposal.status,
        )
    return proposal


def _edited(
    db: Session, proposal: ProjectProposal, user: User, action: str, before: dict[str, Any]
) -> None:
    """Save an edit. A submitted or approved proposal goes back to draft."""
    from_status = proposal.status
    if from_status != "draft":
        proposal.status = "draft"
    proposal.updated_at = utcnow()
    _save(db)
    extra = {"from": from_status, "to": "draft"} if from_status != "draft" else {}
    _record(db, proposal, user, action=action, before=before, **extra)
    _commit(db)


@router.patch("/{project_id}/proposal", response_model=ProposalOut)
def update_proposal(project_id: int, body: ProposalUpdate, user: Editor, db: Db) -> ProposalOut:
    proposal = _editable(db, project_id, body.version, user, "update")
    before = snapshot(proposal)
    changes = body.model_dump(exclude={"version"}, exclude_unset=True)
    changed = False
    now = utcnow().isoformat()
    for name in TEXT_SECTIONS:
        if name in changes and getattr(proposal, name) != changes[name]:
            setattr(proposal, name, changes[name])
            # A person's edit makes the section theirs, whoever drafted it.
            mark_section(proposal, name, source="person", reviewed_by=user.id, reviewed_at=now)
            changed = True
    if changed:
        _edited(db, proposal, user, "update", before)
    return _proposal_out(db, proposal)


@router.post("/{project_id}/proposal/refresh", response_model=ProposalOut)
def refresh_proposal(project_id: int, body: ProposalWrite, user: Refresher, db: Db) -> ProposalOut:
    proposal = _editable(db, project_id, body.version, user, "refresh")
    before = snapshot(proposal)
    plan_version_seen = proposal.plan_version_seen
    refresh_from_plan(proposal, plan_facts(db, _plan(db, project_id)))
    if snapshot(proposal) != before or proposal.plan_version_seen != plan_version_seen:
        _edited(db, proposal, user, "refresh", before)
    return _proposal_out(db, proposal)


# --- Changes of status


def _for_decision(
    db: Session, project_id: int, version: int, user: User, action: str, wanted: tuple[str, ...]
) -> ProjectProposal:
    """The proposal, locked, if it is at `version` and in one of the `wanted` statuses; any
    other case is a refusal, recorded."""
    proposal = _proposal(db, project_id, for_update=True)
    _refuse_if_cancelled(db, proposal, user, action)
    if proposal.version != version:
        raise _refuse(db, proposal, user, action=action, refusal="stale", code=409, detail=STALE)
    if proposal.status not in wanted:
        message = {
            "submit": NOT_DRAFT,
            "approve": NOT_SUBMITTED,
            "return": NOT_SUBMITTED,
            "sent": NOT_APPROVED,
            "answer": NOT_SENT,
            "withdraw": CANT_WITHDRAW,
        }[action]
        raise _refuse(
            db,
            proposal,
            user,
            action=action,
            refusal="wrong_status",
            code=409,
            detail=message,
            status=proposal.status,
        )
    return proposal


def _move(
    db: Session,
    proposal: ProjectProposal,
    user: User,
    *,
    action: str,
    to: str,
    event: ProposalEvent | None,
    version: ProposalVersion | None,
) -> ProposalOut:
    def change() -> None:
        rules.move(
            db,
            proposal,
            actor_user_id=user.id,
            trigger="human",
            action=action,
            to=to,
            event=event,
            version=version,
        )

    return _apply(db, proposal, change)


def _apply(db: Session, proposal: ProjectProposal, change) -> ProposalOut:
    """Run a status change from `app.proposals`, commit it, and give the proposal back; a row
    another write changed meanwhile is a 409."""
    try:
        change()
    except StaleDataError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE) from None
    _commit(db)
    return _proposal_out(db, proposal)


def _refuse_with(
    db: Session, proposal: ProjectProposal, user: User, action: str, refusal: Refusal | None
) -> None:
    """A refusal from `app.proposals`, recorded and raised as a 409."""
    if refusal is not None:
        raise _refuse(
            db,
            proposal,
            user,
            action=action,
            refusal=refusal.refusal,
            code=409,
            detail=refusal.detail,
            **refusal.extra,
        )


def _refuse_open_flags(
    db: Session, proposal: ProjectProposal, user: User, action: str, message: str
) -> None:
    """Run the checker; any open flag refuses the step (409, with the flags), recorded."""
    flags = check(values_of(proposal), plan_facts(db, _plan(db, proposal.project_id)))
    if flags:
        found = [flag.as_dict() for flag in flags]
        raise _refuse(
            db,
            proposal,
            user,
            action=action,
            refusal="flags_open",
            code=409,
            detail={"message": message, "flags": found},
            flags=found,
        )


@router.post("/{project_id}/proposal/submit", response_model=ProposalOut)
def submit_proposal(project_id: int, body: ProposalWrite, user: Submitter, db: Db) -> ProposalOut:
    proposal = _for_decision(db, project_id, body.version, user, "submit", ("draft",))
    _plan(db, project_id)
    _refuse_with(db, proposal, user, "submit", rules.submit_refusal(db, proposal))
    return _apply(
        db,
        proposal,
        lambda: rules.submit(db, proposal, actor_user_id=user.id, trigger="human"),
    )


def _decide(
    db: Session, project_id: int, body: ProposalDecision, user: User, action: str
) -> ProposalOut:
    proposal = _for_decision(db, project_id, body.version, user, action, ("submitted",))
    latest = latest_version(db, proposal.id)
    contributors = set(latest.contributors or ()) if latest is not None else set()
    if latest is not None and latest.submitted_by is not None:
        contributors.add(latest.submitted_by)
    if user.id in contributors:
        reason = OWN if latest.submitted_by == user.id else REVIEWED
        _refuse(
            db,
            proposal,
            user,
            action=action,
            refusal="own_submission",
            code=403,
            detail=reason,
            contributors=sorted(contributors),
        )
        not_own_submission(user.id, contributors, reason)
    event = ProposalEvent(
        proposal_id=proposal.id,
        version_id=latest.id if latest else None,
        kind=action,
        actor_user_id=user.id,
        reason=body.reason,
    )
    to = "approved" if action == "approve" else "draft"
    return _move(db, proposal, user, action=action, to=to, event=event, version=latest)


@router.post("/{project_id}/proposal/approve", response_model=ProposalOut)
def approve_proposal(
    project_id: int, body: ProposalDecision, user: Approver, db: Db
) -> ProposalOut:
    return _decide(db, project_id, body, user, "approve")


@router.post("/{project_id}/proposal/return", response_model=ProposalOut)
def return_proposal(project_id: int, body: ProposalDecision, user: Returner, db: Db) -> ProposalOut:
    return _decide(db, project_id, body, user, "return")


@router.post("/{project_id}/proposal/sent", response_model=ProposalOut)
def mark_sent(project_id: int, body: ProposalSent, user: SentRecorder, db: Db) -> ProposalOut:
    proposal = _for_decision(db, project_id, body.version, user, "sent", ("approved",))
    _refuse_open_flags(db, proposal, user, "sent", SENT_HAS_FLAGS)
    latest = latest_version(db, proposal.id)
    event = ProposalEvent(
        proposal_id=proposal.id,
        version_id=latest.id if latest else None,
        kind="sent",
        actor_user_id=user.id,
        sent_to=body.sent_to,
        sent_on=body.sent_on,
        means=body.means,
    )
    return _move(db, proposal, user, action="sent", to="sent", event=event, version=latest)


@router.post("/{project_id}/proposal/answer", response_model=ProposalOut)
def record_answer(
    project_id: int, body: ProposalAnswer, user: AnswerRecorder, db: Db
) -> ProposalOut:
    proposal = _for_decision(db, project_id, body.version, user, "answer", ("sent",))
    _refuse_with(db, proposal, user, "answer", rules.answer_refusal(proposal))
    return _apply(
        db,
        proposal,
        lambda: rules.answer(
            db,
            proposal,
            answer=body.answer,
            answered_on=body.answered_on,
            note=body.note,
            actor_user_id=user.id,
            source="person",
            trigger="human",
        ),
    )


@router.post("/{project_id}/proposal/withdraw", response_model=ProposalOut)
def withdraw_proposal(
    project_id: int, body: ProposalDecision, user: Withdrawer, db: Db
) -> ProposalOut:
    proposal = _for_decision(db, project_id, body.version, user, "withdraw", WITHDRAWABLE)
    latest = latest_version(db, proposal.id)
    event = ProposalEvent(
        proposal_id=proposal.id,
        version_id=latest.id if latest else None,
        kind="withdraw",
        actor_user_id=user.id,
        reason=body.reason,
    )
    return _move(db, proposal, user, action="withdraw", to="withdrawn", event=event, version=latest)


# --- The proposal agent (item 5.3)


def get_claude_client() -> risk_agent.ClaudeClient:
    """The proposal agent's Claude client; tests replace it through `dependency_overrides`."""
    return risk_agent.AnthropicClient(get_settings())


@router.post("/{project_id}/proposal/draft", response_model=ProposalDraftResult)
def draft_proposal(
    project_id: int,
    user: Drafter,
    db: Db,
    claude: Annotated[risk_agent.ClaudeClient, Depends(get_claude_client)],
    dry_run: bool = False,
) -> ProposalDraftResult:
    settings = get_settings()
    proposal = _proposal(db, project_id)
    _refuse_if_cancelled(db, proposal, user, "draft")
    if settings.agent_mode == "off":
        raise _refuse(
            db, proposal, user, action="draft", refusal="agent_off", code=409, detail=AGENT_OFF
        )
    if proposal.status != "draft":
        raise _refuse(
            db,
            proposal,
            user,
            action="draft",
            refusal="wrong_status",
            code=409,
            detail=NOT_DRAFT_AGENT,
            status=proposal.status,
        )
    facts = plan_facts(db, _plan(db, project_id))
    common = {
        "mode": settings.agent_mode,
        "dry_run": dry_run,
        "model": settings.proposal_agent_model,
        "prompt_version": proposal_agent.PROMPT_VERSION,
    }
    if dry_run:
        data = proposal_agent.request_data(db, proposal, facts)
        api_request = proposal_agent.build_request(data, settings)
        ceiling = proposal_agent.cost_ceiling(api_request, settings)
        return ProposalDraftResult(
            **common,
            request=api_request,
            cost_ceiling_usd=ceiling.quantize(Decimal("0.01"), rounding=ROUND_UP),
        )
    save_it = settings.agent_mode == "enforce"
    try:
        result = proposal_agent.run(
            db, claude, proposal, facts, settings=settings, save_it=save_it, actor_user_id=user.id
        )
    except risk_agent.AgentError:
        logger.exception("proposal agent run failed for project %s", project_id)
        _commit(db)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, AGENT_FAILED) from None
    _commit(db)
    checked = result.checked
    # The checker runs on the result: the saved proposal, or in shadow the one it would be.
    values = values_of(proposal)
    values.update({name: checked.sections[name] for name in result.replaced})
    return ProposalDraftResult(
        **common,
        sections=checked.sections,
        removed=checked.removed,
        filled=checked.filled,
        dropped=checked.dropped,
        replaced=result.replaced if save_it else [],
        kept=result.kept,
        flags=[flag.as_dict() for flag in check(values, facts)],
        proposal_version=proposal.version,
        audit_id=result.audit_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )


@router.post("/{project_id}/proposal/sections/{section}/confirm", response_model=ProposalOut)
def confirm_section(
    project_id: int, section: Section, body: ProposalWrite, user: Confirmer, db: Db
) -> ProposalOut:
    """A person accepts an agent's draft section as it is."""
    proposal = _proposal(db, project_id, for_update=True)
    _refuse_if_cancelled(db, proposal, user, "confirm")
    if proposal.version != body.version:
        raise _refuse(db, proposal, user, action="confirm", refusal="stale", code=409, detail=STALE)
    if proposal.status != "draft":
        raise _refuse(
            db,
            proposal,
            user,
            action="confirm",
            refusal="wrong_status",
            code=409,
            detail=CONFIRM_NOT_DRAFT,
            status=proposal.status,
            section=section,
        )
    if section not in unreviewed_sections(proposal):
        raise _refuse(
            db,
            proposal,
            user,
            action="confirm",
            refusal="not_agent_draft",
            code=409,
            detail=NOT_AGENT_DRAFT,
            section=section,
        )
    before = snapshot(proposal)
    mark_section(
        proposal, section, source="agent", reviewed_by=user.id, reviewed_at=utcnow().isoformat()
    )
    proposal.updated_at = utcnow()
    _save(db)
    _record(db, proposal, user, action="confirm", before=before, section=section)
    _commit(db)
    return _proposal_out(db, proposal)


# --- Versions


@router.get("/{project_id}/proposal/versions", response_model=Page[VersionOut])
def list_versions(
    project_id: int, _user: Reader, db: Db, limit: Limit = DEFAULT_LIMIT, offset: Offset = 0
) -> Page[VersionOut]:
    proposal = _proposal(db, project_id)
    stmt = select(ProposalVersion).where(ProposalVersion.proposal_id == proposal.id)
    rows, total = paginate(
        db,
        stmt,
        order_by=(ProposalVersion.number.desc(), ProposalVersion.id.desc()),
        limit=limit,
        offset=offset,
    )
    events: list[ProposalEvent] = []
    if rows:
        events = list(
            db.scalars(
                select(ProposalEvent)
                .where(ProposalEvent.version_id.in_([r.id for r in rows]))
                .order_by(ProposalEvent.id)
            )
        )
    people = (
        {r.submitted_by for r in rows}
        | {user_id for r in rows for user_id in r.contributors or ()}
        | {e.actor_user_id for e in events}
    ) - {None}
    names = _names(db, User, people, User.display_name)
    items = [
        VersionOut(
            id=row.id,
            number=row.number,
            content=row.content,
            submitted_by=row.submitted_by,
            submitted_by_name=names.get(row.submitted_by),
            contributors=list(row.contributors or []),
            contributor_names=_contributor_names(list(row.contributors or []), names),
            submitted_at=row.submitted_at,
            events=[
                EventOut.model_validate(e).model_copy(
                    update={"actor_name": names.get(e.actor_user_id)}
                )
                for e in events
                if e.version_id == row.id
            ],
        )
        for row in rows
    ]
    return Page[VersionOut](items=items, total=total, limit=limit, offset=offset)
