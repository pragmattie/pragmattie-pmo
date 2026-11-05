"""SQL injection attempts against list filters and paging (item 2.4).

Every query goes through SQLAlchemy with bound parameters, so an injection string is only ever
a value. The filters must match nothing (200, empty page), paging values must be refused (422),
nothing may give a 500, and every table and row must still be there afterwards.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from app.audit import record_decision
from app.db import engine
from app.main import app
from app.models import AuditDecision, Client, Project, User

INJECTIONS = [
    "' OR '1'='1",
    "' OR 1=1 --",
    '" OR "1"="1',
    "auth' --",
    "auth'; DROP TABLE users; --",
    "1; DELETE FROM pmo_audit",
    "' UNION SELECT id, email, password_hash FROM users --",
    "auth' AND SLEEP(5) --",
    "%",
    "_",
    "\\",
    "auth\x00",
    "`trigger`",
]

AUDIT_FILTERS = ["agent", "subject_type", "subject_id", "status", "tier"]
LIST_ROUTES = ["/api/v1/audit", "/api/v1/clients", "/api/v1/projects"]
PAGING_VALUES = [
    "1 OR 1=1",
    "1; DROP TABLE users",
    "1)--",
    "-1",
    "0x10",
    "1e3",
    "",
    "99999999999999999999",
    "9223372036854775808",
]


@pytest.fixture
def seeded(db, sign_in) -> TestClient:
    """An auditor's client, with rows in every listed table so a match would show."""
    web = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
    sign_in(web, db, "auditor")
    client = Client(name="Northwind Example", contact_email="ops@northwind.example")
    db.add(client)
    db.flush()
    db.add(
        Project(
            client_id=client.id,
            name="Ledger Example",
            stage="intake",
            sponsor="Sam Sponsor",
            pm="Pat Manager",
            status="on_track",
        )
    )
    for n in range(3):
        record_decision(
            db, agent="auth", trigger="human", subject_type="login", subject_id=str(n), status="ok"
        )
    db.commit()
    return web


def _snapshot(db) -> dict:
    db.expire_all()
    counts = {
        model.__tablename__: db.scalar(select(func.count()).select_from(model))
        for model in (AuditDecision, Client, Project, User)
    }
    return {"tables": sorted(inspect(engine).get_table_names()), "counts": counts}


@pytest.mark.parametrize("value", INJECTIONS)
@pytest.mark.parametrize("field", AUDIT_FILTERS)
def test_injection_in_an_audit_filter_matches_nothing(seeded, db, field, value):
    before = _snapshot(db)
    response = seeded.get("/api/v1/audit", params={field: value})
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
    assert response.json()["total"] == 0
    assert _snapshot(db) == before


def test_a_genuine_filter_still_matches(seeded):
    # Proves the filters work, so the empty pages above mean "no match", not "broken".
    assert seeded.get("/api/v1/audit", params={"agent": "auth"}).json()["total"] == 3


@pytest.mark.parametrize("value", PAGING_VALUES)
@pytest.mark.parametrize("param", ["limit", "offset"])
@pytest.mark.parametrize("route", LIST_ROUTES)
def test_injection_in_paging_is_422(seeded, db, route, param, value):
    before = _snapshot(db)
    response = seeded.get(route, params={param: value})
    assert response.status_code == 422, response.text
    assert _snapshot(db) == before


@pytest.mark.parametrize("route", LIST_ROUTES)
def test_the_largest_allowed_offset_is_an_empty_page(seeded, route):
    from app.schemas.common import MAX_OFFSET

    response = seeded.get(route, params={"offset": MAX_OFFSET})
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert seeded.get(route, params={"offset": MAX_OFFSET + 1}).status_code == 422


@pytest.mark.parametrize("value", INJECTIONS[:5])
def test_injection_in_the_login_email_is_a_plain_failure(db, value):
    before = _snapshot(db)
    web = TestClient(app, raise_server_exceptions=False)
    response = web.post("/api/v1/auth/login", json={"email": value, "password": value})
    assert response.status_code == 401
    after = _snapshot(db)
    # The failure itself adds one audit row; nothing else changes.
    assert after["tables"] == before["tables"]
    assert after["counts"] == {**before["counts"], "pmo_audit": before["counts"]["pmo_audit"] + 1}
