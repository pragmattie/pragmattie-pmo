"""Oversized payloads (item 2.4).

A body over `max_body_bytes` gets 413 before any route runs, counted by the bytes received, so a
missing or false `Content-Length` doesn't get around it. Over-long fields inside a small body are
refused by the route itself.
"""

import pytest
from fastapi.testclient import TestClient

from app.api.auth import LOGIN_FAILED
from app.config import get_settings
from app.headers import TOO_LARGE
from app.models import AuditDecision
from app.schemas.auth import PASSWORD_MAX_LENGTH

LOGIN = "/api/v1/auth/login"
JSON = {"content-type": "application/json"}


def _cap() -> int:
    return get_settings().max_body_bytes


def _body(size: int) -> bytes:
    """A JSON login body of exactly `size` bytes."""
    head, tail = b'{"email":"a@pmo.example","password":"', b'"}'
    return head + b"x" * (size - len(head) - len(tail)) + tail


def _chunks(data: bytes, size: int = 4096):
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _audit_count(db) -> int:
    db.expire_all()
    return db.query(AuditDecision).count()


def test_the_default_cap_is_64_kib():
    assert type(get_settings()).model_fields["max_body_bytes"].default == 64 * 1024


def test_a_body_at_the_cap_reaches_the_route(web, db):
    response = web.post(LOGIN, content=_body(_cap()), headers=JSON)
    # The route ran: the over-long password gets the generic login failure.
    assert response.status_code == 401
    assert response.json()["detail"] == LOGIN_FAILED


def test_an_oversized_body_is_413(web, db):
    response = web.post(LOGIN, content=_body(_cap() + 1), headers=JSON)
    assert response.status_code == 413
    assert response.json() == {"detail": TOO_LARGE}
    # Refused before the route: no login failure was recorded.
    assert _audit_count(db) == 0


def test_an_oversized_body_without_content_length_is_413(web, db):
    request = web.build_request("POST", LOGIN, content=_chunks(_body(_cap() + 1)), headers=JSON)
    assert "content-length" not in request.headers
    assert request.headers["transfer-encoding"] == "chunked"
    response = web.send(request)
    assert response.status_code == 413
    assert _audit_count(db) == 0


def test_an_oversized_body_with_a_false_content_length_is_413(web, db):
    headers = {**JSON, "content-length": "64"}
    request = web.build_request("POST", LOGIN, content=_body(_cap() * 2), headers=headers)
    assert request.headers["content-length"] == "64"
    response = web.send(request)
    assert response.status_code == 413
    assert _audit_count(db) == 0


def test_a_declared_length_over_the_cap_is_413(web, db):
    headers = {**JSON, "content-length": str(_cap() + 1)}
    request = web.build_request("POST", LOGIN, content=b"{}", headers=headers)
    assert web.send(request).status_code == 413


def test_an_oversized_body_on_a_signed_in_route_is_413(web, db, sign_in, target):
    sign_in(web, db, "admin")
    padding = "x" * (_cap() + 1)
    response = web.patch(
        f"/api/v1/users/{target.id}/role", json={"role": "auditor", "padding": padding}
    )
    assert response.status_code == 413
    db.refresh(target)
    assert target.role == "project_manager"


def test_the_cap_comes_from_settings(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_body_bytes", 128)
    from app.main import create_app

    web = TestClient(create_app())
    assert web.post(LOGIN, content=_body(129), headers=JSON).status_code == 413
    assert web.post(LOGIN, content=_body(128), headers=JSON).status_code == 401


def test_an_over_long_password_gets_the_generic_401(web, db):
    # Login answers every bad body with the same 401 (test_auth.py), so a too-long password is
    # refused before argon2 runs without saying why.
    body = {"email": "a@pmo.example", "password": "x" * (PASSWORD_MAX_LENGTH + 1)}
    response = web.post(LOGIN, json=body)
    assert response.status_code == 401
    assert response.json()["detail"] == LOGIN_FAILED


@pytest.mark.parametrize(
    "body",
    [
        {"role": "x" * 10_000},
        {"role": ["admin"] * 1000},
        {"role": {"nested": {"deeper": {"deepest": "admin"}}}},
    ],
)
def test_an_over_long_or_odd_field_is_422(web, db, sign_in, target, body):
    sign_in(web, db, "admin")
    response = web.patch(f"/api/v1/users/{target.id}/role", json=body)
    assert response.status_code == 422
