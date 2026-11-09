"""The simulated client (item 6.8): rules only, no Claude call.

In the demo, a proposal sent to a client whose `simulated` flag is set (its contact email is in
the reserved `.example` domain) is answered `SIMULATED_CLIENT_DELAY_MINUTES` after its `sent`
event, with the answer in the autonomy policy (`agents.simulated_client.answer`: `accepted`, or
`declined` for a demo). A real client is never answered here: a person records its answer.

The answer goes through the answer route's rules (`app.proposals.answer_refusal` and `answer`),
with no person and `source = simulated`, so the proposal page, the board and the audit rows say
"Simulated client". The orchestrator's `client_answer` step calls `answer_now`, which checks again
at its due time that the proposal is still `sent`, its project is still live (not cancelled, and
in planning or delivery, as for drafting and submitting), the client is still simulated, nothing
has answered it yet and the delay is over; otherwise it changes nothing and says why. It answers
at most once.
"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import proposals as rules
from app.models import Client, Project, ProjectProposal, ProposalEvent
from app.models.portfolio import is_live
from app.models.proposal import SIMULATED

AGENT = "simulated_client"
TRIGGER = "orchestrator"
LABEL = "Simulated client"

NOT_SENT = "the proposal is no longer waiting for an answer"
NOT_LIVE = "the project is cancelled or no longer in planning or delivery"
NOT_SIMULATED = "the client is not simulated, so a person records its answer"
ANSWERED = "the proposal already has an answer"
NOT_DUE = "the delay after the proposal was sent is not over yet"


class Skipped(Exception):
    """The simulated client found nothing to do; nothing changed."""


def is_simulated(db: Session, proposal: ProjectProposal) -> bool:
    project = db.get(Project, proposal.project_id)
    client = db.get(Client, project.client_id) if project and project.client_id else None
    return bool(client and client.simulated)


def sent_at(db: Session, proposal: ProjectProposal) -> datetime | None:
    """When the proposal was marked sent: its newest `sent` event."""
    stmt = (
        select(ProposalEvent.created_at)
        .where(ProposalEvent.proposal_id == proposal.id, ProposalEvent.kind == "sent")
        .order_by(ProposalEvent.id.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def due_at(db: Session, proposal: ProjectProposal, delay_minutes: int) -> datetime | None:
    """When the simulated client answers: the delay after the `sent` event; none without one."""
    sent = sent_at(db, proposal)
    return sent + timedelta(minutes=delay_minutes) if sent is not None else None


def _answered(db: Session, proposal: ProjectProposal) -> bool:
    stmt = select(ProposalEvent.id).where(
        ProposalEvent.proposal_id == proposal.id, ProposalEvent.kind == "answer"
    )
    return db.scalar(stmt.limit(1)) is not None


def skip_reason(
    db: Session, proposal: ProjectProposal, *, delay_minutes: int, now: datetime
) -> str | None:
    """Why the simulated client may not answer the proposal now, or None when it may."""
    if rules.answer_refusal(proposal) is not None:
        return NOT_SENT
    if not is_live(db.get(Project, proposal.project_id)):
        return NOT_LIVE
    if not is_simulated(db, proposal):
        return NOT_SIMULATED
    if _answered(db, proposal):
        return ANSWERED
    due = due_at(db, proposal, delay_minutes)
    if due is None or now < due:
        return NOT_DUE
    return None


def answer_now(
    db: Session,
    proposal_id: int,
    *,
    answer: str,
    delay_minutes: int,
    now: datetime,
) -> int:
    """Answer the proposal as the simulated client: one audit row (no person, `source =
    simulated`), whose id is returned. Raises `Skipped`, changing nothing, when it may not.
    The caller commits."""
    proposal = db.get(ProjectProposal, proposal_id, with_for_update=True, populate_existing=True)
    if proposal is None:
        raise Skipped(NOT_SENT)
    why = skip_reason(db, proposal, delay_minutes=delay_minutes, now=now)
    if why is not None:
        raise Skipped(why)
    return rules.answer(
        db,
        proposal,
        answer=answer,
        answered_on=now.date(),
        note=f"{LABEL}: answered automatically in the demo",
        actor_user_id=None,
        source=SIMULATED,
        trigger=TRIGGER,
        by=AGENT,
        delay_minutes=delay_minutes,
    )
