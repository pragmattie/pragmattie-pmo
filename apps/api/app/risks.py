"""Risk register logic shared by the API and the `rescore` command: scoring and audit rows.

`python -m app.risks rescore` recomputes every risk's score and band from the current risk policy.
Run it after the risk policy file changes: stored scores and bands are otherwise only refreshed
when a risk is next edited.

`risk_from_template` turns a starter risk library template into a `proposed`, unscored risk.

Every create, change, close and re-score of a risk is one audit row: agent `risk_register`,
subject the risk, `version` the risk's version after the change, the values before in `signals`
(none for a create) and the values after in `output`.
"""

import argparse
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import policy as policies
from app.audit import record_decision
from app.models import Risk
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
    return values


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
        attempt=1,
        status="ok",
        actor_user_id=actor_user_id,
        signals=before,
        output=snapshot(risk),
        action_taken={"action": action, **(details or {})},
    )


def rescore(db: Session, policy: RiskPolicy) -> int:
    """Bring every risk's score and band in line with `policy`, in one transaction. Each risk
    whose score or band changes gets one audit row (action `rescore`, no actor). Returns how
    many risks changed."""
    changed = 0
    for risk in db.scalars(select(Risk).order_by(Risk.id)):
        if score_risk(policy, risk.probability, risk.impact) == (risk.score, risk.band):
            continue
        before = snapshot(risk)
        apply_score(risk, policy)
        db.flush()
        record_risk_change(db, risk, action="rescore", before=before, actor_user_id=None)
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
