"""Session token tampering (item 2.4). Every forged, altered or stale token gets 401.

Each token below names a real, active admin, so a token that slipped through would get 200.
"""

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import get_settings
from app.models import RevokedToken
from app.security import JWT_ALGORITHM, SESSION_COOKIE, issue_token

ROUTES = ("/api/v1/auth/me", "/api/v1/audit")


def _b64(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _claims(user_id: int, **changes) -> dict:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "role": "admin",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=30)).timestamp()),
        "jti": "a" * 32,
    }
    claims.update(changes)
    return {k: v for k, v in claims.items() if v is not None}


def _signed(claims: dict, key: str | None = None, algorithm: str = JWT_ALGORITHM) -> str:
    return jwt.encode(claims, key or get_settings().jwt_secret, algorithm=algorithm)


def _alg_none(claims: dict) -> str:
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."


def _altered_payload(user_id: int, **changes) -> str:
    """A genuine token whose payload is swapped after signing, keeping the old signature."""
    header, _payload, signature = _signed(_claims(user_id, role="auditor")).split(".")
    return f"{header}.{_b64(_claims(user_id, **changes))}.{signature}"


FORGERIES = {
    "alg none": lambda uid: _alg_none(_claims(uid)),
    "alg None, capitalised": lambda uid: (
        f"{_b64({'alg': 'None', 'typ': 'JWT'})}.{_b64(_claims(uid))}."
    ),
    "wrong key": lambda uid: _signed(_claims(uid), key="w" * 48),
    "other algorithm, same key": lambda uid: _signed(_claims(uid), algorithm="HS512"),
    "expired": lambda uid: _signed(
        _claims(
            uid,
            iat=int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
            exp=int((datetime.now(UTC) - timedelta(seconds=1)).timestamp()),
        )
    ),
    "altered payload": lambda uid: _altered_payload(uid, role="admin"),
    "altered user id": lambda uid: _altered_payload(uid + 1),
    "missing sub": lambda uid: _signed(_claims(uid, sub=None)),
    "missing exp": lambda uid: _signed(_claims(uid, exp=None)),
    "missing jti": lambda uid: _signed(_claims(uid, jti=None)),
    "non-numeric sub": lambda uid: _signed(_claims(uid, sub="admin")),
    "sub of a user that does not exist": lambda uid: _signed(_claims(uid, sub="99999")),
    "signature stripped": lambda uid: _signed(_claims(uid)).rsplit(".", 1)[0] + ".",
    "empty": lambda uid: "",
    "garbage": lambda uid: "not.a.token",
}


@pytest.mark.parametrize("forgery", list(FORGERIES), ids=list(FORGERIES))
def test_a_tampered_token_is_401(web, db, sign_in, forgery):
    admin = sign_in(web, db, "admin")
    # The genuine token works, so a 401 below is down to the forgery alone.
    assert web.get(ROUTES[0]).status_code == 200
    web.cookies.set(SESSION_COOKIE, FORGERIES[forgery](admin.id))
    for route in ROUTES:
        assert web.get(route).status_code == 401, route


def test_a_token_expiring_now_is_401(web, db, sign_in):
    admin = sign_in(web, db, "admin")
    ttl = get_settings().jwt_ttl_minutes
    stale = issue_token(
        admin.id, "admin", now=datetime.now(UTC) - timedelta(minutes=ttl, seconds=1)
    )
    web.cookies.set(SESSION_COOKIE, stale.token)
    assert web.get(ROUTES[0]).status_code == 401


def test_a_revoked_token_is_401(web, db, sign_in):
    admin = sign_in(web, db, "admin")
    claims = jwt.decode(
        web.cookies[SESSION_COOKIE], get_settings().jwt_secret, algorithms=[JWT_ALGORITHM]
    )
    db.add(RevokedToken(jti=claims["jti"], expires_at=datetime(2999, 1, 1)))
    db.commit()
    assert web.get(ROUTES[0]).status_code == 401
    assert admin.id  # the user still exists and is active; only the token is refused
