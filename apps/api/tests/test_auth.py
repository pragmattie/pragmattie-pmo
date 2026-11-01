import logging
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.api.auth as auth_api
from app.api.auth import LOGIN_FAILED
from app.config import get_settings
from app.db import engine
from app.main import app
from app.models import Base, RevokedToken, User
from app.schemas.auth import PASSWORD_MAX_LENGTH
from app.security import CSRF_COOKIE, SESSION_COOKIE, hash_password, issue_token

# Made up for these tests only; never a real credential.
PASSWORD = "correct-horse-battery-staple-test"
EMAIL = "ada.reviewer@pmo.example"
LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


@pytest.fixture
def user(db) -> User:
    row = User(
        email="Ada.Reviewer@PMO.example",
        display_name="Ada Reviewer",
        password_hash=hash_password(PASSWORD),
        role="pm",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def web(db) -> TestClient:
    # COOKIE_SECURE is off in tests (conftest.py), so the client sends cookies back over http.
    return TestClient(app, base_url="http://localhost")


def _login(web: TestClient, email: str = EMAIL, password: str = PASSWORD):
    return web.post(LOGIN, json={"email": email, "password": password})


def _csrf(web: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": web.cookies[CSRF_COOKIE]}


def _set_cookies(response) -> dict[str, str]:
    return {h.split("=", 1)[0]: h for h in response.headers.get_list("set-cookie")}


def test_password_is_hashed_with_argon2id(user):
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash


def test_correct_login_sets_cookies_and_returns_the_user(web, user):
    response = _login(web)
    assert response.status_code == 200
    assert response.json() == {
        "id": user.id,
        "email": EMAIL,
        "display_name": "Ada Reviewer",
        "role": "pm",
    }
    cookies = _set_cookies(response)
    session = cookies[SESSION_COOKIE].lower()
    assert "httponly" in session and "samesite=lax" in session and "max-age=1800" in session
    assert "secure" not in session  # COOKIE_SECURE is off in tests
    csrf = cookies[CSRF_COOKIE].lower()
    assert "httponly" not in csrf and "samesite=lax" in csrf

    me = web.get(ME)
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL


def test_token_claims(web, user):
    _login(web)
    claims = jwt.decode(
        web.cookies[SESSION_COOKIE], get_settings().jwt_secret, algorithms=["HS256"]
    )
    assert set(claims) == {"sub", "role", "iat", "exp", "jti"}
    assert claims["sub"] == str(user.id) and claims["role"] == "pm"
    assert claims["exp"] - claims["iat"] == 30 * 60


@pytest.mark.parametrize("base_url", ["http://localhost", "http://api:8000", "http://pmo.example"])
def test_cookie_secure_on_marks_cookies_secure_whatever_the_host(db, user, monkeypatch, base_url):
    monkeypatch.setattr(get_settings(), "cookie_secure", True)
    response = _login(TestClient(app, base_url=base_url))
    assert response.status_code == 200
    for header in _set_cookies(response).values():
        assert "secure" in header.lower()
    # The client won't send Secure cookies over http, so send them by hand to sign out.
    session, csrf = response.cookies[SESSION_COOKIE], response.cookies[CSRF_COOKIE]
    cookie = f"{SESSION_COOKIE}={session}; {CSRF_COOKIE}={csrf}"
    logout = TestClient(app, base_url=base_url).post(
        LOGOUT, headers={"cookie": cookie, "X-CSRF-Token": csrf}
    )
    assert logout.status_code == 204
    for header in _set_cookies(logout).values():
        assert "secure" in header.lower()


@pytest.mark.parametrize("base_url", ["http://localhost", "http://pmo.example"])
def test_cookie_secure_off_leaves_cookies_plain_whatever_the_host(db, user, monkeypatch, base_url):
    monkeypatch.setattr(get_settings(), "cookie_secure", False)
    response = _login(TestClient(app, base_url=base_url))
    assert response.status_code == 200
    for header in _set_cookies(response).values():
        assert "secure" not in header.lower()


def test_last_login_is_recorded(web, user, db):
    _login(web)
    db.refresh(user)
    assert user.last_login_at is not None


def test_email_is_stored_and_compared_in_lower_case(web, user):
    assert user.email == EMAIL
    assert _login(web, email="  ADA.Reviewer@pmo.EXAMPLE ").status_code == 200


def test_wrong_password_and_unknown_user_get_the_same_response(web, user):
    wrong = _login(web, password="not-the-password")
    unknown = _login(web, email="nobody@pmo.example")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": LOGIN_FAILED}
    assert SESSION_COOKIE not in web.cookies


def test_unknown_user_is_still_verified_against_a_hash(web, user, monkeypatch):
    calls = []
    real = auth_api.verify_password

    def spy(password_hash, password):
        calls.append(password_hash)
        return real(password_hash, password)

    monkeypatch.setattr(auth_api, "verify_password", spy)
    _login(web, email="nobody@pmo.example")
    assert calls == [None]


def test_inactive_user_cannot_log_in(web, user, db):
    user.is_active = False
    db.commit()
    response = _login(web)
    assert response.status_code == 401
    assert response.json() == {"detail": LOGIN_FAILED}


def test_deactivation_takes_effect_at_once(web, user, db):
    _login(web)
    user.is_active = False
    db.commit()
    assert web.get(ME).status_code == 401


def test_role_comes_from_the_database_not_the_token(web, user, db):
    _login(web)
    user.role = "sponsor"
    db.commit()
    assert web.get(ME).json()["role"] == "sponsor"


def test_too_long_password_is_refused_with_the_generic_message(web, user, monkeypatch):
    monkeypatch.setattr(auth_api, "verify_password", lambda *_: pytest.fail("argon2 ran"))
    response = _login(web, password="x" * (PASSWORD_MAX_LENGTH + 1))
    assert response.status_code == 401
    assert response.json() == {"detail": LOGIN_FAILED}


def test_password_at_the_cap_is_checked_normally(web, db):
    password = "p" * PASSWORD_MAX_LENGTH
    db.add(User(email=EMAIL, display_name="Ada", password_hash=hash_password(password), role="pm"))
    db.commit()
    assert _login(web, password=password).status_code == 200


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"email": "ada.reviewer@pmo.example"}', b'{"email": 1, "password": 123456}'],
)
def test_malformed_login_body_gets_the_generic_message(web, user, body):
    response = web.post(LOGIN, content=body, headers={"content-type": "application/json"})
    assert response.status_code == 401
    assert response.json() == {"detail": LOGIN_FAILED}
    assert "123456" not in response.text


@pytest.mark.parametrize(
    "content_type",
    [None, "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "text/plain"],
)
def test_login_without_a_json_content_type_gets_the_generic_401(web, user, content_type):
    # A form post from another site must not sign the visitor in, even with a JSON-looking body.
    body = f'{{"email": "{EMAIL}", "password": "{PASSWORD}"}}'.encode()
    headers = {"content-type": content_type} if content_type else {}
    response = web.post(LOGIN, content=body, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": LOGIN_FAILED}
    assert SESSION_COOKIE not in _set_cookies(response)


def test_login_accepts_json_with_a_charset(web, user):
    body = f'{{"email": "{EMAIL}", "password": "{PASSWORD}"}}'.encode()
    response = web.post(
        LOGIN, content=body, headers={"content-type": "Application/JSON; charset=utf-8"}
    )
    assert response.status_code == 200


def test_me_without_a_session_is_401(web, user):
    assert web.get(ME).status_code == 401


def test_expired_token_is_rejected(web, user):
    old = issue_token(user.id, user.role, now=datetime.now(UTC) - timedelta(minutes=31))
    web.cookies.set(SESSION_COOKIE, old.token)
    assert web.get(ME).status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        jwt.encode({"sub": "1", "role": "pm"}, "x" * 32, algorithm="HS256"),
        "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiIxIn0.",
    ],
)
def test_malformed_or_foreign_tokens_are_rejected(web, user, token):
    web.cookies.set(SESSION_COOKIE, token)
    assert web.get(ME).status_code == 401


def test_logout_revokes_the_token_and_clears_cookies(web, user, db):
    _login(web)
    token = web.cookies[SESSION_COOKIE]
    response = web.post(LOGOUT, headers=_csrf(web))
    assert response.status_code == 204
    cleared = _set_cookies(response)
    assert set(cleared) == {SESSION_COOKIE, CSRF_COOKIE}
    assert all("max-age=0" in header.lower() for header in cleared.values())
    assert SESSION_COOKIE not in web.cookies

    # The revoked token no longer works, even though it has not expired.
    web.cookies.set(SESSION_COOKIE, token)
    assert web.get(ME).status_code == 401
    assert db.scalar(select(RevokedToken)) is not None


def test_logout_deletes_expired_revocations(web, user, db):
    db.add(RevokedToken(jti="old", expires_at=datetime(2000, 1, 1)))
    db.commit()
    _login(web)
    assert web.post(LOGOUT, headers=_csrf(web)).status_code == 204
    jtis = db.scalars(select(RevokedToken.jti)).all()
    assert "old" not in jtis and len(jtis) == 1


def test_logout_is_safe_to_repeat(web, user):
    assert web.post(LOGOUT).status_code == 204  # no session at all
    _login(web)
    headers = _csrf(web)
    token = web.cookies[SESSION_COOKIE]
    assert web.post(LOGOUT, headers=headers).status_code == 204
    # The same, already revoked session again.
    web.cookies.set(SESSION_COOKIE, token)
    web.cookies.set(CSRF_COOKIE, headers["X-CSRF-Token"])
    assert web.post(LOGOUT, headers=headers).status_code == 204


def test_logout_with_an_expired_session_succeeds(web, user):
    old = issue_token(user.id, user.role, now=datetime.now(UTC) - timedelta(minutes=31))
    web.cookies.set(SESSION_COOKIE, old.token)
    web.cookies.set(CSRF_COOKIE, "csrf-value")
    response = web.post(LOGOUT, headers={"X-CSRF-Token": "csrf-value"})
    assert response.status_code == 204
    assert SESSION_COOKIE in _set_cookies(response)


@pytest.mark.parametrize("headers", [{}, {"X-CSRF-Token": "wrong"}])
def test_post_without_a_matching_csrf_header_is_403(web, user, headers):
    _login(web)
    response = web.post(LOGOUT, headers=headers)
    assert response.status_code == 403
    # Still signed in: the forged request did nothing.
    assert web.get(ME).status_code == 200


def test_csrf_applies_to_every_unsafe_method(db, user):
    # The same router-wide dependency as app/main.py, on routes for every method.
    probe = FastAPI()
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(auth_api.require_csrf)])
    api.include_router(auth_api.router)
    api.add_api_route(
        "/probe", lambda: {"ok": True}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    probe.include_router(api)
    web = TestClient(probe, base_url="http://localhost")
    _login(web)
    assert web.get("/api/v1/probe").status_code == 200
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert web.request(method, "/api/v1/probe").status_code == 403
        assert web.request(method, "/api/v1/probe", headers=_csrf(web)).status_code == 200


def test_no_password_or_token_in_logs_or_responses(web, user, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = []
    bodies.append(_login(web, password="wrong-password-value").text)
    bodies.append(_login(web, email="nobody@pmo.example").text)
    ok = _login(web)
    bodies.append(ok.text)
    token = web.cookies[SESSION_COOKIE]
    csrf = web.cookies[CSRF_COOKIE]
    bodies.append(web.get(ME).text)
    logout = web.post(LOGOUT, headers={"X-CSRF-Token": csrf})
    bodies.append(logout.text)

    secrets = (PASSWORD, "wrong-password-value", token, csrf, user.password_hash)
    for secret in secrets:
        assert secret not in caplog.text
        for body in bodies:
            assert secret not in body
    assert "password" not in ok.json() and "password_hash" not in ok.text
    assert PASSWORD not in repr(user)
    assert user.password_hash not in repr(user)
