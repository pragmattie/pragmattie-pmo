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
from app.models import AuditDecision, Base, RevokedToken, User
from app.ratelimit import SlidingWindowLimiter
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
        role="project_manager",
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
        "role": "project_manager",
        "actions": [
            "project:read",
            "project:write",
            "report:publish",
            "risk:accept",
            "standards:read",
        ],
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
    assert claims["sub"] == str(user.id) and claims["role"] == "project_manager"
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
    password_hash = hash_password(password)
    db.add(
        User(email=EMAIL, display_name="Ada", password_hash=password_hash, role="project_manager")
    )
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


# --- Audit, lockout and rate limiting (item 2.3)

START = datetime(2026, 11, 1, 9, 0, 0)


class Clock:
    """A frozen `utcnow` for the auth module; advance it by hand."""

    def __init__(self, monkeypatch):
        self.now = START
        monkeypatch.setattr(auth_api, "utcnow", lambda: self.now)

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    return Clock(monkeypatch)


def _rows(db) -> list[AuditDecision]:
    db.expire_all()
    return list(db.scalars(select(AuditDecision).order_by(AuditDecision.id)))


def _events(db) -> list[tuple[str, str | None]]:
    return [(r.signals["event"], r.signals.get("reason")) for r in _rows(db)]


def _fail_times(web, n: int, password: str = "wrong-password-value"):
    return [_login(web, password=password).status_code for _ in range(n)]


def test_lockout_engages_on_the_fifth_failure(web, user, db, clock):
    assert _fail_times(web, 4) == [401] * 4
    db.refresh(user)
    assert user.failed_logins == 4 and user.locked_until is None
    assert _fail_times(web, 1) == [401]
    db.refresh(user)
    assert user.locked_until == START + timedelta(minutes=15)
    # Locked: even the correct password is refused, with the generic message.
    response = _login(web)
    assert response.status_code == 401
    assert response.json() == {"detail": LOGIN_FAILED}
    assert SESSION_COOKIE not in web.cookies
    assert _events(db)[-3:] == [
        ("login_failure", "bad_password"),
        ("lockout", "too_many_failures"),
        ("login_failure", "locked"),
    ]


def test_the_lock_expires_after_fifteen_minutes(web, user, db, clock):
    _fail_times(web, 5)
    clock.advance(minutes=14, seconds=59)
    assert _login(web).status_code == 401
    clock.advance(seconds=1)
    assert _login(web).status_code == 200
    db.refresh(user)
    assert user.locked_until is None and user.failed_logins == 0
    assert user.failed_window_started_at is None


def test_attempts_while_locked_neither_count_nor_extend_the_lock(web, user, db, clock):
    _fail_times(web, 5)
    for _ in range(4):
        clock.advance(minutes=3)
        assert _login(web, password="still-wrong").status_code == 401
    db.refresh(user)
    assert user.locked_until == START + timedelta(minutes=15)
    assert user.failed_logins == 0
    assert [e for e in _events(db) if e[0] == "lockout"] == [("lockout", "too_many_failures")]
    clock.advance(minutes=3)  # 15 minutes after the fifth failure
    assert _login(web).status_code == 200


def test_failures_outside_the_window_start_a_new_count(web, user, db, clock):
    _fail_times(web, 4)
    clock.advance(minutes=15)
    _fail_times(web, 1)
    db.refresh(user)
    assert user.failed_logins == 1 and user.locked_until is None
    assert user.failed_window_started_at == clock.now


def test_a_success_resets_the_count(web, user, db, clock):
    _fail_times(web, 4)
    assert _login(web).status_code == 200
    _fail_times(web, 4)
    db.refresh(user)
    assert user.failed_logins == 4 and user.locked_until is None


def test_a_locked_account_and_an_unknown_email_get_identical_responses(web, user, db, clock):
    _fail_times(web, 5)
    locked = _login(web)
    unknown = _login(web, email="nobody@pmo.example")
    assert locked.status_code == unknown.status_code == 401
    assert locked.content == unknown.content
    assert locked.headers.items() == unknown.headers.items()


def test_the_password_is_checked_before_the_lock(web, user, db, clock, monkeypatch):
    _fail_times(web, 5)
    calls = []
    real = auth_api.verify_password
    monkeypatch.setattr(auth_api, "verify_password", lambda h, p: calls.append(h) or real(h, p))
    _login(web)
    assert calls == [user.password_hash]


def test_rate_limit_returns_429_per_ip_and_writes_no_row(web, user, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_per_minute", 3)
    assert [_login(web).status_code for _ in range(3)] == [200, 200, 200]
    before = len(_rows(db))
    blocked = _login(web)
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": auth_api.TOO_MANY}
    assert blocked.headers["retry-after"] == "60"
    assert len(_rows(db)) == before
    # A forwarded-for header can't buy a fresh allowance.
    spoofed = web.post(
        LOGIN, json={"email": EMAIL, "password": PASSWORD}, headers={"X-Forwarded-For": "1.2.3.4"}
    )
    assert spoofed.status_code == 429


def test_rate_limit_runs_before_the_password_check(web, user, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_per_minute", 1)
    _login(web)
    monkeypatch.setattr(auth_api, "verify_password", lambda *_: pytest.fail("argon2 ran"))
    assert _login(web).status_code == 429


def test_limiter_slides_and_drops_expired_keys():
    now = [0.0]
    limiter = SlidingWindowLimiter(window_seconds=60, clock=lambda: now[0])
    assert [limiter.allow("a", 2) for _ in range(3)] == [True, True, False]
    now[0] = 30
    assert limiter.allow("b", 2)
    now[0] = 60.5  # a's hits have left the window; b's has not
    assert limiter.allow("a", 2)
    for i in range(100):
        limiter.allow(f"ip-{i}", 2)
    now[0] = 200
    limiter.allow("c", 2)
    assert len(limiter) == 1  # everything older than a window was swept


def _all_audit_text(db) -> str:
    return " ".join(
        repr((r.signals, r.output, r.action_taken, r.human_override, r.error)) for r in _rows(db)
    )


def test_each_event_type_writes_one_audit_row_without_secrets(web, user, db, clock):
    typed_email = "Someone.Else@pmo.example"
    assert _login(web, email=typed_email, password="typo-password-1").status_code == 401
    _fail_times(web, 5, password="typo-password-2")
    clock.advance(minutes=15)
    assert _login(web).status_code == 200
    token, csrf = web.cookies[SESSION_COOKIE], web.cookies[CSRF_COOKIE]
    assert web.post(LOGOUT, headers=_csrf(web)).status_code == 204

    rows = _rows(db)
    assert [(r.signals["event"], r.signals.get("reason")) for r in rows] == [
        ("login_failure", "unknown_user"),
        *[("login_failure", "bad_password")] * 5,
        ("lockout", "too_many_failures"),
        ("login_success", None),
        ("logout", None),
    ]
    for row in rows:
        assert (row.agent, row.trigger, row.subject_type) == ("auth", "human", "login")
        assert set(row.signals) <= {"event", "reason", "ip", "user_id"}
        assert row.signals["ip"] == "testclient"
    assert len({r.subject_id for r in rows}) == len(rows)
    assert rows[0].signals["user_id"] is None
    assert all(r.signals["user_id"] == user.id for r in rows[1:])
    # Only a person who proved who they are is the actor.
    assert [r.actor_user_id for r in rows] == [None] * 7 + [user.id, user.id]
    assert [r.status for r in rows][:2] == ["rejected", "rejected"]

    text = _all_audit_text(db)
    for secret in (
        PASSWORD,
        "typo-password-1",
        "typo-password-2",
        typed_email,
        typed_email.lower(),
        EMAIL,
        token,
        csrf,
        user.password_hash,
    ):
        assert secret not in text


def test_inactive_user_failure_is_audited(web, user, db):
    user.is_active = False
    db.commit()
    _login(web)
    assert _events(db) == [("login_failure", "inactive")]


@pytest.mark.parametrize("body", [b"not json", b'{"email": "ada.reviewer@pmo.example"}'])
def test_a_malformed_body_is_audited_without_its_content(web, user, db, body):
    web.post(LOGIN, content=body, headers={"content-type": "application/json"})
    (row,) = _rows(db)
    assert row.signals == {
        "event": "login_failure",
        "reason": "invalid_request",
        "ip": "testclient",
        "user_id": None,
    }


def test_a_failed_login_keeps_its_row_and_count(web, user):
    # Read through a fresh session: the request's own commit must have stored them.
    _login(web, password="wrong-password-value")
    with Session(engine) as fresh:
        assert fresh.scalar(select(User.failed_logins).where(User.id == user.id)) == 1
        assert len(fresh.scalars(select(AuditDecision)).all()) == 1


def test_a_repeat_logout_writes_no_row(web, user, db):
    _login(web)
    headers = _csrf(web)
    token = web.cookies[SESSION_COOKIE]
    web.post(LOGOUT, headers=headers)
    web.cookies.set(SESSION_COOKIE, token)
    web.cookies.set(CSRF_COOKIE, headers["X-CSRF-Token"])
    web.post(LOGOUT, headers=headers)
    web.post(LOGOUT)
    assert _events(db) == [("login_success", None), ("logout", None)]


def test_a_new_login_revokes_the_session_it_replaces(web, user, db):
    _login(web)
    old = web.cookies[SESSION_COOKIE]
    assert _login(web).status_code == 200
    assert web.cookies[SESSION_COOKIE] != old
    other = TestClient(app, base_url="http://localhost")
    other.cookies.set(SESSION_COOKIE, old)
    assert other.get(ME).status_code == 401
    assert web.get(ME).status_code == 200


def test_a_login_survives_a_parallel_revocation_of_the_old_session(web, user, db, monkeypatch):
    _login(web)
    old = jwt.decode(web.cookies[SESSION_COOKIE], options={"verify_signature": False})
    _fail_times(web, 2)
    # Another request revokes the same session first, but this login still sees it as live (as
    # two MySQL transactions can), so its own revocation hits the primary key.
    db.add(RevokedToken(jti=old["jti"], expires_at=datetime.fromtimestamp(old["exp"], UTC)))
    db.commit()
    real_get = Session.get

    def stale_get(self, entity, ident, *args, **kwargs):
        if entity is RevokedToken:
            return None
        return real_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(Session, "get", stale_get)
    response = _login(web)
    assert response.status_code == 200
    assert response.json()["email"] == EMAIL
    with Session(engine) as fresh:
        assert fresh.scalar(select(User.failed_logins).where(User.id == user.id)) == 0
        events = [r.signals["event"] for r in fresh.scalars(select(AuditDecision))]
    assert events.count("login_success") == 2


def test_the_counter_is_updated_from_a_locked_row(web, user, monkeypatch):
    statements = []
    real = Session.scalar

    def spy(self, statement, *args, **kwargs):
        statements.append(statement)
        return real(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalar", spy)
    _login(web, password="wrong-password-value")
    assert any(getattr(s, "_for_update_arg", None) is not None for s in statements)
