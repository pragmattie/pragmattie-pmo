"""Roles, actions and the one permissions table.

Every API route depends on a guard from this module, except the few on `OPEN_PATHS`. The API is
the enforcement point; the web only hides what a role cannot do.

`require(action)` gives 401 when nobody is signed in and 403 when the user's role lacks the
action. `current_user` loads the user's database row on every request and the role is read from
that row, never from the token, so a role change or deactivation applies at once. Each guard
carries a `pmo_guard` attribute so a test can check that every route has one.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import RevokedToken, User
from app.policy import RiskPolicy
from app.security import SESSION_COOKIE, decode_token


class Role(StrEnum):
    ADMIN = "admin"
    SPONSOR = "sponsor"
    PROJECT_MANAGER = "project_manager"
    ETHICS_REVIEWER = "ethics_reviewer"
    AUDITOR = "auditor"


class Action(StrEnum):
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    INTAKE_DECIDE = "intake:decide"
    RISK_ACCEPT = "risk:accept"
    GATE_DECIDE = "gate:decide"
    AUDIT_READ = "audit:read"
    USER_ADMIN = "user:admin"
    REPORT_PUBLISH = "report:publish"
    PROPOSAL_APPROVE = "proposal:approve"
    STANDARDS_READ = "standards:read"


# Admin manages users but holds no decision rights. Project managers accept Low risks and
# sponsors accept Medium and High ones: `may_accept_band` checks the band's owner in the risk
# policy file.
PERMISSIONS: dict[Role, frozenset[Action]] = {
    Role.ADMIN: frozenset(
        {Action.USER_ADMIN, Action.AUDIT_READ, Action.PROJECT_READ, Action.STANDARDS_READ}
    ),
    Role.SPONSOR: frozenset(
        {
            Action.PROJECT_READ,
            Action.INTAKE_DECIDE,
            Action.GATE_DECIDE,
            Action.RISK_ACCEPT,
            Action.PROPOSAL_APPROVE,
            Action.STANDARDS_READ,
        }
    ),
    Role.PROJECT_MANAGER: frozenset(
        {
            Action.PROJECT_READ,
            Action.PROJECT_WRITE,
            Action.RISK_ACCEPT,
            Action.REPORT_PUBLISH,
            Action.STANDARDS_READ,
        }
    ),
    Role.ETHICS_REVIEWER: frozenset(
        {Action.PROJECT_READ, Action.GATE_DECIDE, Action.AUDIT_READ, Action.STANDARDS_READ}
    ),
    Role.AUDITOR: frozenset({Action.PROJECT_READ, Action.AUDIT_READ, Action.STANDARDS_READ}),
}

# Actions that only read. The auditor is read-only across the board: a test checks its actions
# are all in this set.
READ_ACTIONS = frozenset({Action.PROJECT_READ, Action.AUDIT_READ, Action.STANDARDS_READ})

# Paths that need no guard: health, sign-in and sign-out (safe to repeat with an expired or
# missing session), and FastAPI's docs, which are served only when APP_ENV is dev or test.
OPEN_PATHS = frozenset(
    {
        "/api/v1/health",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)

NOT_SIGNED_IN = "not signed in or session expired"
FORBIDDEN = "your role does not allow this"


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    """The signed-in, active user, or 401. Rejects expired, revoked and malformed tokens."""
    not_signed_in = HTTPException(status.HTTP_401_UNAUTHORIZED, NOT_SIGNED_IN)
    token = request.cookies.get(SESSION_COOKIE)
    claims = decode_token(token) if token else None
    if claims is None:
        raise not_signed_in
    if db.get(RevokedToken, str(claims["jti"])) is not None:
        raise not_signed_in
    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError):
        raise not_signed_in from None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise not_signed_in
    return user


def allowed_actions(role: str) -> list[Action]:
    """The actions a role may take, sorted; none for an unknown role."""
    try:
        return sorted(PERMISSIONS[Role(role)])
    except ValueError:
        return []


def can(role: str, action: Action) -> bool:
    return action in allowed_actions(role)


# How senior each role is when accepting a risk: a role may accept a band whose escalation owner
# ranks at or below it. Roles not listed accept no band.
ROLE_RANK: dict[Role, int] = {Role.PROJECT_MANAGER: 1, Role.SPONSOR: 2}


def may_accept_band(role: str, band: str | None, policy: RiskPolicy) -> bool:
    """True when `role` holds `risk:accept` and ranks at or above `band`'s escalation owner in
    the risk policy. False for an unscored risk (no band) or an unknown role or band."""
    if band is None or not can(role, Action.RISK_ACCEPT):
        return False
    owner = next((b.escalation_owner for b in policy.priority_bands if b.name == band), None)
    try:
        return ROLE_RANK.get(Role(role), 0) >= ROLE_RANK[Role(owner)]
    except (KeyError, ValueError):
        return False


def require(action: Action) -> Callable[..., User]:
    """A FastAPI dependency: the signed-in user, if their role allows `action`."""

    def guard(user: Annotated[User, Depends(current_user)]) -> User:
        if not can(user.role, action):
            raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN)
        return user

    guard.pmo_guard = action  # type: ignore[attr-defined]
    guard.__name__ = f"require_{action.value.replace(':', '_')}"
    return guard


def signed_in(user: Annotated[User, Depends(current_user)]) -> User:
    """A guard for routes every signed-in user may use, whatever their role (`auth/me`)."""
    return user


signed_in.pmo_guard = "signed_in"  # type: ignore[attr-defined]


def not_own_submission(actor_id: int, submitter_id: int | None) -> None:
    """Separation of duties: nobody approves or decides on their own submission (403).

    The hook for later items (intake, gates, risks, proposals); no route calls it yet.
    """
    if submitter_id is not None and actor_id == submitter_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "you cannot decide on your own submission")
