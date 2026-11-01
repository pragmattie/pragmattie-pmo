"""Sign in, sign out and who am I.

The session is an httpOnly, SameSite=Lax cookie holding a JWT; a second, readable cookie holds a
CSRF token that the web echoes in the `X-CSRF-Token` header on every unsafe method. Cookies are
Secure except when the API is reached on localhost.

`current_user` loads the user from the database on every request. Code that needs the role reads
it from that row, never from the token, so a role change or deactivation applies at once.

Login failures all get one generic message. Login and logout are not yet audited (item 2.3).
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import RevokedToken, User, utcnow
from app.models.auth import normalize_email
from app.schemas.auth import PASSWORD_MAX_LENGTH, LoginIn, UserOut
from app.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    SessionToken,
    cookie_secure,
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
NOT_SIGNED_IN = "not signed in or session expired"


def _login_failed() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, LOGIN_FAILED)


def _naive_utc(timestamp: int | float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC).replace(tzinfo=None)


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


def _set_cookie(response: Response, request: Request, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        secure=cookie_secure(request.url.hostname),
        httponly=name == SESSION_COOKIE,
        samesite="lax",
    )


def _clear_cookies(response: Response, request: Request) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(
            name,
            path="/",
            secure=cookie_secure(request.url.hostname),
            httponly=name == SESSION_COOKIE,
            samesite="lax",
        )


def _authenticate(db: Session, body: LoginIn) -> tuple[User, SessionToken]:
    user = db.scalar(select(User).where(User.email == normalize_email(body.email)))
    # Verify even for an unknown user (against a dummy hash) so both take similar time.
    if not verify_password(user.password_hash if user else None, body.password):
        raise _login_failed()
    if user is None or not user.is_active:
        raise _login_failed()
    session = issue_token(user.id, user.role)
    user.last_login_at = utcnow()
    db.commit()
    return user, session


@router.post(
    "/login",
    response_model=UserOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": LoginIn.model_json_schema()}},
        }
    },
)
async def login(
    request: Request, response: Response, db: Annotated[Session, Depends(get_db)]
) -> UserOut:
    # The body is parsed here, not by FastAPI, so a bad body gets the generic message and a
    # validation error can never echo the password back.
    try:
        body = LoginIn.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise _login_failed() from None
    if len(body.password) > PASSWORD_MAX_LENGTH:
        raise _login_failed()
    # argon2 and the database block, so they run off the event loop.
    user, session = await run_in_threadpool(_authenticate, db, body)

    max_age = get_settings().jwt_ttl_minutes * 60
    _set_cookie(response, request, SESSION_COOKIE, session.token, max_age)
    _set_cookie(response, request, CSRF_COOKIE, new_csrf_token(), max_age)
    return UserOut.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: Annotated[Session, Depends(get_db)]) -> None:
    """Revoke the session until it would expire, and clear both cookies.

    Safe to repeat: a missing, expired or already revoked session still succeeds.
    """
    token = request.cookies.get(SESSION_COOKIE)
    claims = decode_token(token) if token else None
    now = utcnow()
    if claims is not None:
        jti = str(claims["jti"])
        if db.get(RevokedToken, jti) is None:
            db.add(RevokedToken(jti=jti, expires_at=_naive_utc(claims["exp"])))
    # Keep the table small: a token past its expiry is rejected anyway.
    db.execute(delete(RevokedToken).where(RevokedToken.expires_at < now))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent logout of the same session already revoked it.
        db.rollback()
    _clear_cookies(response, request)


@router.get("/me", response_model=UserOut)
def me(user: Annotated[User, Depends(current_user)]) -> UserOut:
    return UserOut.model_validate(user)
