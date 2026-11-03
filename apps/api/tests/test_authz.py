"""Roles, the permissions table and route guards (item 2.2).

The matrix is built from the app's own route list, so a new route is covered as soon as it is
added, and a route without a guard fails `test_every_route_has_a_guard`.
"""

from datetime import date, datetime

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.api.users as users_api
from app.authz import (
    OPEN_PATHS,
    PERMISSIONS,
    READ_ACTIONS,
    Action,
    Role,
    allowed_actions,
    not_own_submission,
    require,
)
from app.config import get_settings
from app.db import engine
from app.main import app, create_app
from app.models import AuditDecision, Base, Project, Risk, User
from app.security import CSRF_COOKIE, SESSION_COOKIE, issue_token

CSRF = {"X-CSRF-Token": "test-csrf-value"}


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


@pytest.fixture
def web(db) -> TestClient:
    return TestClient(app, base_url="http://localhost")


def _target(db: Session, role: str = "project_manager", email: str = "target@pmo.example") -> User:
    user = User(email=email, display_name="Target", password_hash="not-a-real-hash", role=role)
    db.add(user)
    db.commit()
    return user


# --- The table


def test_permissions_table_is_exactly_the_agreed_one():
    a = Action
    assert PERMISSIONS == {
        Role.ADMIN: {a.USER_ADMIN, a.AUDIT_READ, a.PROJECT_READ, a.STANDARDS_READ},
        Role.SPONSOR: {
            a.PROJECT_READ,
            a.INTAKE_DECIDE,
            a.GATE_DECIDE,
            a.RISK_ACCEPT,
            a.PROPOSAL_APPROVE,
            a.STANDARDS_READ,
        },
        Role.PROJECT_MANAGER: {
            a.PROJECT_READ,
            a.PROJECT_WRITE,
            a.RISK_ACCEPT,
            a.REPORT_PUBLISH,
            a.STANDARDS_READ,
        },
        Role.ETHICS_REVIEWER: {a.PROJECT_READ, a.GATE_DECIDE, a.AUDIT_READ, a.STANDARDS_READ},
        Role.AUDITOR: {a.PROJECT_READ, a.AUDIT_READ, a.STANDARDS_READ},
    }


def test_roles_and_actions_are_the_spec_values():
    assert [r.value for r in Role] == [
        "admin",
        "sponsor",
        "project_manager",
        "ethics_reviewer",
        "auditor",
    ]
    assert {a.value for a in Action} == {
        "project:read",
        "project:write",
        "intake:decide",
        "risk:accept",
        "gate:decide",
        "audit:read",
        "user:admin",
        "report:publish",
        "proposal:approve",
        "standards:read",
    }


def test_auditor_is_read_only_and_every_role_reads_standards():
    assert PERMISSIONS[Role.AUDITOR] <= READ_ACTIONS
    assert all(Action.STANDARDS_READ in actions for actions in PERMISSIONS.values())


def test_admin_holds_no_decision_rights():
    decisions = {
        Action.INTAKE_DECIDE,
        Action.GATE_DECIDE,
        Action.RISK_ACCEPT,
        Action.PROPOSAL_APPROVE,
        Action.REPORT_PUBLISH,
    }
    assert not PERMISSIONS[Role.ADMIN] & decisions


def test_allowed_actions_are_sorted_and_empty_for_an_unknown_role():
    assert allowed_actions("auditor") == ["audit:read", "project:read", "standards:read"]
    assert allowed_actions("pm") == []


def test_not_own_submission():
    not_own_submission(1, 2)
    not_own_submission(1, None)
    with pytest.raises(HTTPException) as caught:
        not_own_submission(3, 3)
    assert caught.value.status_code == 403


# --- Every route has a guard


def _dependency_calls(dependant) -> list:
    calls = []
    for dep in dependant.dependencies:
        calls.append(dep.call)
        calls += _dependency_calls(dep)
    return calls


def _routes(application: FastAPI) -> list:
    """Every route with its full path and effective dependencies (router-level ones included)."""
    return list(iter_route_contexts(application.routes))


def _guard(route):
    """The route's guard (an Action, or "signed_in"), or None."""
    for call in _dependency_calls(route.dependant):
        guard = getattr(call, "pmo_guard", None)
        if guard is not None:
            return guard
    return None


def unguarded_routes(application: FastAPI) -> list[str]:
    offenders = []
    for route in _routes(application):
        path = route.path or repr(route)
        if path in OPEN_PATHS:
            continue
        if not isinstance(route.original_route, APIRoute) or _guard(route) is None:
            offenders.append(path)
    return offenders


def test_every_route_has_a_guard():
    assert unguarded_routes(app) == []


def test_an_unguarded_route_fails_the_check():
    probe = create_app()
    probe.add_api_route("/api/v1/forgotten", lambda: {"ok": True})
    probe.add_api_route(
        "/api/v1/guarded",
        lambda: {"ok": True},
        dependencies=[Depends(require(Action.PROJECT_READ))],
    )
    assert unguarded_routes(probe) == ["/api/v1/forgotten"]


def test_open_paths_are_only_health_login_logout_and_docs():
    assert OPEN_PATHS == {
        "/api/v1/health",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/docs",
        "/redoc",
        "/openapi.json",
    }


@pytest.mark.parametrize(("env", "served"), [("dev", True), ("test", True), ("prod", False)])
def test_docs_are_served_only_in_dev_and_test(monkeypatch, env, served):
    monkeypatch.setattr(get_settings(), "app_env", env)
    web = TestClient(create_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (web.get(path).status_code == 200) is served, path
    assert web.get("/docs/oauth2-redirect").status_code == 404


# --- The matrix: every role x every guarded route


def _guarded_routes() -> list[tuple[str, str, object]]:
    rows = []
    for route in _routes(app):
        if isinstance(route.original_route, APIRoute) and route.path not in OPEN_PATHS:
            for method in sorted(route.methods):
                rows.append((method, route.path, _guard(route)))
    return rows


GUARDED = _guarded_routes()

RISK_BODY = {
    "kind": "threat",
    "title": "Model drift",
    "cause": "Training data ages",
    "effect": "Answers get worse",
    "category": "ai",
    "objectives": ["quality"],
    "probability": 3,
    "impact": 4,
    "response": "reduce",
    "response_plan": "Retrain every quarter",
}


def _project_with_risk(**changes) -> tuple[int, int]:
    """A project holding one risk, for the risk routes' samples."""
    with Session(engine) as session:
        project = Project(name="Matrix", stage="delivery", sponsor="S", pm="P", status="on_track")
        session.add(project)
        session.flush()
        values = {"status": "open", "source": "person", **RISK_BODY, **changes}
        risk = Risk(project_id=project.id, **values)
        session.add(risk)
        session.commit()
        return project.id, risk.id


def _risk_sample(item: bool, body: dict | None = None):
    def sample(_target: int) -> dict:
        project_id, risk_id = _project_with_risk()
        url = f"/api/v1/projects/{project_id}/risks" + (f"/{risk_id}" if item else "")
        return {"url": url, "json": body} if body is not None else {"url": url}

    return sample


# Routes with path parameters or a body need a sample request that succeeds when allowed. Each
# takes the id of a project-manager user that the caller may act on.
SAMPLES = {
    ("GET", "/api/v1/projects/{project_id}/risks"): _risk_sample(item=False),
    ("POST", "/api/v1/projects/{project_id}/risks"): _risk_sample(False, RISK_BODY),
    ("GET", "/api/v1/projects/{project_id}/risks/{risk_id}"): _risk_sample(item=True),
    ("PATCH", "/api/v1/projects/{project_id}/risks/{risk_id}"): _risk_sample(
        True, {"version": 1, "status": "closed"}
    ),
    ("GET", "/api/v1/projects/{project_id}/risks/heatmap"): lambda _target: {
        "url": f"/api/v1/projects/{_project_with_risk()[0]}/risks/heatmap",
    },
    ("GET", "/api/v1/projects/{project_id}/risks/{risk_id}/history"): lambda _target: {
        "url": "/api/v1/projects/{}/risks/{}/history".format(*_project_with_risk()),
    },
    ("POST", "/api/v1/projects/{project_id}/risks/from-library"): lambda _target: {
        "url": f"/api/v1/projects/{_project_with_risk()[0]}/risks/from-library",
        "json": {"template_ids": ["ai-model-drift", "project-scope-creep"]},
    },
    # A Low risk, so every role holding risk:accept may accept it.
    ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/accept"): lambda _target: {
        "url": "/api/v1/projects/{}/risks/{}/accept".format(
            *_project_with_risk(probability=1, impact=2, score=2, band="Low")
        ),
        "json": {
            "version": 1,
            "reason": "Small and well understood; we will watch it",
            "review_date": date.today().isoformat(),
        },
    },
    ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/open"): lambda _target: {
        "url": "/api/v1/projects/{}/risks/{}/open".format(*_project_with_risk(status="proposed")),
        "json": {"version": 1},
    },
    ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/reject"): lambda _target: {
        "url": "/api/v1/projects/{}/risks/{}/reject".format(*_project_with_risk(status="proposed")),
        "json": {"version": 1, "reason": "Already covered by another risk"},
    },
    ("PATCH", "/api/v1/users/{user_id}/role"): lambda target: {
        "url": f"/api/v1/users/{target}/role",
        "json": {"role": "auditor"},
    },
    ("POST", "/api/v1/users/{user_id}/unlock"): lambda target: {
        "url": f"/api/v1/users/{target}/unlock",
    },
}


def _request(web: TestClient, method: str, path: str, target: int):
    sample = SAMPLES.get((method, path))
    if sample is None:
        assert "{" not in path and method == "GET", f"add a sample request for {method} {path}"
        return web.request(method, path)
    return web.request(method, **sample(target))


# Allowed requests that create something answer 201 rather than 200.
CREATED = {
    ("POST", "/api/v1/projects/{project_id}/risks"),
    ("POST", "/api/v1/projects/{project_id}/risks/from-library"),
}


def _expected(guard, role: Role, method: str = "GET", path: str = "") -> int:
    if guard != "signed_in" and guard not in PERMISSIONS[role]:
        return 403
    return 201 if (method, path) in CREATED else 200


def test_matrix_covers_the_existing_routes():
    paths = {(method, path) for method, path, _ in GUARDED}
    assert {
        ("GET", "/api/v1/audit"),
        ("GET", "/api/v1/clients"),
        ("GET", "/api/v1/projects"),
        ("GET", "/api/v1/standards"),
        ("GET", "/api/v1/auth/me"),
        ("PATCH", "/api/v1/users/{user_id}/role"),
        ("POST", "/api/v1/users/{user_id}/unlock"),
        ("GET", "/api/v1/projects/{project_id}/risks"),
        ("POST", "/api/v1/projects/{project_id}/risks"),
        ("GET", "/api/v1/projects/{project_id}/risks/{risk_id}"),
        ("PATCH", "/api/v1/projects/{project_id}/risks/{risk_id}"),
        ("GET", "/api/v1/risk-library"),
        ("POST", "/api/v1/projects/{project_id}/risks/from-library"),
        ("GET", "/api/v1/projects/{project_id}/risks/heatmap"),
        ("GET", "/api/v1/projects/{project_id}/risks/{risk_id}/history"),
        ("GET", "/api/v1/users"),
        ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/accept"),
        ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/open"),
        ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/reject"),
        ("GET", "/api/v1/risks/escalations"),
    } <= paths
    guards = {path: guard for _, path, guard in GUARDED}
    assert guards["/api/v1/audit"] == Action.AUDIT_READ
    assert guards["/api/v1/clients"] == guards["/api/v1/projects"] == Action.PROJECT_READ
    assert guards["/api/v1/standards"] == Action.STANDARDS_READ
    assert guards["/api/v1/users/{user_id}/role"] == Action.USER_ADMIN
    assert guards["/api/v1/users/{user_id}/unlock"] == Action.USER_ADMIN
    assert guards["/api/v1/auth/me"] == "signed_in"
    by_method = {(method, path): guard for method, path, guard in GUARDED}
    risks, risk = (
        "/api/v1/projects/{project_id}/risks",
        "/api/v1/projects/{project_id}/risks/{risk_id}",
    )
    assert by_method[("GET", risks)] == by_method[("GET", risk)] == Action.PROJECT_READ
    assert by_method[("POST", risks)] == by_method[("PATCH", risk)] == Action.PROJECT_WRITE
    assert by_method[("GET", "/api/v1/risk-library")] == Action.PROJECT_READ
    assert by_method[("POST", f"{risks}/from-library")] == Action.PROJECT_WRITE
    assert by_method[("GET", f"{risks}/heatmap")] == Action.PROJECT_READ
    assert by_method[("GET", f"{risk}/history")] == Action.PROJECT_READ
    assert by_method[("GET", "/api/v1/users")] == Action.PROJECT_READ
    assert by_method[("POST", f"{risk}/accept")] == Action.RISK_ACCEPT
    assert by_method[("POST", f"{risk}/open")] == Action.PROJECT_WRITE
    assert by_method[("POST", f"{risk}/reject")] == Action.PROJECT_WRITE
    assert by_method[("GET", "/api/v1/risks/escalations")] == Action.PROJECT_READ


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize(
    ("method", "path", "guard"), GUARDED, ids=[f"{m} {p}" for m, p, _ in GUARDED]
)
def test_matrix(web, db, sign_in, role, method, path, guard):
    target = _target(db)
    sign_in(web, db, role)
    response = _request(web, method, path, target.id)
    assert response.status_code == _expected(guard, role, method, path), response.text


@pytest.mark.parametrize(
    ("method", "path", "guard"), GUARDED, ids=[f"{m} {p}" for m, p, _ in GUARDED]
)
def test_matrix_without_a_session_is_401(web, db, method, path, guard):
    target = _target(db)
    # A valid CSRF pair, so only the missing session is wrong.
    web.cookies.set(CSRF_COOKIE, CSRF["X-CSRF-Token"])
    web.headers.update(CSRF)
    assert _request(web, method, path, target.id).status_code == 401


def test_a_deactivated_user_is_refused_everywhere(web, db, sign_in):
    user = sign_in(web, db, "admin")
    user.is_active = False
    db.commit()
    for method, path, _ in GUARDED:
        assert _request(web, method, path, user.id).status_code == 401, path


def test_me_lists_the_roles_actions(web, db, sign_in):
    sign_in(web, db, "ethics_reviewer")
    body = web.get("/api/v1/auth/me").json()
    assert body["role"] == "ethics_reviewer"
    assert body["actions"] == ["audit:read", "gate:decide", "project:read", "standards:read"]


# --- Changing a role


def _role_change(web: TestClient, user_id: int, role: str):
    return web.patch(f"/api/v1/users/{user_id}/role", json={"role": role})


def _audit_rows(db: Session) -> list[AuditDecision]:
    db.expire_all()
    return list(db.scalars(select(AuditDecision).order_by(AuditDecision.id)))


def test_admin_changes_a_role_and_the_change_is_one_audit_row(web, db, sign_in):
    admin = sign_in(web, db, "admin")
    target = _target(db)
    response = _role_change(web, target.id, "sponsor")
    assert response.status_code == 200
    assert response.json()["role"] == "sponsor"
    assert "proposal:approve" in response.json()["actions"]
    db.refresh(target)
    assert target.role == "sponsor"

    (row,) = _audit_rows(db)
    assert (row.agent, row.trigger, row.subject_type, row.subject_id) == (
        "auth",
        "human",
        "user",
        str(target.id),
    )
    assert (row.version, row.attempt, row.status) == (1, 1, "ok")
    assert row.actor_user_id == admin.id
    assert row.action_taken == {"action": "role_change", "from": "project_manager", "to": "sponsor"}

    # A second change is the next attempt for the same user.
    assert _role_change(web, target.id, "auditor").status_code == 200
    assert [r.attempt for r in _audit_rows(db)] == [1, 2]


def test_the_new_role_applies_on_the_next_request(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db, role="auditor")
    other = TestClient(app, base_url="http://localhost")
    other.cookies.set(SESSION_COOKIE, issue_token(target.id, "auditor").token)
    assert other.get("/api/v1/audit").status_code == 200
    assert _role_change(web, target.id, "sponsor").status_code == 200
    assert other.get("/api/v1/audit").status_code == 403


def test_nobody_can_change_their_own_role(web, db, sign_in):
    admin = sign_in(web, db, "admin")
    _target(db, role="admin", email="second.admin@pmo.example")
    response = _role_change(web, admin.id, "auditor")
    assert response.status_code == 403
    db.refresh(admin)
    assert admin.role == "admin"
    assert _audit_rows(db) == []


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.ADMIN])
def test_only_admin_can_change_a_role(web, db, sign_in, role):
    sign_in(web, db, role)
    target = _target(db)
    assert _role_change(web, target.id, "sponsor").status_code == 403
    db.refresh(target)
    assert target.role == "project_manager"
    assert _audit_rows(db) == []


@pytest.mark.parametrize("body", [{"role": "pm"}, {"role": "superuser"}, {}, {"role": None}])
def test_an_unknown_role_is_422(web, db, sign_in, body):
    sign_in(web, db, "admin")
    target = _target(db)
    assert web.patch(f"/api/v1/users/{target.id}/role", json=body).status_code == 422
    assert _audit_rows(db) == []


def test_extra_fields_are_refused(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    body = {"role": "sponsor", "is_active": False}
    assert web.patch(f"/api/v1/users/{target.id}/role", json=body).status_code == 422


def test_an_unknown_user_is_404(web, db, sign_in):
    sign_in(web, db, "admin")
    assert _role_change(web, 9999, "sponsor").status_code == 404


def test_the_same_role_again_changes_nothing(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    assert _role_change(web, target.id, "project_manager").status_code == 200
    assert _audit_rows(db) == []


def test_role_change_needs_the_csrf_header(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    del web.headers["X-CSRF-Token"]
    assert _role_change(web, target.id, "sponsor").status_code == 403
    assert _audit_rows(db) == []


def test_admin_can_demote_another_admin_while_one_remains(web, db, sign_in):
    sign_in(web, db, "admin")
    other = _target(db, role="admin", email="second.admin@pmo.example")
    assert _role_change(web, other.id, "auditor").status_code == 200


def test_a_change_that_would_leave_no_active_admin_is_409(web, db, sign_in, monkeypatch):
    # Two admins demote each other at once: the actor is demoted by the other admin after its
    # own request has passed the guard. Its change would leave no admin, so it is refused.
    actor = sign_in(web, db, "admin")
    other = _target(db, role="admin", email="second.admin@pmo.example")
    real = users_api._active_admin_ids

    def demoted_meanwhile(session):
        with Session(engine) as concurrent:
            concurrent.get(User, actor.id).role = "auditor"
            concurrent.commit()
        return real(session)

    monkeypatch.setattr(users_api, "_active_admin_ids", demoted_meanwhile)
    response = _role_change(web, other.id, "auditor")
    assert response.status_code == 409
    db.refresh(other)
    assert other.role == "admin"
    assert _audit_rows(db) == []


def test_no_secret_in_the_role_change_audit_row(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    _role_change(web, target.id, "sponsor")
    (row,) = _audit_rows(db)
    text = repr([getattr(row, c) for c in AuditDecision.__table__.columns.keys()])
    secrets = (web.cookies[SESSION_COOKIE], CSRF["X-CSRF-Token"], target.password_hash)
    for secret in secrets:
        assert secret not in text
    assert row.action_taken == {"action": "role_change", "from": "project_manager", "to": "sponsor"}


# --- Unlocking an account


def _locked_target(db: Session) -> User:
    target = _target(db)
    target.locked_until = datetime(2999, 1, 1)
    target.failed_logins = 3
    target.failed_window_started_at = datetime(2026, 11, 1)
    db.commit()
    return target


def test_admin_unlocks_an_account_and_the_unlock_is_one_audit_row(web, db, sign_in):
    admin = sign_in(web, db, "admin")
    target = _locked_target(db)
    response = web.post(f"/api/v1/users/{target.id}/unlock")
    assert response.status_code == 200
    db.refresh(target)
    assert target.locked_until is None
    assert target.failed_logins == 0
    assert target.failed_window_started_at is None

    (row,) = _audit_rows(db)
    assert (row.agent, row.trigger, row.subject_type, row.subject_id, row.status) == (
        "auth",
        "human",
        "user",
        str(target.id),
        "ok",
    )
    assert row.actor_user_id == admin.id
    assert row.signals == {"event": "unlock"}
    assert row.action_taken == {"action": "unlock", "was_locked": True}


def test_unlocking_an_expired_lock_records_was_locked_false(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    target.locked_until = datetime(2000, 1, 1)
    db.commit()
    assert web.post(f"/api/v1/users/{target.id}/unlock").status_code == 200
    (row,) = _audit_rows(db)
    assert row.action_taken == {"action": "unlock", "was_locked": False}


def test_unlocking_an_account_that_was_never_locked_records_was_locked_false(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    assert web.post(f"/api/v1/users/{target.id}/unlock").status_code == 200
    (row,) = _audit_rows(db)
    assert row.action_taken == {"action": "unlock", "was_locked": False}


def test_unlock_and_role_change_share_the_attempt_sequence(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _locked_target(db)
    assert web.post(f"/api/v1/users/{target.id}/unlock").status_code == 200
    assert _role_change(web, target.id, "auditor").status_code == 200
    assert [r.attempt for r in _audit_rows(db)] == [1, 2]


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.ADMIN])
def test_only_admin_can_unlock(web, db, sign_in, role):
    sign_in(web, db, role)
    target = _locked_target(db)
    assert web.post(f"/api/v1/users/{target.id}/unlock").status_code == 403
    db.refresh(target)
    assert target.locked_until is not None
    assert _audit_rows(db) == []


def test_an_admin_cannot_unlock_their_own_account(web, db, sign_in):
    admin = sign_in(web, db, "admin")
    admin.locked_until = datetime(2999, 1, 1)
    db.commit()
    assert web.post(f"/api/v1/users/{admin.id}/unlock").status_code == 403
    db.refresh(admin)
    assert admin.locked_until is not None
    assert _audit_rows(db) == []


def test_unlock_of_an_unknown_user_is_404(web, db, sign_in):
    sign_in(web, db, "admin")
    assert web.post("/api/v1/users/9999/unlock").status_code == 404


def test_unlock_needs_the_csrf_header(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _locked_target(db)
    del web.headers["X-CSRF-Token"]
    assert web.post(f"/api/v1/users/{target.id}/unlock").status_code == 403
    assert _audit_rows(db) == []
