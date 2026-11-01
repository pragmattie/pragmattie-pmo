"""Password hashing, session tokens and the CSRF check.

Passwords are hashed with argon2id using the cost parameters in settings; plaintext is never
stored or logged. Session tokens are HS256 JWTs signed with JWT_SECRET. Nothing here logs a
password, a hash or a token.
"""

import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

JWT_ALGORITHM = "HS256"
SESSION_COOKIE = "pmo_session"
CSRF_COOKIE = "pmo_csrf"
CSRF_HEADER = "X-CSRF-Token"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _params() -> tuple[int, int, int]:
    settings = get_settings()
    return settings.argon2_time_cost, settings.argon2_memory_cost, settings.argon2_parallelism


@lru_cache
def _hasher(time_cost: int, memory_cost: int, parallelism: int) -> PasswordHasher:
    return PasswordHasher(
        time_cost=time_cost, memory_cost=memory_cost, parallelism=parallelism, type=Type.ID
    )


@lru_cache
def _dummy_hash(time_cost: int, memory_cost: int, parallelism: int) -> str:
    # A hash of a random value nobody knows, made once per process with the same parameters.
    return _hasher(time_cost, memory_cost, parallelism).hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _hasher(*_params()).hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """True if `password` matches `password_hash`. argon2 compares in constant time.

    With no hash (an unknown user), a dummy hash is checked instead so the two failures take
    about the same time.
    """
    params = _params()
    try:
        matched = _hasher(*params).verify(password_hash or _dummy_hash(*params), password)
    except (VerificationError, InvalidHashError):
        return False
    return matched and password_hash is not None


@dataclass(frozen=True)
class SessionToken:
    token: str
    jti: str
    expires_at: datetime


def issue_token(user_id: int, role: str, now: datetime | None = None) -> SessionToken:
    settings = get_settings()
    now = now or datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.jwt_ttl_minutes)
    jti = uuid.uuid4().hex
    claims = {"sub": str(user_id), "role": role, "iat": now, "exp": expires_at, "jti": jti}
    token = jwt.encode(claims, settings.jwt_secret or "", algorithm=JWT_ALGORITHM)
    return SessionToken(token=token, jti=jti, expires_at=expires_at)


def decode_token(token: str) -> dict | None:
    """The token's claims, or None if it is malformed, badly signed or expired."""
    try:
        return jwt.decode(
            token,
            get_settings().jwt_secret or "",
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "role", "iat", "exp", "jti"]},
        )
    except jwt.PyJWTError:
        return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_value: str | None, header_value: str | None) -> bool:
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value.encode(), header_value.encode())


def cookie_secure(host: str | None) -> bool:
    """Cookies are Secure everywhere except when the API is reached on localhost."""
    return (host or "").lower() not in LOCAL_HOSTS
