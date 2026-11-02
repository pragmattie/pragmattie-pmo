"""API security headers on every response, errors included (item 2.4).

The web build's CSP is checked by Vitest (`apps/web/src/__tests__/csp.spec.ts`).
"""

import pytest
from fastapi.testclient import TestClient

from app.api.auth import login_limiter
from app.config import get_settings
from app.headers import SECURITY_HEADERS
from app.main import app

EXPECTED = {"x-content-type-options": "nosniff", "referrer-policy": "no-referrer"}


def _assert_headers(response) -> None:
    for name, value in EXPECTED.items():
        assert response.headers.get(name) == value, (response.status_code, name)
        # Exactly once, not appended to a value a route set.
        assert len(response.headers.get_list(name)) == 1


def test_the_header_values_are_the_agreed_ones():
    assert {k.lower(): v for k, v in SECURITY_HEADERS.items()} == EXPECTED


def test_200(web, db, sign_in):
    sign_in(web, db, "auditor")
    response = web.get("/api/v1/clients")
    assert response.status_code == 200
    _assert_headers(response)


def test_health(web):
    response = web.get("/api/v1/health")
    _assert_headers(response)


def test_204(web, db, sign_in):
    sign_in(web, db, "auditor")
    response = web.post("/api/v1/auth/logout")
    assert response.status_code == 204
    _assert_headers(response)


def test_401(web, db):
    response = web.get("/api/v1/auth/me")
    assert response.status_code == 401
    _assert_headers(response)


def test_401_failed_login(web, db):
    response = web.post("/api/v1/auth/login", json={"email": "a@pmo.example", "password": "x"})
    assert response.status_code == 401
    _assert_headers(response)


def test_403(web, db, sign_in):
    sign_in(web, db, "auditor")
    response = web.patch("/api/v1/users/1/role", json={"role": "admin"})
    assert response.status_code == 403
    _assert_headers(response)


def test_404(web):
    response = web.get("/api/v1/no-such-route")
    assert response.status_code == 404
    _assert_headers(response)


def test_413(web):
    body = b"x" * (get_settings().max_body_bytes + 1)
    response = web.post("/api/v1/auth/login", content=body)
    assert response.status_code == 413
    _assert_headers(response)


def test_422(web, db, sign_in):
    sign_in(web, db, "auditor")
    response = web.get("/api/v1/clients", params={"limit": "lots"})
    assert response.status_code == 422
    _assert_headers(response)


def test_429(web, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_per_minute", 1)
    login_limiter.reset()
    body = {"email": "a@pmo.example", "password": "x"}
    web.post("/api/v1/auth/login", json=body)
    response = web.post("/api/v1/auth/login", json=body)
    assert response.status_code == 429
    _assert_headers(response)


def test_409_lock_conflict(web, db, sign_in, target, monkeypatch):
    # A deadlock reported by MySQL is a clean 409, with the headers (see test_mysql_races.py).
    from sqlalchemy.exc import OperationalError

    import app.api.users as users_api

    class Deadlock(Exception):
        args = (1213, "Deadlock found when trying to get lock")

    def deadlock(_db):
        raise OperationalError("SELECT ... FOR UPDATE", {}, Deadlock())

    sign_in(web, db, "admin")
    admin_two = sign_in(TestClient(app), db, "admin", email="admin.two@pmo.example")
    monkeypatch.setattr(users_api, "_active_admin_ids", deadlock)
    response = web.patch(f"/api/v1/users/{admin_two.id}/role", json={"role": "auditor"})
    assert response.status_code == 409
    _assert_headers(response)


def test_another_database_error_is_still_a_500(web, db, sign_in, target, monkeypatch):
    from sqlalchemy.exc import OperationalError

    import app.api.users as users_api

    class Gone(Exception):
        args = (2006, "MySQL server has gone away")

    def gone(_db):
        raise OperationalError("SELECT 1", {}, Gone())

    sign_in(web, db, "admin")
    admin_two = sign_in(TestClient(app), db, "admin", email="admin.two@pmo.example")
    monkeypatch.setattr(users_api, "_active_admin_ids", gone)
    web = TestClient(app, raise_server_exceptions=False, cookies=web.cookies, headers=web.headers)
    assert (
        web.patch(f"/api/v1/users/{admin_two.id}/role", json={"role": "auditor"}).status_code == 500
    )


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs(web, path):
    _assert_headers(web.get(path))


def test_cors_preflight(web):
    origin = get_settings().cors_origin_list[0]
    response = web.options(
        "/api/v1/auth/login",
        headers={"origin": origin, "access-control-request-method": "POST"},
    )
    assert response.status_code == 200
    _assert_headers(response)
