"""Risk register logic shared by the API and the `rescore` command: scoring and audit rows.

`python -m app.risks rescore` recomputes every risk's score and band from the current risk policy.
Run it after the risk policy file changes: stored scores and bands are otherwise only refreshed
when a risk is next edited.

`risk_from_template` turns a starter risk library template into a `proposed`, unscored risk.

Every create, change, close, decision and re-score of a risk is one audit row: agent
`risk_register`, subject the risk, `version` the risk's version after the change, the values before
in `signals` (none for a create) and the values after in `output`.

Escalation: each priority band names an escalation owner role in the risk policy. When an open risk
enters a band whose owner differs from its band's owner before (a new or newly opened risk had
none), it is escalated to the new owner: `escalated_to` and `escalated_band` are set and one more
audit row (action `escalate`) names the band, the owner role and the risk. The escalation stays
through edits that keep the owner, and clears when the risk leaves `open` (accepted, rejected or
closed) or loses its score. Re-scoring an accepted risk into a higher band reopens it, which
escalates it again. Who may close an escalated risk or lower its band is the PATCH route's
escalation hold (`app.api.risks`).

An audit row is unique per risk version and `attempt`, so the rows for one version count up: the
change is attempt 1, and an escalation or a refused acceptance at the same version takes the next
attempt (`next_attempt`).
"""

import argparse
import sys
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import policy as policies
from app.audit import record_decision
from app.models import AuditDecision, Risk
from app.policy import PolicyError, RiskLibrary, RiskPolicy, RiskTemplate, get_policies, score_risk

AUDIT_AGENT = "risk_register"
AUDIT_SUBJECT = "risk"

# The values an audit row records, before and after.
SNAPSHOT_FIELDS = (
    "project_id",
    "kind",
    "title",
    "cause",
    "effect",
    "category",
    "objectives",
    "probability",
    "impact",
    "score",
    "band",
    "response",
    "response_plan",
    "owner_user_id",
    "status",
    "needs_judgement",
    "source",
    "escalated_to",
    "escalated_band",
    "review_date",
    "version",
)


def risk_policy() -> RiskPolicy:
    """The risk policy; raises `PolicyError` when the policy files can't be loaded."""
    return get_policies().risk


def risk_library() -> RiskLibrary:
    """The starter risk library; raises `PolicyError` when its file can't be loaded."""
    return policies.get_risk_library()


def risk_from_template(project_id: int, template: RiskTemplate) -> Risk:
    """A `proposed` risk from a person, with no score: the project's people score it."""
    return Risk(
        project_id=project_id,
        kind=template.kind,
        title=template.title,
        cause=template.cause,
        effect=template.effect,
        category=template.category,
        objectives=list(template.objectives),
        probability=None,
        impact=None,
        score=None,
        band=None,
        response=template.response,
        response_plan=template.response_plan,
        owner_user_id=None,
        status="proposed",
        needs_judgement=False,
        source="person",
    )


def apply_score(risk: Risk, policy: RiskPolicy) -> None:
    """Set the risk's score and band from the policy. Only the server ever writes them."""
    risk.score, risk.band = score_risk(policy, risk.probability, risk.impact)


def snapshot(risk: Risk) -> dict[str, Any]:
    values = {name: getattr(risk, name) for name in SNAPSHOT_FIELDS}
    values["objectives"] = list(values["objectives"] or [])
    if values["review_date"] is not None:
        values["review_date"] = values["review_date"].isoformat()
    return values


def band_owner(policy: RiskPolicy, band: str | None) -> str | None:
    """The role a band escalates to, from the policy; none for no band or an unknown one."""
    return next((b.escalation_owner for b in policy.priority_bands if b.name == band), None)


def band_rank(policy: RiskPolicy, band: str | None) -> int:
    """A band's place in the policy, lowest first from 0; -1 for no band or an unknown one."""
    names = [b.name for b in policy.priority_bands]
    return names.index(band) if band in names else -1


def settle_escalation(risk: Risk, policy: RiskPolicy, *, before: dict[str, Any] | None) -> bool:
    """Bring the risk's escalation in line with its status and band, before it is flushed.

    `before` is the risk's snapshot before the change (none for a new risk). Returns True when the
    risk has just been escalated, so the caller records it with `record_escalation` once the
    change is flushed and its own row written."""
    if risk.status != "open" or risk.band is None:
        risk.escalated_to = risk.escalated_band = None
        return False
    was_open = before is not None and before["status"] == "open"
    owner = band_owner(policy, risk.band)
    previous = band_owner(policy, before["band"]) if was_open else None
    if owner != previous:
        risk.escalated_to, risk.escalated_band = owner, risk.band
        return True
    if risk.escalated_to is not None:
        risk.escalated_band = risk.band
    return False


def rescore_decisions(risk: Risk, policy: RiskPolicy, before: dict[str, Any]) -> bool:
    """After a change that may have moved the risk's band: reopen an accepted risk that rose into
    a higher band (its review date goes with its acceptance), then settle the escalation. Returns
    `settle_escalation`'s answer."""
    if risk.status == "accepted" and band_rank(policy, risk.band) > band_rank(
        policy, before["band"]
    ):
        risk.status = "open"
        risk.review_date = None
    return settle_escalation(risk, policy, before=before)


def next_attempt(db: Session, risk: Risk) -> int:
    """The next free audit `attempt` for the risk's current version: 1 when it has no row yet."""
    stmt = select(func.max(AuditDecision.attempt)).where(
        AuditDecision.agent == AUDIT_AGENT,
        AuditDecision.subject_type == AUDIT_SUBJECT,
        AuditDecision.subject_id == str(risk.id),
        AuditDecision.version == risk.version,
    )
    return (db.scalar(stmt) or 0) + 1


def record_risk_change(
    db: Session,
    risk: Risk,
    *,
    action: str,
    before: dict[str, Any] | None,
    actor_user_id: int | None,
    details: dict[str, Any] | None = None,
) -> None:
    """One audit row for a change already flushed to `risk` (so its version is the new one).
    `details` adds to the recorded action, e.g. the library template a risk came from."""
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=AUDIT_SUBJECT,
        subject_id=str(risk.id),
        version=risk.version,
        attempt=next_attempt(db, risk),
        status="ok",
        actor_user_id=actor_user_id,
        signals=before,
        output=snapshot(risk),
        action_taken={"action": action, **(details or {})},
    )


def record_escalation(db: Session, risk: Risk, *, actor_user_id: int | None) -> None:
    """The escalation audit row: the band, the role it now goes to, and the risk."""
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=AUDIT_SUBJECT,
        subject_id=str(risk.id),
        version=risk.version,
        attempt=next_attempt(db, risk),
        status="ok",
        actor_user_id=actor_user_id,
        output=snapshot(risk),
        action_taken={
            "action": "escalate",
            "risk_id": risk.id,
            "band": risk.escalated_band,
            "owner_role": risk.escalated_to,
        },
    )


def rescore(db: Session, policy: RiskPolicy) -> int:
    """Bring every risk's score and band in line with `policy`, in one transaction. Each risk
    whose score or band changes gets one audit row (action `rescore`, no actor), and one more when
    that escalates it; an accepted risk that rises into a higher band reopens. Returns how many
    risks changed."""
    changed = 0
    for risk in db.scalars(select(Risk).order_by(Risk.id)):
        if score_risk(policy, risk.probability, risk.impact) == (risk.score, risk.band):
            continue
        before = snapshot(risk)
        apply_score(risk, policy)
        escalated = rescore_decisions(risk, policy, before)
        db.flush()
        record_risk_change(db, risk, action="rescore", before=before, actor_user_id=None)
        if escalated:
            record_escalation(db, risk, actor_user_id=None)
        changed += 1
    db.commit()
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.risks", description="Risk register tasks.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("rescore", help="recompute every score and band from the risk policy")
    parser.parse_args(argv)

    from app.db import SessionLocal

    try:
        policy = risk_policy()
    except PolicyError as exc:
        print(f"rescore: the risk policy could not be loaded: {exc}", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        changed = rescore(db, policy)
    print(f"rescore: {changed} risk(s) changed score or band")
    return 0


if __name__ == "__main__":
    sys.exit(main())
