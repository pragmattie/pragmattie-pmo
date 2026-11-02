import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Base, Client, Project
from app.models.portfolio import STAGES, STATUSES
from app.seed import CLIENTS, PROJECTS, clear_seeded, main, seed

API_DIR = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[\w-]+")


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


def _snapshot(db: Session) -> list[tuple]:
    clients = db.execute(select(Client).order_by(Client.id)).scalars()
    projects = db.execute(select(Project).order_by(Project.id)).scalars()
    return [
        tuple(getattr(row, col) for col in row.__table__.columns.keys())
        for row in [*clients, *projects]
    ]


def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def _own_project(client_id: int) -> Project:
    """A project a person created: no seed_key."""
    return Project(
        client_id=client_id, name="Own", stage="intake", sponsor="S", pm="P", status="on_track"
    )


def test_seed_creates_three_clients_and_an_internal_project(db):
    seed(db)
    db.commit()
    assert _count(db, Client) == 3
    assert _count(db, Project) == 4
    sync = db.scalar(select(Project).where(Project.name == "PragMattie Sync"))
    assert sync is not None and sync.client_id is None
    client_projects = db.scalars(select(Project).where(Project.client_id.is_not(None))).all()
    assert len({p.client_id for p in client_projects}) == 3


def test_seeded_projects_use_known_stages_and_statuses():
    for project in PROJECTS:
        assert project["stage"] in STAGES, project["seed_key"]
        assert project["status"] in STATUSES, project["seed_key"]


def test_every_email_and_website_is_on_example(db):
    seed(db)
    db.commit()
    # Check the data itself and what was stored, including any address hidden in a text field.
    texts = [str(v) for row in (*CLIENTS, *PROJECTS) for v in row.values() if v]
    texts += [str(v) for row in _snapshot(db) for v in row if isinstance(v, str)]
    emails = [m.group(0) for t in texts for m in EMAIL.finditer(t)]
    assert emails, "seed should include contact emails"
    assert all(email.endswith(".example") for email in emails), emails
    for client in db.scalars(select(Client)):
        assert client.website.removeprefix("https://").endswith(".example")
        assert client.contact_email.endswith(".example")
    for project in db.scalars(select(Project)):
        assert project.sponsor_email.endswith(".example")
        assert project.pm_email.endswith(".example")


def test_seed_is_deterministic_and_idempotent(db):
    seed(db)
    db.commit()
    first = _snapshot(db)
    # Drift a seeded row; seeding again restores it and adds nothing.
    db.scalar(select(Project).where(Project.name == "PragMattie Sync")).status = "off_track"
    db.commit()
    seed(db)
    db.commit()
    assert _snapshot(db) == first


def test_reset_clears_only_seeded_rows(db):
    seed(db)
    own_client = Client(name="A Person's Client Ltd", website="https://own.example")
    db.add(own_client)
    db.flush()
    db.add(_own_project(own_client.id))
    db.commit()

    assert clear_seeded(db) == (3, 4)
    db.commit()
    assert db.scalars(select(Client.name)).all() == ["A Person's Client Ltd"]
    assert db.scalars(select(Project.name)).all() == ["Own"]


def test_reset_keeps_a_seeded_client_that_a_persons_project_uses(db):
    seed(db)
    db.commit()
    quillmere = db.scalar(select(Client).where(Client.seed_key == "client-quillmere"))
    db.add(_own_project(quillmere.id))
    db.commit()

    assert clear_seeded(db) == (2, 4)
    db.commit()
    assert db.scalars(select(Client.seed_key)).all() == ["client-quillmere"]
    # Seeding again reuses that client rather than duplicating it.
    seed(db, reset=True)
    db.commit()
    assert _count(db, Client) == 3 and _count(db, Project) == 5


def test_reset_then_seed_gives_the_same_data(db):
    seed(db)
    db.commit()
    names = sorted(db.scalars(select(Project.name)))
    result = seed(db, reset=True)
    db.commit()
    assert (result.cleared_clients, result.cleared_projects) == (3, 4)
    assert sorted(db.scalars(select(Project.name))) == names
    assert _count(db, Client) == 3


def test_main_seeds_and_resets(db, capsys):
    assert main([]) == 0
    assert main([]) == 0
    assert main(["--reset"]) == 0
    out = capsys.readouterr().out
    assert "Cleared 3 clients and 4 projects." in out
    assert _count(db, Client) == 3 and _count(db, Project) == 4


def test_main_reports_a_missing_schema_without_a_traceback(capsys):
    # No tables (the fixture is not used), so the seed fails cleanly.
    assert main([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Seed failed:") and "Traceback" not in err


def test_runs_as_a_module(tmp_path):
    env = {"APP_ENV": "test", "DATABASE_URL": f"sqlite:///{tmp_path / 'm.db'}"}
    result = subprocess.run(
        [sys.executable, "-m", "app.seed", "--help"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and "--reset" in result.stdout


def test_api_lists_are_paged_and_stable(db, client, sign_in):
    sign_in(client, db, "auditor")
    seed(db)
    db.commit()
    body = client.get("/api/v1/clients").json()
    assert body["total"] == 3 and body["limit"] == 50 and body["offset"] == 0
    names = [c["name"] for c in body["items"]]
    assert names == sorted(names)
    assert "seed_key" not in body["items"][0]

    seen = []
    for offset in (0, 2):
        page = client.get("/api/v1/projects", params={"limit": 2, "offset": offset}).json()
        assert page["total"] == 4
        seen += [p["id"] for p in page["items"]]
    assert len(set(seen)) == 4
    sync = next(p for p in client.get("/api/v1/projects").json()["items"] if p["client_id"] is None)
    assert sync["name"] == "PragMattie Sync"


@pytest.mark.parametrize("path", ["/api/v1/clients", "/api/v1/projects"])
def test_api_lists_are_read_only_and_validate_paging(db, client, sign_in, path):
    sign_in(client, db, "auditor")
    assert client.get(path, params={"limit": 101}).status_code == 422
    assert client.get(path, params={"offset": -1}).status_code == 422
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(path).status_code == 405
