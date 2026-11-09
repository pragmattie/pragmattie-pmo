"""Seed and reset (item 6.9) on real MySQL.

These run only against the migrated MySQL database named by `TEST_MYSQL_URL` (CI runs them with
`--require-mysql`); without it they skip. They touch only the demo portfolio: the reset clears
the seeded rows and the orchestrator's work, and the test clears the seeded rows again at the
end, so the next MySQL test starts from what it expects. The audit table is never emptied here.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, func, make_url, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuditDecision, ChangeRequest, Client, Project, ProjectRequest, Risk
from app.seed import CLIENTS, PROJECTS, REQUESTS, RISKS, clear_seeded, seed

LOCAL_HOSTS = {"localhost", "127.0.0.1"}


@pytest.fixture(scope="module")
def mysql_engine() -> Iterator[Engine]:
    url = get_settings().test_mysql_url
    if not url:
        pytest.skip("TEST_MYSQL_URL is not set; these tests need a real, migrated MySQL database")
    host = make_url(url).host
    assert host in LOCAL_HOSTS, f"TEST_MYSQL_URL must point at localhost or 127.0.0.1, not {host!r}"
    engine = create_engine(url, pool_pre_ping=True)
    assert engine.dialect.name == "mysql", "TEST_MYSQL_URL must be a MySQL database"
    yield engine
    engine.dispose()


def _count(db: Session, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where))


def _seeded(db: Session) -> tuple[int, int, int, int]:
    return (
        _count(db, Client, Client.seed_key.is_not(None)),
        _count(db, Project, Project.seed_key.is_not(None)),
        _count(db, ProjectRequest, ProjectRequest.seed_key.is_not(None)),
        _count(db, Risk, Risk.seed_key.is_not(None)),
    )


@pytest.mark.mysql
def test_seed_and_reset_on_mysql(mysql_engine):
    expected = (len(CLIENTS), len(PROJECTS), len(REQUESTS), len(RISKS))
    with Session(mysql_engine, expire_on_commit=False) as db:
        try:
            audit_before = _count(db, AuditDecision)
            seed(db)
            db.commit()
            seed(db)  # seeding again adds nothing
            db.commit()
            assert _seeded(db) == expected
            for _ in range(2):  # a reset can be repeated
                seed(db, reset=True)
                db.commit()
                assert _seeded(db) == expected
            assert _count(db, ChangeRequest, ChangeRequest.status == "submitted") >= 1
            # Append-only: each run adds rows and none is deleted.
            assert _count(db, AuditDecision) > audit_before + 4
        finally:
            db.rollback()
            clear_seeded(db)
            db.commit()
