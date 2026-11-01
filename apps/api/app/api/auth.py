"""Sign in, sign out and who am I.

The session is an httpOnly, SameSite=Lax cookie holding a JWT; a second, readable cookie holds a
CSRF token that the web echoes in the `X-CSRF-Token` header on every unsafe method. Cookies are
Secure unless the `cookie_secure` setting is off (local development over plain http only).

Login accepts only an `application/json` body. A browser can't send that type cross-site without
a CORS preflight, so another site can't sign a visitor in with a plain form post.

`current_user` (in `app/authz.py`) loads the user from the database on every request. Code that
needs the role reads it from that row, never from the token, so a role change or deactivation
applies at once. Every response that describes the user also lists the actions their role allows,
so the web can hide what the role cannot do; the API still checks each request.

Login and logout need no guard (see `OPEN_PATHS` in `app/authz.py`); `me` needs only a session.

Login failures all get one generic message, and a locked account gets it too, only after the
password has been checked, so no response shows whether an account exists or is locked.

Audit: every login success, failure and lockout, and every logout that revokes a session, is one
`pmo_audit` row (agent `auth`, trigger `human`, subject type `login` with a fresh id per event).
`signals` holds only the event, a reason code, the client IP and the user id (null for an
unknown email). The typed email, the password and tokens are never stored, because people
sometimes type a password into the email box. `actor_user_id` is set only once a person has
proved who they are: on success and logout. A refused request (429) writes no row, so a flood
can't fill the table.

Lockout (our own numbers, in `app/config.py`): `lockout_threshold` failures within
`lockout_minutes` lock the account for `lockout_minutes` from the last of them. Attempts on a
locked account are refused and audited (reason `locked`) but neither count nor extend the lock,
so nobody can keep another person locked out by continuing to try. An admin can unlock early
(`app/api/users.py`). The counter is updated from a row reloaded with `SELECT ... FOR UPDATE`
after the password check, so parallel failures can't slip past the threshold; the row lock is
not held while argon2 runs.

Rate limit: each client IP (`request.client.host`; `X-Forwarded-For` is ignored, since anyone can
set it) may try `login_rate_per_minute` times per minute; further tries get 429 before anything
else runs. The limiter is in memory (`app/ratelimit.py`), so it covers one process only.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import record_decision
from app.authz import allowed_actions, signed_in
from app.config import get_settings
from app.db import get_db
from app.models import RevokedToken, User, utcnow
from app.models.auth import normalize_email
from app.ratelimit import SlidingWindowLimiter
from app.schemas.auth import PASSWORD_MAX_LENGTH, LoginIn, UserOut
from app.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    SessionToken,
    csrf_matches,
    decode_token,
    issue_token,
    new_csrf_token,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_PATH = "/api/v1/auth/login"
LOGOUT_PATH = "/api/v1/auth/logout"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOGIN_FAILED = "invalid email or password"
JSON_MEDIA_TYPE = "application/json"
TOO_MANY = "too many sign-in attempts; try again later"

AUDIT_AGENT = "auth"
LOGIN_SUBJECT = "login"

# Per-IP limit on login attempts in this process (see the module docstring).
login_limiter = SlidingWindowLimiter(window_seconds=60.0)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def record_auth_event(
    db: Session,
    event: str,
    *,
    ip: str,
    user_id: int | None,
    reason: str | None = None,
    actor_user_id: int | None = None,
    status_: str = "ok",
    action_taken: dict[str, Any] | None = None,
) -> None:
    """One audit row for a sign-in event. The caller commits."""
    signals: dict[str, Any] = {"event": event, "ip": ip, "user_id": user_id}
    if reason is not None:
        signals["reason"] = reason
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=LOGIN_SUBJECT,
        subject_id=str(uuid.uuid4()),
        status=status_,
        actor_user_id=actor_user_id,
        signals=signals,
        action_taken=action_taken,
    )


def user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        actions=allowed_actions(user.role),
    )


def _login_failed() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, LOGIN_FAILED)


def _is_json(request: Request) -> bool:
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    return media_type.strip().lower() == JSON_MEDIA_TYPE


def _naive_utc(timestamp: int | float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC).replace(tzinfo=None)


def _user_id(claims: dict[str, Any]) -> int | None:
    try:
        return int(claims["sub"])
    except (KeyError, TypeError, ValueError):
        return None


def require_csrf(request: Request) -> None:
    """Router-wide dependency: unsafe methods need the CSRF header to match the CSRF cookie.

    Login is exempt (there is no session yet), and so is a logout that carries no session
    cookie, so signing out is always safe to repeat.
    """
    if request.method in SAFE_METHODS or request.url.path == LOGIN_PATH:
        return
    if request.url.path == LOGOUT_PATH and SESSION_COOKIE not in request.cookies:
        return
    if not csrf_matches(request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=get_settings().cookie_secure,
        httponly=name == SESSION_COOKIE,
        samesite="lax",
    )


def _clear_cookies(response: Response) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            name,
            path="/",
            secure=get_settings().cookie_secure,
            httponly=name == SESSION_COOKIE,
            samesite="lax",
        )


def _fail(
    db: Session, ip: str, user: User | None, reason: str, *, count_at: datetime | None = None
) -> HTTPException:
    """Audit a failed login, count it against the user if `count_at` is given, and commit both
    before the 401 is raised, so a failure never loses its row."""
    user_id = user.id if user else None
    record_auth_event(
        db, "login_failure", ip=ip, user_id=user_id, reason=reason, status_="rejected"
    )
    if count_at is not None and user is not None:
        _count_failure(db, user, ip, count_at)
    db.commit()
    return _login_failed()


def _count_failure(db: Session, user: User, ip: str, now: datetime) -> None:
    """Add a failure to the user's window, locking the account when it reaches the threshold."""
    settings = get_settings()
    window = timedelta(minutes=settings.lockout_minutes)
    started = user.failed_window_started_at
    if started is None or now - started >= window:
        user.failed_window_started_at = now
        user.failed_logins = 1
    else:
        user.failed_logins += 1
    if user.failed_logins >= settings.lockout_threshold:
        user.locked_until = now + window
        # The lock starts a fresh count once it expires.
        user.failed_logins = 0
        user.failed_window_started_at = None
        record_auth_event(
            db,
            "lockout",
            ip=ip,
            user_id=user.id,
            reason="too_many_failures",
            action_taken={"action": "lockout", "locked_until": user.locked_until.isoformat()},
        )


def _authenticate(
    db: Session, body: LoginIn | None, ip: str, old_jti: str | None, old_exp: int | None
) -> tuple[User, SessionToken]:
    if body is None:
        raise _fail(db, ip, None, "invalid_request")
    user = db.scalar(select(User).where(User.email == normalize_email(body.email)))
    # Verify even for an unknown user (against a dummy hash) so both take similar time. No row
    # lock is held while argon2 runs.
    password_ok = verify_password(user.password_hash if user else None, body.password)
    if user is None:
        raise _fail(db, ip, None, "unknown_user")
    # Reload the row under a lock so parallel failures count one at a time.
    user = db.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = utcnow()
    if user.locked_until is not None and user.locked_until > now:
        # Neither counted nor extended: the lock runs from the failure that set it.
        raise _fail(db, ip, user, "locked")
    if not password_ok or not user.is_active:
        reason = "bad_password" if not password_ok else "inactive"
        raise _fail(db, ip, user, reason, count_at=now)

    user.failed_logins = 0
    user.failed_window_started_at = None
    user.locked_until = None
    user.last_login_at = now
    # Signing in replaces a session the browser still holds.
    if old_jti is not None and old_exp is not None and db.get(RevokedToken, old_jti) is None:
        db.add(RevokedToken(jti=old_jti, expires_at=_naive_utc(old_exp)))
    session = issue_token(user.id, user.role)
    record_auth_event(db, "login_success", ip=ip, user_id=user.id, actor_user_id=user.id)
    db.commit()
    return user, session


@router.post(
    "/login",
    response_model=UserOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {JSON_MEDIA_TYPE: {"schema": LoginIn.model_json_schema()}},
        }
    },
)
async def login(
    request: Request, response: Response, db: Annotated[Session, Depends(get_db)]
) -> UserOut:
    ip = _client_ip(request)
    # The rate limit comes first, so a refused request does no other work and writes no row.
    if not login_limiter.allow(ip, get_settings().login_rate_per_minute):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, TOO_MANY, headers={"Retry-After": "60"}
        )
    # The body is parsed here, not by FastAPI, so a bad body gets the generic message and a
    # validation error can never echo the password back. Only JSON is accepted (see above).
    body: LoginIn | None = None
    if _is_json(request):
        try:
            body = LoginIn.model_validate(await request.json())
        except (ValueError, ValidationError):
            body = None
    if body is not None and len(body.password) > PASSWORD_MAX_LENGTH:
        body = None
    token = request.cookies.get(SESSION_COOKIE)
    old = decode_token(token) if token else None
    old_jti, old_exp = (str(old["jti"]), old["exp"]) if old else (None, None)
    # argon2 and the database block, so they run off the event loop.
    user, session = await run_in_threadpool(_authenticate, db, body, ip, old_jti, old_exp)

    max_age = get_settings().jwt_ttl_minutes * 60
    _set_cookie(response, SESSION_COOKIE, session.token, max_age)
    _set_cookie(response, CSRF_COOKIE, new_csrf_token(), max_age)
    return user_out(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Annotated[Session, Depends(get_db)]) -> None:
    """Revoke the session until it would expire, and clear both cookies.

    Safe to repeat: a missing, expired or already revoked session still succeeds. Only a logout
    that revokes a session writes an audit row, so a repeat writes none.
    """
    token = request.cookies.get(SESSION_COOKIE)
    claims = decode_token(token) if token else None
    now = utcnow()
    if claims is not None:
        jti = str(claims["jti"])
        if db.get(RevokedToken, jti) is None:
            db.add(RevokedToken(jti=jti, expires_at=_naive_utc(claims["exp"])))
            claimed = _user_id(claims)
            user = db.get(User, claimed) if claimed is not None else None
            user_id = user.id if user else None
            record_auth_event(
                db, "logout", ip=_client_ip(request), user_id=user_id, actor_user_id=user_id
            )
    # Keep the table small: a token past its expiry is rejected anyway.
    db.execute(delete(RevokedToken).where(RevokedToken.expires_at < now))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent logout of the same session already revoked it.
        db.rollback()
    _clear_cookies(response)


@router.get("/me", response_model=UserOut)
def me(user: Annotated[User, Depends(signed_in)]) -> UserOut:
    return user_out(user)
