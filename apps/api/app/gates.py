"""The automatic checks a gate criterion can name (item 4.4).

Each check reads the subject as it is now and returns whether it is met and a short, plain detail
for the person deciding. The names here must be exactly `app.policy.KNOWN_CHECKS`, which the
policy loader uses to refuse a gates file naming a check the code doesn't know.

The three readiness checks apply only to a request that uses AI (R11); for any other request they
are met on their own, and the detail says so.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.intake import readiness_gaps, readiness_of, value_case_gaps, value_case_of
from app.models import ProjectRequest, User


@dataclass(frozen=True)
class CheckResult:
    met: bool
    detail: str


NOT_AI = "not needed: this request doesn't use AI"

Check = Callable[[Session, ProjectRequest], CheckResult]


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


CHECKS: dict[str, Check] = {
    "value_case_confirmed": value_case_confirmed,
    "readiness_confirmed": readiness_confirmed,
    "readiness_not_red": readiness_not_red,
    "yellow_actions_owned": yellow_actions_owned,
}
