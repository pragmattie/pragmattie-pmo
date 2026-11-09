"""`python -m app.seed users`: one demo user per role, random passwords handed over once."""

import logging
import os
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from secret_scan import repo_root, tracked_files
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.authz import Role
from app.db import engine
from app.main import app
from app.models import AuditDecision, Base, User, utcnow
from app.seed import AUDIT_AGENT, AUDIT_SUBJECT, demo_users, main, seed_users

HERE = Path(__file__).resolve().parent
LOGIN = "/api/v1/auth/login"
# One demo user per role, plus a second project manager (item 6.9).
ROLES = sorted([*(r.value for r in Role), Role.PROJECT_MANAGER.value])
USERS = len(ROLES)


@pytest.fixture(autouse=True)
def _not_in_ci(monkeypatch):
    """CI sets `CI`, which makes printing refuse; tests that want that set it themselves."""
    monkeypatch.delenv("CI", raising=False)


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A throwaway git repository that ignores `*password*.txt`, as this repository does."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("*password*.txt\n")
    return tmp_path


def _parse(text: str) -> dict[str, str]:
    """email -> password from the role/email/password table."""
    rows = [line.split() for line in text.splitlines() if line.strip()]
    assert rows[0] == ["role", "email", "password"]
    return {row[1]: row[2] for row in rows[1:] if len(row) == 3}


def _printed(capsys) -> tuple[dict[str, str], str]:
    out = capsys.readouterr().out
    table, _, summary = out.rpartition("Seeded ")
    return _parse(table), "Seeded " + summary


def _counts(db: Session) -> tuple[int, int]:
    db.expire_all()
    users = db.scalar(select(func.count()).select_from(User))
    audits = db.scalar(select(func.count()).select_from(AuditDecision))
    return users, audits


def _logs_in(email: str, password: str) -> bool:
    web = TestClient(app, base_url="http://localhost")
    return web.post(LOGIN, json={"email": email, "password": password}).status_code == 200


def test_one_user_per_role_on_example(db):
    logins = seed_users(db)
    db.commit()
    users = db.scalars(select(User).order_by(User.id)).all()
    assert sorted(u.role for u in users) == ROLES
    assert len(users) == USERS == len(logins)
    assert len({u.email for u in users}) == USERS
    assert all(u.email.endswith(".example") and u.is_active for u in users)
    # The password is never in a repr.
    assert all(login.password not in repr(login) for login in logins)


def test_printed_passwords_log_in_and_change_every_run(db, capsys):
    assert main(["users"]) == 0
    first, summary = _printed(capsys)
    assert set(first) == {u["email"] for u in demo_users()}
    assert f"Seeded {USERS} demo users" in summary
    for email, password in first.items():
        assert _logs_in(email, password), email

    assert main(["users"]) == 0
    second, _ = _printed(capsys)
    assert set(second) == set(first)
    assert all(second[email] != first[email] for email in first)
    assert len(set(second.values())) == len(second)
    email = next(iter(first))
    assert not _logs_in(email, first[email])
    assert _logs_in(email, second[email])
    # Seeding again adds no users.
    assert _counts(db)[0] == USERS


def test_each_run_restores_the_whole_demo_user(db):
    seed_users(db)
    db.commit()
    admin_email = next(u["email"] for u in demo_users() if u["role"] == Role.ADMIN)
    admin = db.scalar(select(User).where(User.email == admin_email))
    admin.role = Role.AUDITOR.value
    admin.display_name = "Renamed in a rehearsal"
    admin.is_active = False
    admin.failed_logins = 4
    admin.failed_window_started_at = utcnow()
    admin.locked_until = utcnow() + timedelta(minutes=15)
    db.commit()

    logins = seed_users(db)
    db.commit()
    db.refresh(admin)
    assert admin.role == Role.ADMIN
    assert admin.display_name == "Demo Admin"
    assert admin.is_active is True
    assert (admin.failed_logins, admin.failed_window_started_at, admin.locked_until) == (
        0,
        None,
        None,
    )
    password = next(x.password for x in logins if x.email == admin_email)
    assert _logs_in(admin_email, password)


def test_one_audit_row_per_run_without_passwords(db):
    logins = seed_users(db)
    db.commit()
    rows = db.scalars(select(AuditDecision)).all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.agent, row.subject_type, row.trigger, row.status) == (
        AUDIT_AGENT,
        AUDIT_SUBJECT,
        "human",
        "ok",
    )
    assert row.actor_user_id is None and row.signals["actor"] == "system"
    assert row.action_taken["users"] == USERS
    assert sorted(row.action_taken["roles"]) == ROLES
    seed_users(db)
    db.commit()
    assert _counts(db)[1] == 2

    stored = [
        str(getattr(r, c))
        for model in (User, AuditDecision)
        for r in db.scalars(select(model))
        for c in model.__table__.columns.keys()
    ]
    for login in logins:
        assert not any(login.password in value for value in stored)


def test_no_password_in_logs(db, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    assert main(["users"]) == 0
    printed, _ = _printed(capsys)
    assert printed
    for password in printed.values():
        assert password not in caplog.text


def test_printed_passwords_are_not_in_any_tracked_file(db, capsys):
    assert main(["users"]) == 0
    printed, _ = _printed(capsys)
    root = repo_root(HERE)
    names = tracked_files(root)
    assert names
    for name in names:
        path = root / name
        if path.is_file():
            content = path.read_bytes()
            assert not any(p.encode() in content for p in printed.values()), name


def test_ci_refuses_to_print_before_touching_the_database(db, capsys, monkeypatch):
    monkeypatch.setenv("CI", "true")
    assert main(["users"]) != 0
    captured = capsys.readouterr()
    assert "CI" in captured.err and captured.out == ""
    assert _counts(db) == (0, 0)


def test_ci_with_a_password_file_is_allowed(db, repo, capsys, monkeypatch):
    monkeypatch.setenv("CI", "true")
    target = repo / "demo-passwords.txt"
    assert main(["users", "--password-file", str(target)]) == 0
    assert len(_parse(target.read_text())) == USERS


def test_password_file_holds_the_passwords_and_the_terminal_does_not(db, repo, capsys):
    target = repo / "demo-passwords.txt"
    assert main(["users", "--password-file", str(target)]) == 0
    written = _parse(target.read_text())
    assert set(written) == {u["email"] for u in demo_users()}
    for email, password in written.items():
        assert _logs_in(email, password), email
    out = capsys.readouterr().out
    assert f"Seeded {USERS} demo users" in out and str(target) in out
    assert not any(p in out for p in written.values())


@pytest.mark.skipif(sys.platform == "win32", reason="Windows ignores file modes")
@pytest.mark.parametrize("existing", [False, True])
def test_password_file_is_owner_only(db, repo, capsys, existing):
    target = repo / "demo-passwords.txt"
    if existing:
        target.write_text("an older, much longer file that must be replaced completely\n" * 20)
        os.chmod(target, 0o644)
    assert main(["users", "--password-file", str(target)]) == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert len(_parse(target.read_text())) == USERS


def _assert_refused(db, capsys, target: Path, reason: str) -> None:
    before = target.read_bytes() if target.exists() else None
    assert main(["users", "--password-file", str(target)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("Refused:") and reason in err
    # Nothing changed: no users, no audit row, the file as it was.
    assert _counts(db) == (0, 0)
    assert (target.read_bytes() if target.exists() else None) == before


def test_a_path_git_does_not_ignore_is_refused(db, repo, capsys):
    _assert_refused(db, capsys, repo / "demo-users.txt", "not git-ignored")


def test_a_tracked_file_is_never_overwritten(db, repo, capsys):
    target = repo / "demo-passwords.txt"
    target.write_text("tracked content\n")
    subprocess.run(["git", "-C", str(repo), "add", "-f", target.name], check=True)
    _assert_refused(db, capsys, target, "tracked by git")


def test_a_path_outside_a_repository_is_refused(db, tmp_path, capsys):
    _assert_refused(db, capsys, tmp_path / "demo-passwords.txt", "can't say")


def test_without_git_the_file_is_refused(db, repo, capsys, monkeypatch):
    monkeypatch.setattr("app.seed.shutil.which", lambda _name: None)
    _assert_refused(db, capsys, repo / "demo-passwords.txt", "git is not installed")


def test_a_missing_folder_is_refused(db, repo, capsys):
    _assert_refused(db, capsys, repo / "nowhere" / "demo-passwords.txt", "does not exist")


def test_a_failed_seed_hands_out_no_passwords(repo, capsys):
    # No tables (the db fixture is not used), so the database step fails.
    target = repo / "demo-passwords.txt"
    assert main(["users", "--password-file", str(target)]) == 1
    assert not target.exists()
    assert main(["users"]) == 1
    captured = capsys.readouterr()
    assert captured.err.count("Seed failed:") == 2 and captured.out == ""


def test_reset_does_not_apply_to_users(capsys):
    with pytest.raises(SystemExit):
        main(["--reset", "users"])
