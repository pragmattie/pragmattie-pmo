"""Rules SQLite can't prove, run on real MySQL (item 2.4).

SQLite has no row locks (`SELECT ... FOR UPDATE` does nothing there), so these run only against
the MySQL database named by the `TEST_MYSQL_URL` setting, which must already be migrated
(`alembic upgrade head`). Without it they skip; CI runs them with `--require-mysql`, which turns
a skip into a failure.

The database is emptied before each test with raw SQL on a plain connection: that is the very
gap `docs/security.md` records (the audit table's append-only guard is in the ORM only), used here
on purpose on a throwaway database. Never point TEST_MYSQL_URL at data that matters.

Each test makes the race certain rather than lucky: a barrier holds every request at the point
just before it takes its row lock, then releases them all at once.
"""

import json
import threading
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url, select, text
from sqlalchemy.orm import Session, sessionmaker

import app.api.auth as auth_api
import app.api.gates as gates_api
import app.api.risks as risks_api
import app.api.users as users_api
from app.agents import risk_agent
from app.agents.risk_agent import ClaudeReply
from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import (
    AuditDecision,
    GateDecision,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectBaseline,
    ProjectBrief,
    ProjectPlan,
    Risk,
    User,
)
from app.models.audit import AUDIT_TABLE
from app.models.auth import REVOKED_TOKENS_TABLE, USERS_TABLE
from app.models.baseline import BASELINES_TABLE, GATE_DECISIONS_TABLE
from app.models.plan import (
    DELIVERABLES_TABLE,
    ESTIMATE_LINES_TABLE,
    MILESTONES_TABLE,
    PLANS_TABLE,
)
from app.models.planning import BRIEFS_TABLE
from app.models.risk import PROJECTS_TABLE, RISKS_TABLE
from app.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, hash_password, issue_token

CSRF_VALUE = "mysql-race-csrf-value"
# Made up for these tests only; never a real credential.
PASSWORD = "mysql-race-test-passphrase"  # secret-scan: allow
# Below both the lockout threshold and the per-IP login limit, so neither can hide a lost count.
PARALLEL_FAILURES = 4
ROUNDS = 5
BARRIER_TIMEOUT = 20
# These tests delete rows, audit rows included, so they only ever touch a local server.
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
# Emptied before each test, children before the rows they point at.
TABLES = (
    BASELINES_TABLE,
    GATE_DECISIONS_TABLE,
    ESTIMATE_LINES_TABLE,
    DELIVERABLES_TABLE,
    MILESTONES_TABLE,
    PLANS_TABLE,
    BRIEFS_TABLE,
    AUDIT_TABLE,
    RISKS_TABLE,
    PROJECTS_TABLE,
    REVOKED_TOKENS_TABLE,
    USERS_TABLE,
)
GATE_REASON = "The plan is complete and inside the budget"
GATE_TICKS = [
    {"id": "team_and_dates_realistic", "met": True, "note": "Owners agreed every date"},
    {"id": "risks_acceptable", "met": True, "note": "Nothing open we can't carry"},
]


def _assert_local(url: str) -> None:
    """Fail (not skip) unless TEST_MYSQL_URL names a local host, so a wrong URL can never
    reach the hosted database."""
    host = make_url(url).host
    assert host in LOCAL_HOSTS, f"TEST_MYSQL_URL must point at localhost or 127.0.0.1, not {host!r}"


@pytest.fixture(scope="module")
def mysql_engine() -> Iterator[Engine]:
    url = get_settings().test_mysql_url
    if not url:
        pytest.skip("TEST_MYSQL_URL is not set; these tests need a real, migrated MySQL database")
    _assert_local(url)
    engine = create_engine(url, pool_pre_ping=True, pool_size=10)
    assert engine.dialect.name == "mysql", "TEST_MYSQL_URL must be a MySQL database"
    yield engine
    engine.dispose()


@pytest.fixture
def mysql_db(mysql_engine) -> Iterator[sessionmaker]:
    """Empty tables, and the app's `get_db` pointed at MySQL for the test."""
    with mysql_engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"DELETE FROM `{table}`"))
    factory = sessionmaker(bind=mysql_engine, autoflush=False, expire_on_commit=False)

    def mysql_get_db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = mysql_get_db
    try:
        yield factory
    finally:
        app.dependency_overrides.pop(get_db, None)


def _add_user(factory: sessionmaker, email: str, role: str, password_hash: str) -> int:
    with factory() as session:
        user = User(email=email, display_name=email, password_hash=password_hash, role=role)
        session.add(user)
        session.commit()
        return user.id


def _signed_in(user_id: int, role: str) -> TestClient:
    web = TestClient(app, raise_server_exceptions=False)
    web.cookies.set(SESSION_COOKIE, issue_token(user_id, role).token)
    web.cookies.set(CSRF_COOKIE, CSRF_VALUE)
    web.headers[CSRF_HEADER] = CSRF_VALUE
    return web


def _run_together(calls) -> list:
    """Run each call in its own thread and return their results in order."""
    results: list = [None] * len(calls)

    def run(i, call):
        results[i] = call()

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "a request hung"
    return results


@pytest.mark.parametrize(
    "url",
    [
        "mysql+pymysql://root@localhost:3307/pragmattie_pmo",
        "mysql+pymysql://root@127.0.0.1:3306/pragmattie_pmo",
    ],
)
def test_local_mysql_urls_are_allowed(url):
    _assert_local(url)


@pytest.mark.parametrize(
    "url",
    [
        "mysql+pymysql://user@db-hosted.example:3306/pragmattie_pmo",
        "mysql+pymysql://user@localhost.example:3306/pragmattie_pmo",
        "mysql+pymysql://user@10.0.0.5/pragmattie_pmo",
        "mysql+pymysql:///pragmattie_pmo",
    ],
)
def test_non_local_mysql_urls_are_refused(url):
    with pytest.raises(AssertionError, match="localhost or 127.0.0.1"):
        _assert_local(url)


@pytest.mark.mysql
def test_two_admins_demoting_each_other_leave_one_admin(mysql_db, monkeypatch):
    factory = mysql_db
    a = _add_user(factory, "admin.a@pmo.example", "admin", "not-a-real-hash")
    b = _add_user(factory, "admin.b@pmo.example", "admin", "not-a-real-hash")
    real = users_api._active_admin_ids
    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def at_the_same_moment(session):
        # Both requests have passed the guard and loaded their target; now both lock at once.
        barrier.wait()
        return real(session)

    monkeypatch.setattr(users_api, "_active_admin_ids", at_the_same_moment)
    statuses = []
    for _ in range(ROUNDS):
        barrier.reset()
        web_a, web_b = _signed_in(a, "admin"), _signed_in(b, "admin")
        responses = _run_together(
            [
                lambda web_a=web_a: web_a.patch(
                    f"/api/v1/users/{b}/role", json={"role": "auditor"}
                ),
                lambda web_b=web_b: web_b.patch(
                    f"/api/v1/users/{a}/role", json={"role": "auditor"}
                ),
            ]
        )
        codes = sorted(r.status_code for r in responses)
        statuses.append(codes)
        assert 500 not in codes, [r.text for r in responses]
        assert set(codes) <= {200, 403, 409}, [r.text for r in responses]
        assert codes.count(200) <= 1

        with factory() as session:
            admins = session.scalars(
                select(User.id).where(User.role == "admin", User.is_active.is_(True))
            ).all()
            changes = session.scalars(
                select(AuditDecision).where(AuditDecision.subject_type == "user")
            ).all()
        assert len(admins) >= 1
        # One audit row per change made, none for a refused one.
        assert len(changes) == codes.count(200)
        # Put both back to admin for the next round (raw SQL on a throwaway database).
        with factory.kw["bind"].begin() as conn:
            conn.execute(text(f"UPDATE `{USERS_TABLE}` SET role = 'admin'"))
            conn.execute(text(f"DELETE FROM `{AUDIT_TABLE}`"))
    # Normally one demotion goes through and the other is refused (409). If MySQL picks a
    # deadlock victim instead, that request is a clean 409 too and nothing is lost.
    assert all(codes in ([200, 409], [409, 409]) for codes in statuses), statuses
    # At least one demotion must go through, or a guard that refuses everything would pass.
    assert [200, 409] in statuses, statuses


@pytest.mark.mysql
def test_parallel_login_failures_are_all_counted(mysql_db, monkeypatch):
    settings = get_settings()
    assert PARALLEL_FAILURES < settings.lockout_threshold
    assert PARALLEL_FAILURES < settings.login_rate_per_minute
    factory = mysql_db
    email = "parallel.failures@pmo.example"
    user_id = _add_user(factory, email, "auditor", hash_password(PASSWORD))
    real = auth_api.verify_password
    barrier = threading.Barrier(PARALLEL_FAILURES, timeout=BARRIER_TIMEOUT)

    def at_the_same_moment(password_hash, password):
        # Every request has read the user (count 0) before any of them takes the row lock.
        ok = real(password_hash, password)
        barrier.wait()
        return ok

    monkeypatch.setattr(auth_api, "verify_password", at_the_same_moment)
    body = {"email": email, "password": "not-the-password"}
    clients = [TestClient(app, raise_server_exceptions=False) for _ in range(PARALLEL_FAILURES)]
    responses = _run_together(
        [lambda web=web: web.post("/api/v1/auth/login", json=body) for web in clients]
    )
    assert [r.status_code for r in responses] == [401] * PARALLEL_FAILURES, [
        r.text for r in responses
    ]

    with factory() as session:
        user = session.get(User, user_id)
        failures = session.scalars(
            select(AuditDecision).where(AuditDecision.subject_type == "login")
        ).all()
    assert user.failed_logins == PARALLEL_FAILURES
    assert user.locked_until is None
    assert len(failures) == PARALLEL_FAILURES
    assert {row.signals["reason"] for row in failures} == {"bad_password"}


class _ClaudeAtTheSameMoment:
    """A fake Claude client: every run waits here until all have read the project (and so
    picked the same highest audit attempt), then they all go on to save at once."""

    def __init__(self, barrier: threading.Barrier):
        self.barrier = barrier
        self.calls = 0
        self.lock = threading.Lock()

    def create(self, request: dict) -> ClaudeReply:
        with self.lock:
            self.calls += 1
            title = f"Parallel risk {self.calls}"
        self.barrier.wait()
        item = {
            "kind": "threat",
            "category": "ai",
            "title": title,
            "cause": "Two people asked at once",
            "effect": "Both runs save at the same moment",
            "objectives": ["quality"],
            "probability": 3,
            "impact": 3,
            "response": "reduce",
            "judgement": "none",
            "reason": "The project is in delivery.",
        }
        return ClaudeReply(json.dumps({"risks": [item]}), 1000, 200, "end_turn")


@pytest.mark.mysql
def test_two_overlapping_risk_agent_runs_each_write_one_audit_row(mysql_db, monkeypatch):
    factory = mysql_db
    monkeypatch.setattr(get_settings(), "agent_mode", "enforce")
    pm = _add_user(factory, "pm.parallel@pmo.example", "project_manager", "not-a-real-hash")
    with factory() as session:
        project = Project(
            name="Parallel Analytics", stage="delivery", sponsor="S", pm="P", status="on_track"
        )
        session.add(project)
        session.commit()
        project_id = project.id
    claude = _ClaudeAtTheSameMoment(threading.Barrier(2, timeout=BARRIER_TIMEOUT))
    app.dependency_overrides[risks_api.get_claude_client] = lambda: claude
    try:
        for round_ in range(1, ROUNDS + 1):
            claude.barrier.reset()
            webs = [_signed_in(pm, "project_manager") for _ in range(2)]
            responses = _run_together(
                [
                    lambda web=web: web.post(f"/api/v1/projects/{project_id}/risks/suggest")
                    for web in webs
                ]
            )
            assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
            with factory() as session:
                runs = session.scalars(
                    select(AuditDecision)
                    .where(AuditDecision.agent == risk_agent.AGENT)
                    .order_by(AuditDecision.attempt)
                ).all()
                saved = session.scalars(select(Risk).where(Risk.project_id == project_id)).all()
            # Exactly one audit row per run, each at its own attempt, and every risk kept.
            assert [run.attempt for run in runs] == list(range(1, 2 * round_ + 1))
            assert {run.status for run in runs} == {"ok"}
            assert {r.json()["audit_id"] for r in responses} <= {run.id for run in runs}
            assert len(saved) == 2 * round_
    finally:
        app.dependency_overrides.pop(risks_api.get_claude_client, None)


def _ready_project(factory: sessionmaker, pm: int) -> int:
    """A project in planning that can pass the go-ahead gate: a complete plan of 900.00 inside
    the brief's top budget, no client, no risks and no request."""
    with factory() as session:
        project = Project(
            name="Parallel Forecast",
            stage="planning",
            sponsor="S",
            pm="P",
            pm_user_id=pm,
            status="on_track",
        )
        session.add(project)
        session.flush()
        session.add(
            ProjectBrief(
                project_id=project.id,
                purpose="Fewer stock-outs",
                objectives="Forecast weekly demand",
                decision_rights=[],
                budget_high="1000.00",
            )
        )
        plan = ProjectPlan(
            project_id=project.id,
            scope_in="Weekly forecast for the pilot stores",
            scope_out="Other stores",
            contingency_pct=10,
            report_period="weekly",
            report_due_day=5,
        )
        session.add(plan)
        session.flush()
        milestone = PlanMilestone(
            plan_id=plan.id, name="Pilot live", target_date=date(2027, 3, 1), owner_user_id=pm
        )
        session.add(milestone)
        session.flush()
        session.add_all(
            [
                PlanDeliverable(plan_id=plan.id, name="Forecast model", milestone_id=milestone.id),
                PlanEstimateLine(
                    plan_id=plan.id, description="Analysts", category="people", amount="818.18"
                ),
            ]
        )
        session.commit()
        return project.id


@pytest.mark.mysql
def test_two_sponsors_deciding_one_project_give_one_decision_and_one_baseline(
    mysql_db, gates_policy, monkeypatch
):
    factory = mysql_db
    pm = _add_user(factory, "pm.gate@pmo.example", "project_manager", "not-a-real-hash")
    a = _add_user(factory, "sponsor.a@pmo.example", "sponsor", "not-a-real-hash")
    b = _add_user(factory, "sponsor.b@pmo.example", "sponsor", "not-a-real-hash")
    real = gates_api.ProjectSubject.load
    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def at_the_same_moment(db, subject_id, *, for_update=False):
        # Both decisions have passed the guard; now both take the project's row lock at once.
        if for_update:
            barrier.wait()
        return real(db, subject_id, for_update=for_update)

    monkeypatch.setattr(gates_api.ProjectSubject, "load", staticmethod(at_the_same_moment))
    body = {"version": 1, "outcome": "pass", "reason": GATE_REASON, "criteria": GATE_TICKS}
    for _ in range(ROUNDS):
        barrier.reset()
        pid = _ready_project(factory, pm)
        url = f"/api/v1/projects/{pid}/gate"
        webs = (_signed_in(a, "sponsor"), _signed_in(b, "sponsor"))
        responses = _run_together(
            [lambda web=web, url=url: web.post(url, json=body) for web in webs]
        )
        # One decision goes through; the other then sees the new version and is refused.
        assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]

        with factory() as session:
            project = session.get(Project, pid)
            decisions = session.scalars(
                select(GateDecision).where(GateDecision.subject_id == pid)
            ).all()
            baselines = session.scalars(
                select(ProjectBaseline).where(ProjectBaseline.project_id == pid)
            ).all()
            rows = session.scalars(
                select(AuditDecision)
                .where(AuditDecision.agent == "gate", AuditDecision.subject_id == str(pid))
                .order_by(AuditDecision.id)
            ).all()
        # One stage change, one decision, one baseline, made by that decision.
        assert (project.stage, project.version) == ("delivery", 2)
        assert len(decisions) == 1
        assert [(bl.number, bl.gate_decision_id) for bl in baselines] == [(1, decisions[0].id)]
        # One audit row for the decision and one for the refused attempt.
        assert [(r.status, r.action_taken["action"]) for r in rows] == [
            ("ok", "gate_pass"),
            ("rejected", "gate_refused"),
        ]
        assert rows[0].action_taken["baseline_id"] == baselines[0].id
        assert rows[1].action_taken["refusal"] == "stale_version"
