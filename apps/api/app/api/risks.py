"""A project's risk register: list, create, read and change risks.

Reading needs `project:read`; creating and changing need `project:write`. There is no DELETE: a
risk is closed with a PATCH that sets `status` to `closed`. A PATCH moves status only from open to
closed, from closed back to open, or from accepted to closed; it never opens a proposed, accepted
or rejected risk. On an accepted risk it may not change `response` or `needs_judgement` or clear
the score (409); probability and impact may still change, and a higher band reopens it. A
rejected risk is final: any PATCH to it is a 409.

Escalation hold: while a risk is escalated (`escalated_to` set), a caller whose role does not rank
at or above the escalation owner (`authz.may_accept_band` for `escalated_band`) may not close it
or re-score it into a lower band (losing the score counts as lower). The whole PATCH is refused
with a 403 before anything changes, and the refusal is one audit row (`status: rejected`) naming
the caller, band, owner and what was attempted. Raising the score and other edits are allowed.
The hold ends when the escalation does: the owner accepts the risk.

The decisions have their own routes, each one audit row with the person's id:

- `POST .../{id}/accept` (`risk:accept`) accepts an open, scored risk. The caller's role must rank
  at or above the band's escalation owner in the risk policy (`authz.may_accept_band`); a refusal
  is a 403 that is audited too (`status: rejected`). It needs a reason of at least 20 characters
  and a review date, and sets the response to `accept` and the status to `accepted`.
- `POST .../{id}/open` and `.../{id}/reject` (`project:write`) decide on a proposed risk;
  rejecting needs a reason.

These routes need a signed-in person: no agent can accept, reject or open a risk. Escalations are
kept by `app.risks`; `GET /api/v1/risks/escalations` lists the open ones for the caller's role.

`POST .../risks/suggest` (`project:write`) runs the risk agent (`app.agents.risk_agent`), gated by
`AGENT_MODE`: `off` refuses (409) and calls nothing; `shadow` returns its proposals and saves
nothing; `enforce` saves them as `proposed`, unscored risks with `source = agent`. With
`?dry_run=true` it returns the exact request and a cost ceiling and calls nothing (any mode but
`off`). A failed run is audited and answered with a 502; it saves no risk.

`POST .../risks/{id}/suggest-score` (`project:write`) asks the risk agent to score one proposed,
unscored risk (`risk_agent.score`), with the same `AGENT_MODE` rules and dry run. `enforce` keeps
the score as the risk's suggestion (`suggested_*`), never its real score; `shadow` returns it and
saves nothing. A risk marked `needs_judgement`, one a person has scored or one that is not
proposed is refused (409) before any call. The orchestrator's `score_risk` step calls the same
code. A person uses a suggestion through the PATCH (their own score) and the open route.

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
from decimal import ROUND_UP, Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.agents import risk_agent
from app.api.risk_library import library
from app.audit import ORDER_BY, decisions_query, record_refusal
from app.authz import Action, may_accept_band, require
from app.config import get_settings
from app.db import get_db
from app.models import AuditDecision, Project, Risk, User
from app.policy import SCALE_LEVELS, PolicyError, RiskPolicy, score_risk
from app.risks import (
    AUDIT_AGENT,
    AUDIT_SUBJECT,
    apply_score,
    band_owner,
    band_rank,
    decide_proposed,
    record_escalation,
    record_risk_change,
    rescore_decisions,
    risk_from_template,
    risk_policy,
    settle_escalation,
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
    RiskAccept,
    RiskCreate,
    RiskDecision,
    RiskEscalationOut,
    RiskFields,
    RiskHeatmap,
    RiskHistoryEntry,
    RiskOut,
    RiskProposalOut,
    RiskReject,
    RiskScoreSuggestion,
    RiskSuggestion,
    RiskUpdate,
    SuggestedScore,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/risks", tags=["risks"])
escalations_router = APIRouter(prefix="/risks", tags=["risks"])

POLICY_UNAVAILABLE = "the risk policy could not be loaded, so risks cannot be saved; try later"
STALE = "this risk has changed since you read it; reload it and try again"
BAD_OWNER = "owner_user_id must be an existing, active user"

NOT_OPEN = "only an open risk can be accepted"
NOT_PROPOSED = "only a proposed risk can be opened or rejected"
UNSCORED = "this risk has no score yet; a person must score it before it can be accepted"
BAD_MOVE = "a change here may only close an open or accepted risk, or reopen a closed one"
REJECTED_FINAL = "a rejected risk can't be edited"
ACCEPTED_LOCKED = (
    "an accepted risk keeps its response, needs_judgement and score; "
    "you may re-score it or close it"
)

# The status moves a PATCH may make; anything else goes through the decision routes.
PATCH_MOVES = frozenset({("open", "closed"), ("closed", "open"), ("accepted", "closed")})

AGENT_OFF = "the risk agent is off (AGENT_MODE=off); a person can add risks by hand"
AGENT_FAILED = "the risk agent could not finish; the failed run is recorded in the audit trail"

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


def _check_version(version: int, risk: Risk) -> None:
    if version != risk.version:
        raise HTTPException(status.HTTP_409_CONFLICT, STALE)


def _flush(db: Session) -> None:
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


@router.get("", response_model=Page[RiskOut])
def list_risks(
    project_id: int,
    _user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
    status_: Annotated[ListStatus | None, Query(alias="status")] = None,
    band: Annotated[str | None, Query(max_length=32)] = None,
    category: Category | None = None,
    kind: Kind | None = None,
    probability: Annotated[int | None, Query(ge=1, le=5)] = None,
    impact: Annotated[int | None, Query(ge=1, le=5)] = None,
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
    # One heat map cell; the screen sets both.
    if probability is not None:
        stmt = stmt.where(Risk.probability == probability)
    if impact is not None:
        stmt = stmt.where(Risk.impact == impact)
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
    escalated = settle_escalation(risk, policy, before=None)
    db.add(risk)
    db.flush()
    record_risk_change(db, risk, action="create", before=None, actor_user_id=user.id)
    if escalated:
        record_escalation(db, risk, actor_user_id=user.id)
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


def get_claude_client() -> risk_agent.ClaudeClient:
    """The risk agent's Claude client; tests replace it through `dependency_overrides`."""
    return risk_agent.AnthropicClient(get_settings())


@router.post("/suggest", response_model=RiskSuggestion)
def suggest_risks(
    project_id: int,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
    claude: Annotated[risk_agent.ClaudeClient, Depends(get_claude_client)],
    dry_run: bool = False,
) -> RiskSuggestion:
    settings = get_settings()
    if settings.agent_mode == "off":
        raise HTTPException(status.HTTP_409_CONFLICT, AGENT_OFF)
    policy = _policy()
    templates = library()
    project = _project(db, project_id)
    common = {
        "mode": settings.agent_mode,
        "dry_run": dry_run,
        "model": settings.risk_agent_model,
        "prompt_version": risk_agent.PROMPT_VERSION,
    }
    if dry_run:
        data = risk_agent.project_data(db, project, templates, policy)
        request = risk_agent.build_request(data, settings)
        ceiling = risk_agent.cost_ceiling(request, settings)
        return RiskSuggestion(
            **common,
            request=request,
            cost_ceiling_usd=ceiling.quantize(Decimal("0.01"), rounding=ROUND_UP),
        )
    try:
        result = risk_agent.run(
            db,
            claude,
            project,
            library=templates,
            policy=policy,
            settings=settings,
            save=settings.agent_mode == "enforce",
            actor_user_id=user.id,
        )
    except risk_agent.AgentError:
        logger.exception("risk agent run failed for project %s", project_id)
        _commit(db)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, AGENT_FAILED) from None
    _commit(db)
    checked = result.checked
    return RiskSuggestion(
        **common,
        proposals=[RiskProposalOut.model_validate(p) for p in checked.proposals],
        dropped=checked.dropped,
        adjusted=checked.adjusted,
        saved=[RiskOut.model_validate(risk) for risk in result.saved],
        audit_id=result.audit_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )


@router.post("/{risk_id}/suggest-score", response_model=RiskScoreSuggestion)
def suggest_score(
    project_id: int,
    risk_id: int,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
    claude: Annotated[risk_agent.ClaudeClient, Depends(get_claude_client)],
    dry_run: bool = False,
) -> RiskScoreSuggestion:
    settings = get_settings()
    if settings.agent_mode == "off":
        raise HTTPException(status.HTTP_409_CONFLICT, AGENT_OFF)
    policy = _policy()
    # Not locked: the Claude call can take a while. A change to the risk meanwhile bumps its
    # version, so saving the suggestion then fails (audited) rather than overwriting it.
    risk = _risk(db, project_id, risk_id)
    refusal = risk_agent.score_refusal(risk)
    if refusal is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, refusal)
    common = {
        "mode": settings.agent_mode,
        "dry_run": dry_run,
        "model": settings.risk_agent_model,
        "prompt_version": risk_agent.SCORE_PROMPT_VERSION,
    }
    if dry_run:
        request = risk_agent.build_score_request(risk_agent.risk_data(db, risk, policy), settings)
        ceiling = risk_agent.cost_ceiling(request, settings)
        return RiskScoreSuggestion(
            **common,
            request=request,
            cost_ceiling_usd=ceiling.quantize(Decimal("0.01"), rounding=ROUND_UP),
        )
    try:
        result = risk_agent.score(
            db,
            claude,
            risk,
            policy=policy,
            settings=settings,
            save=settings.agent_mode == "enforce",
            actor_user_id=user.id,
        )
    except risk_agent.AgentError:
        logger.exception("risk agent scoring failed for risk %s", risk_id)
        _commit(db)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, AGENT_FAILED) from None
    _commit(db)
    return RiskScoreSuggestion(
        **common,
        suggestion=SuggestedScore.model_validate(result.score),
        adjusted=result.adjusted,
        saved=result.saved,
        risk=RiskOut.model_validate(risk),
        audit_id=result.audit_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )


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
        bands=[
            BandTotal(band=b.name, count=band_counts[b.name], owner_role=b.escalation_owner)
            for b in policy.priority_bands
        ],
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


def _record_refusal(
    db: Session,
    risk: Risk,
    user: User,
    *,
    action: str,
    band: str | None,
    owner_role: str | None,
    attempted: dict | None = None,
) -> None:
    """The audit row for a refused decision (`status: rejected`): who tried what, on which band
    and owner. Nothing about the risk changes, so `output` is the risk as it stays."""
    record_refusal(
        db,
        agent=AUDIT_AGENT,
        subject_type=AUDIT_SUBJECT,
        subject_id=str(risk.id),
        version=risk.version,
        actor_user_id=user.id,
        output=snapshot(risk),
        action_taken={
            "action": action,
            "band": band,
            "owner_role": owner_role,
            "actor_role": user.role,
            **({"attempted": attempted} if attempted is not None else {}),
        },
    )


def _escalation_hold(
    policy: RiskPolicy, risk: Risk, user: User, new_status: str, new_band: str | None
) -> str | None:
    """Whether the escalation hold refuses this change: `close` or `rescore`, or none.

    While a risk is escalated, only a role at or above the escalation owner may close it or move
    it to a lower band; losing the score counts as lower. Anyone may raise it or edit its text."""
    if risk.escalated_to is None or may_accept_band(user.role, risk.escalated_band, policy):
        return None
    if new_status == "closed" and risk.status != "closed":
        return "close"
    if band_rank(policy, new_band) < band_rank(policy, risk.band):
        return "rescore"
    return None


def _breaks_acceptance(current: dict, changes: dict) -> bool:
    """Whether a PATCH would undo what accepting a risk settled: its response, needs_judgement or
    score. Changing probability or impact is still allowed, and may reopen it."""
    merged = {**current, **changes}
    return (
        merged["response"] != current["response"]
        or merged["needs_judgement"] != current["needs_judgement"]
        or merged["probability"] is None
        or merged["impact"] is None
    )


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
    _check_version(body.version, risk)
    if risk.status == "rejected":
        raise HTTPException(status.HTTP_409_CONFLICT, REJECTED_FINAL)

    attempted = body.model_dump(mode="json", exclude_unset=True, exclude={"version"})
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    new_status: EditableStatus | None = changes.pop("status", risk.status)
    if new_status is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "status must be open or closed")
    if new_status != risk.status and (risk.status, new_status) not in PATCH_MOVES:
        raise HTTPException(status.HTTP_409_CONFLICT, BAD_MOVE)
    current = {name: getattr(risk, name) for name in RiskFields.model_fields}
    if risk.status == "accepted" and _breaks_acceptance(current, changes):
        raise HTTPException(status.HTTP_409_CONFLICT, ACCEPTED_LOCKED)
    try:
        fields = RiskFields.model_validate({**current, **changes})
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_context=False)) from None
    if "owner_user_id" in changes:
        _check_owner(db, fields.owner_user_id)

    # Decided before anything is set on the risk, so the commit that keeps the refusal's audit
    # row can't carry part of the edit with it.
    _, new_band = score_risk(policy, fields.probability, fields.impact)
    held = _escalation_hold(policy, risk, user, new_status, new_band)
    if held is not None:
        _record_refusal(
            db,
            risk,
            user,
            action=held,
            band=risk.escalated_band,
            owner_role=risk.escalated_to,
            attempted=attempted,
        )
        _commit(db)
        owner = (risk.escalated_to or "owner").replace("_", " ")
        move = "closed" if held == "close" else "moved to a lower band"
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"this risk is escalated to the {owner}: it can't be {move} until the {owner} "
            "or above accepts it",
        )

    before = snapshot(risk)
    for name, value in {**fields.model_dump(), "status": new_status}.items():
        if getattr(risk, name) != value:
            setattr(risk, name, value)
    apply_score(risk, policy)
    escalated = rescore_decisions(risk, policy, before)
    if snapshot(risk) == before:
        return RiskOut.model_validate(risk)

    _flush(db)
    closing = before["status"] != "closed" and risk.status == "closed"
    reopened = before["status"] == "accepted" and risk.status == "open"
    record_risk_change(
        db,
        risk,
        action="close" if closing else "update",
        before=before,
        actor_user_id=user.id,
        details={"reopened": True} if reopened else None,
    )
    if escalated:
        record_escalation(db, risk, actor_user_id=user.id)
    _commit(db)
    return RiskOut.model_validate(risk)


@router.post("/{risk_id}/accept", response_model=RiskOut)
def accept_risk(
    project_id: int,
    risk_id: int,
    body: RiskAccept,
    user: Annotated[User, Depends(require(Action.RISK_ACCEPT))],
    db: Annotated[Session, Depends(get_db)],
) -> RiskOut:
    policy = _policy()
    risk = _risk(db, project_id, risk_id, for_update=True)
    _check_version(body.version, risk)
    if risk.status != "open":
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_OPEN)
    if risk.score is None:
        raise HTTPException(status.HTTP_409_CONFLICT, UNSCORED)
    owner = band_owner(policy, risk.band)
    if not may_accept_band(user.role, risk.band, policy):
        # The refusal is a decision too: recorded, then the 403. The reason is not kept, since
        # nothing was accepted.
        _record_refusal(db, risk, user, action="accept", band=risk.band, owner_role=owner)
        _commit(db)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"a {risk.band} risk can be accepted only by its owner role ({owner}) or above",
        )

    before = snapshot(risk)
    risk.response = "accept"
    risk.status = "accepted"
    risk.review_date = body.review_date
    settle_escalation(risk, policy, before=before)
    _flush(db)
    record_risk_change(
        db,
        risk,
        action="accept",
        before=before,
        actor_user_id=user.id,
        details={
            "reason": body.reason,
            "review_date": body.review_date.isoformat(),
            "band": risk.band,
            "owner_role": owner,
        },
    )
    _commit(db)
    return RiskOut.model_validate(risk)


def _decide_proposed(
    db: Session, policy: RiskPolicy, risk: Risk, *, new_status: str, user: User, reason: str | None
) -> RiskOut:
    if risk.status != "proposed":
        raise HTTPException(status.HTTP_409_CONFLICT, NOT_PROPOSED)
    try:
        decide_proposed(
            db,
            policy,
            risk,
            new_status=new_status,
            actor_user_id=user.id,
            details={"reason": reason} if reason is not None else None,
        )
    except StaleDataError:
        # Another write changed the row between our read and this update.
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, STALE) from None
    _commit(db)
    return RiskOut.model_validate(risk)


@router.post("/{risk_id}/open", response_model=RiskOut)
def open_risk(
    project_id: int,
    risk_id: int,
    body: RiskDecision,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> RiskOut:
    policy = _policy()
    risk = _risk(db, project_id, risk_id, for_update=True)
    _check_version(body.version, risk)
    return _decide_proposed(db, policy, risk, new_status="open", user=user, reason=None)


@router.post("/{risk_id}/reject", response_model=RiskOut)
def reject_risk(
    project_id: int,
    risk_id: int,
    body: RiskReject,
    user: Annotated[User, Depends(require(Action.PROJECT_WRITE))],
    db: Annotated[Session, Depends(get_db)],
) -> RiskOut:
    policy = _policy()
    risk = _risk(db, project_id, risk_id, for_update=True)
    _check_version(body.version, risk)
    return _decide_proposed(db, policy, risk, new_status="rejected", user=user, reason=body.reason)


@escalations_router.get("/escalations", response_model=Page[RiskEscalationOut])
def list_escalations(
    user: Annotated[User, Depends(require(Action.PROJECT_READ))],
    db: Annotated[Session, Depends(get_db)],
    limit: Limit = DEFAULT_LIMIT,
    offset: Offset = 0,
) -> Page[RiskEscalationOut]:
    """Open risks escalated to the caller's role, across projects, highest score first."""
    stmt = select(Risk).where(Risk.escalated_to == user.role, Risk.status == "open")
    rows, total = paginate(
        db, stmt, order_by=(Risk.score.desc(), Risk.id), limit=limit, offset=offset
    )
    project_ids = {row.project_id for row in rows}
    names = (
        dict(db.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids))).all())
        if project_ids
        else {}
    )
    items = [
        RiskEscalationOut.model_validate(row).model_copy(
            update={"project_name": names.get(row.project_id, "")}
        )
        for row in rows
    ]
    return Page[RiskEscalationOut](items=items, total=total, limit=limit, offset=offset)
