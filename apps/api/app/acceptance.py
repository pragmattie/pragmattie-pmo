"""Deliverable acceptance (item 7.6): rules in code, no model.

A deliverable leaves delivery as `accepted` only through a recorded answer to an offer.

- **Offer.** The project manager offers a deliverable that is `ready_for_acceptance`, recording
  to whom, on what date and by what means. Nothing is sent from here. An offer opens the next
  round, and is refused while the latest round's offer is still waiting for its answer.
- **Answer.** An answer is refused unless the deliverable's latest round is an offer with no
  answer yet (so no answer without an offer, and never a second answer to one offer), and the
  deliverable is still `ready_for_acceptance`. `accepted` sets the deliverable to `accepted`;
  `not_accepted` records what is missing and sets it back to `in_progress`, so the project
  manager works on it and offers it again in a new round. Every round is kept. While an offer
  waits for its answer, the status route won't move the deliverable out of
  `ready_for_acceptance` (`offer_waiting`): the answer does.
- **Who answers.** For a client project, a person with `project:write` records the client's
  answer (`answerer_refusal`). A project with no client has its deliverables accepted by the
  sponsor instead, with `gate:decide`, and never by the project's own project manager. For a
  simulated client, the simulated client answers (`app.simulated_client`, `source = simulated`);
  a person may still record an answer first.
- **Dates.** Neither the offer nor the answer may be dated after today (UTC), and an answer may
  not come before its offer.

Each offer and each answer is one append-only `deliverable_acceptances` row and one audit row on
the deliverable (`record`): agent `delivery` for a person, `simulated_client` for the simulated
client. The callers record refused attempts and commit.
"""

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import delivery
from app.audit import next_attempt, record_decision
from app.authz import Action, can
from app.models import DeliverableAcceptance, PlanDeliverable, Project, User
from app.models.delivery import ACCEPTED_ANSWER, ANSWER, NOT_ACCEPTED, OFFER, PERSON, SIMULATED
from app.models.plan import ACCEPTED
from app.roles import Role

READY = "ready_for_acceptance"
IN_PROGRESS = "in_progress"
SUBJECT_TYPE = "plan_deliverable"

NOT_READY = "only a deliverable that is ready for acceptance can be offered or answered"
ALREADY_OFFERED = "this deliverable's offer is still waiting for its answer"
NO_OFFER = (
    "an answer needs an offer that is waiting for one: offer the deliverable first (each offer "
    "takes one answer)"
)
FUTURE = "a date can't be after today"
BEFORE_OFFER = "the answer can't come before the offer"
NO_CLIENT_ANSWERER = (
    "this project has no client, so the sponsor accepts its deliverables (gate:decide)"
)
OWN_PROJECT = "you cannot accept the deliverables of a project you manage"
CLIENT_ANSWERER = "a person with project:write records the client's answer"
NO_PERSON = "only the simulated client's answer has no person"


def history(db: Session, deliverable_id: int) -> list[DeliverableAcceptance]:
    """Every offer and answer for the deliverable, oldest first."""
    stmt = (
        select(DeliverableAcceptance)
        .where(DeliverableAcceptance.deliverable_id == deliverable_id)
        .order_by(DeliverableAcceptance.id)
    )
    return list(db.scalars(stmt))


def histories(db: Session, deliverable_ids: list[int]) -> dict[int, list[DeliverableAcceptance]]:
    """`history` for several deliverables in one read."""
    found: dict[int, list[DeliverableAcceptance]] = {i: [] for i in deliverable_ids}
    if not deliverable_ids:
        return found
    stmt = (
        select(DeliverableAcceptance)
        .where(DeliverableAcceptance.deliverable_id.in_(deliverable_ids))
        .order_by(DeliverableAcceptance.id)
    )
    for row in db.scalars(stmt):
        found[row.deliverable_id].append(row)
    return found


def latest(db: Session, deliverable_id: int) -> DeliverableAcceptance | None:
    stmt = (
        select(DeliverableAcceptance)
        .where(DeliverableAcceptance.deliverable_id == deliverable_id)
        .order_by(DeliverableAcceptance.id.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def waiting_offer(db: Session, deliverable_id: int) -> DeliverableAcceptance | None:
    """The latest round's offer, when it has no answer yet; else None."""
    row = latest(db, deliverable_id)
    return row if row is not None and row.kind == OFFER else None


def offer_refusal(db: Session, deliverable: PlanDeliverable, offered_on: date) -> tuple | None:
    """Why the deliverable can't be offered now: (refusal, message, is_a_conflict), or None."""
    if deliverable.status != READY:
        return "not_ready", NOT_READY, True
    if waiting_offer(db, deliverable.id) is not None:
        return "already_offered", ALREADY_OFFERED, True
    if offered_on > delivery.today():
        return "future_date", FUTURE, False
    return None


def answer_refusal(
    db: Session, deliverable: PlanDeliverable, answered_on: date
) -> tuple[str, str, bool] | None:
    """Why the deliverable can't be answered now: (refusal, message, is_a_conflict), or None."""
    offer = waiting_offer(db, deliverable.id)
    if offer is None:
        return "no_offer", NO_OFFER, True
    if deliverable.status != READY:
        return "not_ready", NOT_READY, True
    if answered_on > delivery.today():
        return "future_date", FUTURE, False
    if offer.offered_on is not None and answered_on < offer.offered_on:
        return "before_offer", BEFORE_OFFER, False
    return None


def answerer_refusal(user: User, project: Project) -> tuple[str, str] | None:
    """Who may record an answer on `project`'s deliverables: for a client project a role with
    `project:write`; with no client the sponsor (`gate:decide`), never the project's own project
    manager. (refusal, message), or None when `user` may."""
    if project.client_id is not None:
        if not can(user.role, Action.PROJECT_WRITE):
            return "client_answer_needs_project_write", CLIENT_ANSWERER
        return None
    if user.role != Role.SPONSOR or not can(user.role, Action.GATE_DECIDE):
        return "sponsor_accepts", NO_CLIENT_ANSWERER
    if project.pm_user_id == user.id:
        return "own_project", OWN_PROJECT
    return None


def snapshot(deliverable: PlanDeliverable) -> dict[str, Any]:
    return {
        "name": deliverable.name,
        "status": deliverable.status,
        "status_version": deliverable.status_version,
    }


def row_snapshot(row: DeliverableAcceptance) -> dict[str, Any]:
    return {
        "id": row.id,
        "round": row.round,
        "kind": row.kind,
        "offered_to": row.offered_to,
        "offered_on": row.offered_on.isoformat() if row.offered_on else None,
        "means": row.means,
        "answer": row.answer,
        "answered_on": row.answered_on.isoformat() if row.answered_on else None,
        "missing": row.missing,
        "note": row.note,
        "source": row.source,
    }


def record(
    db: Session,
    deliverable: PlanDeliverable,
    *,
    agent: str,
    trigger: str,
    actor_user_id: int | None,
    action: str,
    before: dict[str, Any],
    row: DeliverableAcceptance,
    project_id: int,
    **extra: Any,
) -> int:
    """One audit row for an offer or answer on the deliverable; its id."""
    version = deliverable.status_version
    audit = record_decision(
        db,
        agent=agent,
        trigger=trigger,
        subject_type=SUBJECT_TYPE,
        subject_id=str(deliverable.id),
        version=version,
        attempt=next_attempt(
            db,
            agent=agent,
            subject_type=SUBJECT_TYPE,
            subject_id=str(deliverable.id),
            version=version,
        ),
        status="ok",
        actor_user_id=actor_user_id,
        signals=before,
        output={**snapshot(deliverable), "acceptance": row_snapshot(row)},
        action_taken={"action": action, "project_id": project_id, "source": row.source, **extra},
    )
    return audit.id


def _next_round(db: Session, deliverable_id: int) -> int:
    stmt = select(func.max(DeliverableAcceptance.round)).where(
        DeliverableAcceptance.deliverable_id == deliverable_id
    )
    return (db.scalar(stmt) or 0) + 1


def offer(
    db: Session,
    project: Project,
    deliverable: PlanDeliverable,
    *,
    offered_to: str,
    offered_on: date,
    means: str,
    note: str | None,
    user: User,
    agent: str,
) -> DeliverableAcceptance:
    """Record an offer the caller has checked with `offer_refusal`, and its audit row."""
    before = snapshot(deliverable)
    row = DeliverableAcceptance(
        project_id=project.id,
        deliverable_id=deliverable.id,
        deliverable_name=deliverable.name,
        round=_next_round(db, deliverable.id),
        kind=OFFER,
        offered_to=offered_to,
        offered_on=offered_on,
        means=means,
        note=note,
        source=PERSON,
        recorded_by=user.id,
    )
    db.add(row)
    db.flush()
    record(
        db,
        deliverable,
        agent=agent,
        trigger="human",
        actor_user_id=user.id,
        action="deliverable_offered",
        before=before,
        row=row,
        project_id=project.id,
    )
    return row


class Lost(Exception):
    """The deliverable's status moved while the answer was being saved; nothing was kept."""


def answer(
    db: Session,
    project: Project,
    deliverable: PlanDeliverable,
    *,
    answer: str,
    answered_on: date,
    missing: str | None,
    note: str | None,
    actor_user_id: int | None,
    source: str,
    agent: str,
    trigger: str,
    **extra: Any,
) -> tuple[DeliverableAcceptance, int]:
    """Record the answer to the deliverable's waiting offer, which the caller has locked and
    checked with `answer_refusal`, and move the deliverable: `accepted`, or back to
    `in_progress`. Returns the row and its audit row's id; raises `Lost` when the status moved
    (the caller rolls back)."""
    if actor_user_id is None and source != SIMULATED:
        raise ValueError(NO_PERSON)
    if answer not in (ACCEPTED_ANSWER, NOT_ACCEPTED):
        raise ValueError(f"unknown answer: {answer}")
    offered = waiting_offer(db, deliverable.id)
    if offered is None:
        raise ValueError(NO_OFFER)
    before = snapshot(deliverable)
    row = DeliverableAcceptance(
        project_id=project.id,
        deliverable_id=deliverable.id,
        deliverable_name=deliverable.name,
        round=offered.round,
        kind=ANSWER,
        answer=answer,
        answered_on=answered_on,
        missing=missing if answer == NOT_ACCEPTED else None,
        note=note,
        source=source,
        recorded_by=actor_user_id,
    )
    db.add(row)
    db.flush()
    to = ACCEPTED if answer == ACCEPTED_ANSWER else IN_PROGRESS
    if not delivery.set_status(db, deliverable, deliverable.status_version, to):
        raise Lost
    audit_id = record(
        db,
        deliverable,
        agent=agent,
        trigger=trigger,
        actor_user_id=actor_user_id,
        action="deliverable_accepted" if answer == ACCEPTED_ANSWER else "deliverable_not_accepted",
        before=before,
        row=row,
        project_id=project.id,
        **extra,
    )
    return row, audit_id
