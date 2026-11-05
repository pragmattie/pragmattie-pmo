"""The automatic checks a gate criterion can name (items 4.4 and 5.4).

Each check reads the subject (a project request, or a project for the go-ahead gate) as it is now
and returns whether it is met and a short, plain detail for the person deciding. The names here
must be exactly `app.policy.KNOWN_CHECKS`, which the policy loader uses to refuse a gates file
naming a check the code doesn't know, and each one works for the subjects
`app.policy.CHECK_SUBJECTS` gives it.

The readiness checks apply only to a request that uses AI (R11); for any other request they are
met on their own, and the detail says so. A project's `readiness_not_red` reads the request it
came from by the same rule.

A missing input is unmet, never met: no plan, no top budget in the brief, or no proposal for a
client project each fail their check.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.intake import readiness_gaps, readiness_of, value_case_gaps, value_case_of
from app.models import (
    PlanEstimateLine,
    Project,
    ProjectBrief,
    ProjectPlan,
    ProjectProposal,
    ProjectRequest,
    Risk,
    User,
)
from app.plans import compute_total, missing


@dataclass(frozen=True)
class CheckResult:
    met: bool
    detail: str


NOT_AI = "not needed: this request doesn't use AI"

NO_PLAN = "this project has no plan"
NO_TOP_BUDGET = "The brief has no top budget"

Check = Callable[[Session, ProjectRequest], CheckResult]
ProjectCheck = Callable[[Session, Project], CheckResult]


def value_case_confirmed(db: Session, request: ProjectRequest) -> CheckResult:
    gaps = value_case_gaps(value_case_of(db, request.id))
    if gaps:
        return CheckResult(False, "missing: " + "; ".join(gaps))
    return CheckResult(True, "complete and confirmed by a person")


def readiness_confirmed(db: Session, request: ProjectRequest) -> CheckResult:
    if not request.uses_ai:
        return CheckResult(True, NOT_AI)
    gaps = readiness_gaps(readiness_of(db, request.id))
    if gaps:
        return CheckResult(False, "missing: " + "; ".join(gaps))
    return CheckResult(True, "complete and confirmed by a person")


def readiness_not_red(db: Session, request: ProjectRequest) -> CheckResult:
    if not request.uses_ai:
        return CheckResult(True, NOT_AI)
    check = readiness_of(db, request.id)
    if check is None:
        return CheckResult(False, "no readiness check yet")
    red = [area for area, rating in check.ratings().items() if rating == "red"]
    if red:
        return CheckResult(False, "rated red: " + ", ".join(red))
    if check.overall not in ("green", "yellow"):
        return CheckResult(False, "not every area is rated and confirmed yet")
    return CheckResult(True, f"overall {check.overall}")


def yellow_actions_owned(db: Session, request: ProjectRequest) -> CheckResult:
    if not request.uses_ai:
        return CheckResult(True, NOT_AI)
    check = readiness_of(db, request.id)
    if check is None:
        return CheckResult(False, "no readiness check yet")
    yellow = [area for area, rating in check.ratings().items() if rating == "yellow"]
    owners = {a.owner_id for area in yellow for a in check.area_actions(area)}
    active = set(db.scalars(select(User.id).where(User.id.in_(owners), User.is_active.is_(True))))
    unowned = [
        area
        for area in yellow
        if not check.area_actions(area)
        or any(a.owner_id not in active for a in check.area_actions(area))
    ]
    if unowned:
        return CheckResult(False, "needs an action with an active owner: " + ", ".join(unowned))
    if not yellow:
        return CheckResult(True, "no area is rated yellow")
    return CheckResult(True, "every yellow area has an action with an active owner")


REQUEST_CHECKS: dict[str, Check] = {
    "value_case_confirmed": value_case_confirmed,
    "readiness_confirmed": readiness_confirmed,
    "readiness_not_red": readiness_not_red,
    "yellow_actions_owned": yellow_actions_owned,
}


# --- The go-ahead gate's checks (item 5.4): the subject is a project.


def _plan_of(db: Session, project: Project) -> ProjectPlan | None:
    return db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))


def plan_complete(db: Session, project: Project) -> CheckResult:
    plan = _plan_of(db, project)
    if plan is None:
        return CheckResult(False, NO_PLAN)
    gaps = missing(db, plan)
    if gaps:
        return CheckResult(False, "missing: " + "; ".join(gaps))
    return CheckResult(True, "nothing is missing")


def estimate_within_budget(db: Session, project: Project) -> CheckResult:
    plan = _plan_of(db, project)
    if plan is None:
        return CheckResult(False, NO_PLAN)
    brief = db.scalar(select(ProjectBrief).where(ProjectBrief.project_id == project.id))
    if brief is None or brief.budget_high is None:
        return CheckResult(False, NO_TOP_BUDGET)
    amounts = db.scalars(select(PlanEstimateLine.amount).where(PlanEstimateLine.plan_id == plan.id))
    total = compute_total(amounts, plan.contingency_pct)
    top = brief.budget_high
    if total > top:
        return CheckResult(False, f"the total {total:.2f} is above the top budget {top:.2f}")
    return CheckResult(True, f"the total {total:.2f} is within the top budget {top:.2f}")


def proposal_accepted(db: Session, project: Project) -> CheckResult:
    if project.client_id is None:
        return CheckResult(True, "not needed: this project has no client")
    proposal = db.scalar(select(ProjectProposal).where(ProjectProposal.project_id == project.id))
    if proposal is None:
        return CheckResult(False, "this project has no proposal yet")
    if proposal.status != "accepted":
        return CheckResult(False, f"the latest proposal is {proposal.status}, not accepted")
    return CheckResult(True, "the client accepted the latest proposal")


def _risk_titles(risks: list[Risk]) -> str:
    return "; ".join(f"'{risk.title}'" for risk in risks)


def no_unreviewed_risks(db: Session, project: Project) -> CheckResult:
    stmt = (
        select(Risk)
        .where(Risk.project_id == project.id, Risk.status == "proposed")
        .order_by(Risk.id)
    )
    proposed = list(db.scalars(stmt))
    if proposed:
        return CheckResult(False, "still proposed: " + _risk_titles(proposed))
    return CheckResult(True, "no risk is still proposed")


def escalations_settled(db: Session, project: Project) -> CheckResult:
    """An open risk with `escalated_to` set waits on that role; `app.risks.settle_escalation`
    clears it when the escalation owner acts."""
    stmt = (
        select(Risk)
        .where(
            Risk.project_id == project.id,
            Risk.status == "open",
            Risk.escalated_to.is_not(None),
        )
        .order_by(Risk.id)
    )
    waiting = list(db.scalars(stmt))
    if waiting:
        named = "; ".join(f"'{r.title}' (with {r.escalated_to})" for r in waiting)
        return CheckResult(False, "waiting on an escalation owner: " + named)
    return CheckResult(True, "no open risk is waiting on an escalation owner")


def project_readiness_not_red(db: Session, project: Project) -> CheckResult:
    """The approval gate's rule, read through the request the project came from."""
    request = db.scalar(select(ProjectRequest).where(ProjectRequest.project_id == project.id))
    if request is None:
        return CheckResult(True, "not needed: this project didn't come from a request")
    return readiness_not_red(db, request)


PROJECT_CHECKS: dict[str, ProjectCheck] = {
    "plan_complete": plan_complete,
    "estimate_within_budget": estimate_within_budget,
    "proposal_accepted": proposal_accepted,
    "no_unreviewed_risks": no_unreviewed_risks,
    "escalations_settled": escalations_settled,
    "readiness_not_red": project_readiness_not_red,
}

CHECKS_BY_SUBJECT: dict[str, dict[str, Callable[[Session, object], CheckResult]]] = {
    "request": REQUEST_CHECKS,  # type: ignore[dict-item]
    "project": PROJECT_CHECKS,  # type: ignore[dict-item]
}

# Every check name, each with the subjects it is implemented for, in the loader's order.
CHECKS: dict[str, tuple[str, ...]] = {
    name: tuple(subject for subject, checks in CHECKS_BY_SUBJECT.items() if name in checks)
    for name in dict.fromkeys([*REQUEST_CHECKS, *PROJECT_CHECKS])
}
