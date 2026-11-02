"""Role x route, again, with a forged role claim (item 2.4).

The 2.2 matrix (`tests/test_authz.py`) signs each role in honestly. Here every session token
claims a role other than the one in the database, signed with the real key, as if someone had
got a token minted with the wrong role. The API must go by the database row, never the claim.
"""

import pytest
from test_authz import GUARDED, _expected, _request

from app.authz import Role
from app.security import CSRF_COOKIE, SESSION_COOKIE, issue_token

# The CSRF value `sign_in` (tests/conftest.py) sets as both cookie and header.
CSRF_VALUE = "test-csrf-value"

ROLES = list(Role)


def _forged_session(web, db, sign_in, real: Role, claimed: Role) -> None:
    user = sign_in(web, db, real)
    web.cookies.set(SESSION_COOKIE, issue_token(user.id, claimed).token)
    web.cookies.set(CSRF_COOKIE, CSRF_VALUE)


@pytest.mark.parametrize("real", ROLES)
@pytest.mark.parametrize(
    ("method", "path", "guard"), GUARDED, ids=[f"{m} {p}" for m, p, _ in GUARDED]
)
def test_a_claimed_admin_role_grants_nothing(web, db, sign_in, target, real, method, path, guard):
    _forged_session(web, db, sign_in, real, Role.ADMIN)
    response = _request(web, method, path, target.id)
    assert response.status_code == _expected(guard, real, method, path), response.text


@pytest.mark.parametrize(
    ("method", "path", "guard"), GUARDED, ids=[f"{m} {p}" for m, p, _ in GUARDED]
)
def test_a_claimed_lesser_role_takes_nothing_away(web, db, sign_in, target, method, path, guard):
    # The other direction: an admin whose token says auditor still has admin rights.
    _forged_session(web, db, sign_in, Role.ADMIN, Role.AUDITOR)
    response = _request(web, method, path, target.id)
    assert response.status_code == _expected(guard, Role.ADMIN, method, path), response.text


@pytest.mark.parametrize("claimed", ["superuser", "", "admin "])
def test_an_unknown_claimed_role_is_ignored(web, db, sign_in, claimed):
    user = sign_in(web, db, Role.AUDITOR)
    web.cookies.set(SESSION_COOKIE, issue_token(user.id, claimed).token)
    body = web.get("/api/v1/auth/me").json()
    assert body["role"] == "auditor"
    assert web.patch(f"/api/v1/users/{user.id}/role", json={"role": "admin"}).status_code == 403


def test_a_token_for_another_user_id_acts_as_that_user_only(web, db, sign_in, target):
    # A valid token names its user; the role is that user's own, whatever the token claims.
    sign_in(web, db, Role.ADMIN)
    web.cookies.set(SESSION_COOKIE, issue_token(target.id, Role.ADMIN).token)
    assert web.get("/api/v1/auth/me").json()["id"] == target.id
    assert web.get("/api/v1/audit").status_code == 403
