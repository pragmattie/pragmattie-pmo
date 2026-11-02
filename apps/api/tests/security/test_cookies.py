"""Session and CSRF cookie flags (item 2.4).

The session cookie is HttpOnly (page scripts can't read it), Secure and SameSite=Lax. The CSRF
cookie has the same flags except HttpOnly: the page must read it to echo it in the header.
"""

from http.cookies import SimpleCookie

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import User
from app.security import CSRF_COOKIE, SESSION_COOKIE, hash_password

# Made up for these tests only; never a real credential.
EMAIL = "cookie.check@pmo.example"
PASSWORD = "cookie-flags-test-passphrase"


@pytest.fixture
def secure_login(db, monkeypatch):
    """Sign in with Secure cookies on (the real default) and return the parsed Set-Cookie
    headers by cookie name."""
    monkeypatch.setattr(get_settings(), "cookie_secure", True)
    db.add(
        User(
            email=EMAIL,
            display_name="Cookie Check",
            password_hash=hash_password(PASSWORD),
            role="auditor",
        )
    )
    db.commit()
    response = TestClient(app).post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )
    assert response.status_code == 200
    return _parse(response)


def _parse(response) -> dict:
    cookies = {}
    for header in response.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        for name, morsel in jar.items():
            cookies[name] = morsel
    return cookies


def test_cookie_secure_is_on_by_default():
    field = type(get_settings()).model_fields["cookie_secure"]
    assert field.default is True


def test_only_the_two_cookies_are_set(secure_login):
    assert set(secure_login) == {SESSION_COOKIE, CSRF_COOKIE}


def test_session_cookie_is_httponly_secure_and_lax(secure_login):
    session = secure_login[SESSION_COOKIE]
    assert session["httponly"] is True
    assert session["secure"] is True
    assert session["samesite"].lower() == "lax"
    assert session["path"] == "/"
    assert int(session["max-age"]) == get_settings().jwt_ttl_minutes * 60


def test_csrf_cookie_is_readable_by_the_page_but_secure_and_lax(secure_login):
    csrf = secure_login[CSRF_COOKIE]
    assert not csrf["httponly"]
    assert csrf["secure"] is True
    assert csrf["samesite"].lower() == "lax"
    # Long and random enough that another site can't guess it.
    assert len(csrf.value) >= 32
    assert csrf.value != secure_login[SESSION_COOKIE].value


def test_each_login_gets_a_fresh_csrf_value(db, monkeypatch, secure_login):
    second = TestClient(app).post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert _parse(second)[CSRF_COOKIE].value != secure_login[CSRF_COOKIE].value


def test_logout_clears_both_cookies_with_the_same_flags(db, monkeypatch, secure_login):
    session, csrf = secure_login[SESSION_COOKIE].value, secure_login[CSRF_COOKIE].value
    response = TestClient(app).post(
        "/api/v1/auth/logout",
        headers={
            "cookie": f"{SESSION_COOKIE}={session}; {CSRF_COOKIE}={csrf}",
            "X-CSRF-Token": csrf,
        },
    )
    assert response.status_code == 204
    cleared = _parse(response)
    assert set(cleared) == {SESSION_COOKIE, CSRF_COOKIE}
    for name, morsel in cleared.items():
        assert morsel["max-age"] == "0"
        assert morsel["secure"] is True
        assert morsel["samesite"].lower() == "lax"
        assert bool(morsel["httponly"]) is (name == SESSION_COOKIE)


def test_a_failed_login_sets_no_cookie(db, monkeypatch, secure_login):
    response = TestClient(app).post(
        "/api/v1/auth/login", json={"email": EMAIL, "password": "not-the-password"}
    )
    assert response.status_code == 401
    assert response.headers.get_list("set-cookie") == []
