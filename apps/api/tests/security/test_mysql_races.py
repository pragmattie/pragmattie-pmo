"""Two rules SQLite can't prove, run on real MySQL (item 2.4).

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

import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

import app.api.auth as auth_api
import app.api.users as users_api
from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import AuditDecision, User
from app.models.audit import AUDIT_TABLE
from app.models.auth import REVOKED_TOKENS_TABLE, USERS_TABLE
from app.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, hash_password, issue_token

pytestmark = pytest.mark.mysql

CSRF_VALUE = "mysql-race-csrf-value"
# Made up for these tests only; never a real credential.
PASSWORD = "mysql-race-test-passphrase"
# Below both the lockout threshold and the per-IP login limit, so neither can hide a lost count.
PARALLEL_FAILURES = 4
ROUNDS = 5
BARRIER_TIMEOUT = 20


@pytest.fixture(scope="module")
def mysql_engine() -> Iterator[Engine]:
    url = get_settings().test_mysql_url
    if not url:
        pytest.skip("TEST_MYSQL_URL is not set; these tests need a real, migrated MySQL database")
    engine = create_engine(url, pool_pre_ping=True, pool_size=10)
    assert engine.dialect.name == "mysql", "TEST_MYSQL_URL must be a MySQL database"
    yield engine
    engine.dispose()


@pytest.fixture
def mysql_db(mysql_engine) -> Iterator[sessionmaker]:
    """Empty tables, and the app's `get_db` pointed at MySQL for the test."""
    with mysql_engine.begin() as conn:
        for table in (AUDIT_TABLE, REVOKED_TOKENS_TABLE, USERS_TABLE):
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
