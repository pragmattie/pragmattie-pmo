"""CSRF on every unsafe route of the real app (item 2.4).

The routes come from the app itself, so a new unsafe route is covered as soon as it is added.
Each request carries a valid admin session, so only the CSRF token is wrong.
"""

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from test_authz import GUARDED, SAMPLES

from app.api.auth import LOGIN_PATH, LOGOUT_PATH, SAFE_METHODS
from app.authz import PERMISSIONS, Role
from app.main import app
from app.models import AuditDecision
from app.security import CSRF_COOKIE, CSRF_HEADER

# The CSRF value `sign_in` (tests/conftest.py) sets as both cookie and header.
CSRF_VALUE = "test-csrf-value"

# Login has no session to protect, and is covered by its JSON-only rule (test_auth.py).
EXEMPT = {("POST", LOGIN_PATH)}


def _unsafe_routes() -> list[tuple[str, str]]:
    rows = []
    for route in iter_route_contexts(app.routes):
        if isinstance(route.original_route, APIRoute):
            for method in sorted(route.methods - SAFE_METHODS):
                if (method, route.path) not in EXEMPT:
                    rows.append((method, route.path))
    return rows


UNSAFE = _unsafe_routes()


def _send(web, method: str, path: str, target_id: int, headers: dict[str, str]):
    sample = SAMPLES.get((method, path))
    request = sample(target_id) if sample else {"url": path}
    return web.request(method, headers=headers, **request)


def test_the_unsafe_routes_are_all_found():
    assert {
        ("POST", LOGOUT_PATH),
        ("PATCH", "/api/v1/users/{user_id}/role"),
        ("POST", "/api/v1/users/{user_id}/unlock"),
        ("POST", "/api/v1/projects/{project_id}/risks"),
        ("PATCH", "/api/v1/projects/{project_id}/risks/{risk_id}"),
        ("POST", "/api/v1/projects/{project_id}/risks/from-library"),
    } <= set(UNSAFE)


BAD_TOKENS = {
    "missing": None,
    "mismatched": "a-different-csrf-value",
    "empty": "",
    "case-changed": CSRF_VALUE.upper(),
    "prefix": CSRF_VALUE[:-1],
}


@pytest.mark.parametrize("bad", list(BAD_TOKENS), ids=list(BAD_TOKENS))
@pytest.mark.parametrize(("method", "path"), UNSAFE, ids=[f"{m} {p}" for m, p in UNSAFE])
def test_a_bad_csrf_header_is_refused(web, db, sign_in, target, method, path, bad):
    sign_in(web, db, "admin")
    del web.headers[CSRF_HEADER]
    value = BAD_TOKENS[bad]
    headers = {} if value is None else {CSRF_HEADER: value}
    response = _send(web, method, path, target.id, headers)
    assert response.status_code == 403, response.text
    db.expire_all()
    assert db.query(AuditDecision).count() == 0


@pytest.mark.parametrize(("method", "path"), UNSAFE, ids=[f"{m} {p}" for m, p in UNSAFE])
def test_a_header_without_its_cookie_is_refused(web, db, sign_in, target, method, path):
    # The header alone proves nothing: another site can set a header but can't read our cookie.
    sign_in(web, db, "admin")
    web.cookies.delete(CSRF_COOKIE)
    assert _send(web, method, path, target.id, {}).status_code == 403


@pytest.mark.parametrize(("method", "path"), UNSAFE, ids=[f"{m} {p}" for m, p in UNSAFE])
def test_an_empty_cookie_and_empty_header_are_refused(web, db, sign_in, target, method, path):
    sign_in(web, db, "admin")
    web.cookies.set(CSRF_COOKIE, "")
    assert _send(web, method, path, target.id, {CSRF_HEADER: ""}).status_code == 403


@pytest.mark.parametrize(("method", "path"), UNSAFE, ids=[f"{m} {p}" for m, p in UNSAFE])
def test_a_matching_token_is_accepted(web, db, sign_in, target, method, path):
    # Admin, unless the route needs an action admin lacks: then the first role that has it.
    guard = {(m, p): g for m, p, g in GUARDED}.get((method, path))
    sign_in(web, db, next(r for r in (Role.ADMIN, *Role) if guard in (None, *PERMISSIONS[r])))
    response = _send(web, method, path, target.id, {CSRF_HEADER: CSRF_VALUE})
    assert response.status_code in (200, 201, 204), response.text
