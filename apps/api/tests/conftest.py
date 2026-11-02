import os
import tempfile

import pytest

# Tests use a throwaway SQLite file, set before the app is imported.
_db_dir = tempfile.mkdtemp()
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_db_dir, 'test.db')}"
# Cheap argon2 parameters keep the suite fast; production uses the defaults in app/config.py.
os.environ["ARGON2_TIME_COST"] = "1"
os.environ["ARGON2_MEMORY_COST"] = "1024"
os.environ["ARGON2_PARALLELISM"] = "1"
# The test client talks plain http, so it would not send Secure cookies back. Tests that check
# the Secure flag turn this on themselves; the real default is on (see app/config.py).
os.environ["COOKIE_SECURE"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

# How the tests marked `mysql` ended in this run (see `--require-mysql`).
_MYSQL_OUTCOMES = {"skipped": 0, "passed": 0}


def pytest_addoption(parser):
    parser.addoption(
        "--require-mysql",
        action="store_true",
        help="fail the run if a test marked mysql is skipped, or if none runs (CI uses this)",
    )


def pytest_runtest_logreport(report):
    """Count how tests marked `mysql` ended, for `--require-mysql`."""
    if "mysql" not in report.keywords:
        return
    if report.skipped:
        _MYSQL_OUTCOMES["skipped"] += 1
    elif report.when == "call" and report.passed:
        _MYSQL_OUTCOMES["passed"] += 1


def pytest_sessionfinish(session, exitstatus):
    """With `--require-mysql`, a skipped MySQL test is a failure: otherwise a missing
    TEST_MYSQL_URL would let CI pass without testing anything."""
    if not session.config.getoption("--require-mysql"):
        return
    skipped, passed = _MYSQL_OUTCOMES["skipped"], _MYSQL_OUTCOMES["passed"]
    if skipped or not passed:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                f"--require-mysql: {skipped} mysql test(s) skipped, {passed} passed; "
                "set TEST_MYSQL_URL to a throwaway MySQL database",
                red=True,
            )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _sign_in(web: TestClient, db, role: str, email: str | None = None):
    """Add an active user with `role` and give `web` a session and CSRF cookie for them.

    The session token is issued directly (no password check), so this is quick; test_auth.py
    covers the real login. The tables must exist (each test file's `db` fixture makes them).
    """
    from app.models import User
    from app.security import CSRF_COOKIE, SESSION_COOKIE, issue_token

    user = User(
        email=email or f"{role.replace('_', '.')}@pmo.example",
        display_name=role.replace("_", " ").title(),
        password_hash="not-a-real-hash",
        role=role,
    )
    db.add(user)
    db.commit()
    web.cookies.set(SESSION_COOKIE, issue_token(user.id, role).token)
    web.cookies.set(CSRF_COOKIE, "test-csrf-value")
    web.headers["X-CSRF-Token"] = "test-csrf-value"
    return user


@pytest.fixture
def sign_in():
    """`sign_in(client, db, role)` signs `client` in as a new user with that role."""
    return _sign_in


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    """The login rate limiter is per process, so every test starts with a clean count."""
    from app.api.auth import login_limiter

    login_limiter.reset()
    yield
    login_limiter.reset()
