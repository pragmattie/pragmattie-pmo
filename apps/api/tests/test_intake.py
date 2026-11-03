"""Project requests and the queue (item 4.1)."""

from datetime import date, datetime

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.intake import FINAL, NOT_DRAFT_EDIT, NOT_DRAFT_SUBMIT, NOT_WITHDRAWABLE, STALE
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    Client,
    ProjectRequest,
    ReadinessAction,
    ReadinessCheck,
    User,
    ValueCase,
    ValueCaseBenefit,
)
from app.models.intake import LONG_TEXT_MAX

BODY = {
    "title": "Warehouse demand forecast",
    "asked_for": "A weekly demand forecast for the two northern warehouses",
    "problem": "Stock-outs every quarter cost sales",
    "outcome": "Half as many stock-outs within a year",
}
URL = "/api/v1/requests"


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


@pytest.fixture
def pm(web, db, sign_in) -> User:
    return sign_in(web, db, "project_manager")


@pytest.fixture
def client_row(db) -> Client:
    row = Client(name="Northwind Example Ltd")
    db.add(row)
    db.commit()
    return row


def _create(web: TestClient, **changes):
    return web.post(URL, json={**BODY, **changes})


def _audit(db: Session) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(AuditDecision.subject_type == "project_request")
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def _move(web: TestClient, request_id: int, action: str, version: int):
    return web.post(f"{URL}/{request_id}/{action}", json={"version": version})


def _set_status(db: Session, request_id: int, status: str) -> int:
    """What the approval gate (a later item) will do; no route here can. Returns the new
    version, so a test can send the current one and reach the status rule."""
    with Session(engine) as session:
        row = session.get(ProjectRequest, request_id)
        row.status = status
        session.commit()
        return row.version


def add_value_case(request_id: int, confirmed_by: int | None, **changes) -> None:
    """A complete value case, straight into the database so the request's version is unchanged.
    `confirmed_by=None` leaves it as an unconfirmed agent draft."""
    fields = {
        "cost_low": "10000.00",
        "cost_high": "20000.00",
        "cost_method": "Two weeks of analysis at the day rate",
        "strategic_fit": "The client's goal of fewer stock-outs",
        "source": "person" if confirmed_by else "agent",
        "confirmed_by": confirmed_by,
        "confirmed_at": datetime(2026, 11, 1) if confirmed_by else None,
        **changes,
    }
    benefit = {"description": "Fewer stock-outs", "measure": "Stock-outs a quarter"}
    benefit |= {"current_value": "12", "target": "6", "target_date": date(2027, 12, 31)}
    with Session(engine) as session:
        value_case = ValueCase(request_id=request_id, **fields)
        value_case.benefits.append(ValueCaseBenefit(position=0, **benefit))
        session.add(value_case)
        session.commit()


def add_readiness(request_id: int, confirmed_by: int | None, owner_id: int, **changes) -> None:
    """A complete readiness check (problem green, approach yellow with an action, data green),
    straight into the database so the request's version is unchanged. `confirmed_by=None` leaves
    it as an unconfirmed agent draft."""
    fields = {
        "problem_rating": "green",
        "problem_note": "Stock-outs cost sales every quarter",
        "approach_rating": "yellow",
        "approach_note": "A simple reorder rule may do as well",
        "data_rating": "green",
        "data_note": "Three years of sales history",
        "source": "person" if confirmed_by else "agent",
        "confirmed_by": confirmed_by,
        "confirmed_at": datetime(2026, 11, 1) if confirmed_by else None,
        **changes,
    }
    action = {"description": "Compare with a reorder rule", "due_date": date(2026, 12, 1)}
    with Session(engine) as session:
        check = ReadinessCheck(request_id=request_id, **fields)
        check.actions.append(
            ReadinessAction(area="approach", position=0, owner_id=owner_id, **action)
        )
        session.add(check)
        session.commit()


def _ready(web: TestClient, user: User, **changes) -> int:
    """A draft request with a complete, confirmed value case, so it can be submitted."""
    rid = _create(web, **changes).json()["id"]
    add_value_case(rid, user.id)
    return rid


def _refused(response, detail: str) -> None:
    assert response.status_code == 409
    assert response.json()["detail"] == detail


# --- Create, edit, submit, withdraw


def test_a_project_manager_creates_a_draft(web, db, pm, client_row):
    response = _create(
        web,
        client_id=client_row.id,
        budget_low="10000",
        budget_high="25000.5",
        wanted_by="2027-03-31",
        uses_ai=True,
        known_risks="Sales history has gaps",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["version"] == 1
    assert body["requested_by"] == pm.id
    assert body["requested_by_name"] == pm.display_name
    assert body["client_name"] == "Northwind Example Ltd"
    assert body["submitted_at"] is None
    # Money is a string with 2 decimal places.
    assert (body["budget_low"], body["budget_high"]) == ("10000.00", "25000.50")
    assert body["wanted_by"] == "2027-03-31"

    (row,) = _audit(db)
    assert (row.agent, row.trigger, row.status) == ("intake", "human", "ok")
    assert (row.subject_id, row.version, row.actor_user_id) == (str(body["id"]), 1, pm.id)
    assert row.signals is None
    assert row.output["budget_low"] == "10000.00"
    assert row.output["status"] == "draft"
    assert row.action_taken == {"action": "create"}


def test_no_client_means_internal_work_and_optional_fields_are_empty(web, pm):
    body = _create(web).json()
    assert body["client_id"] is None and body["client_name"] is None
    assert body["budget_low"] is None and body["budget_high"] is None
    assert body["wanted_by"] is None and body["known_risks"] is None
    assert body["uses_ai"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"requested_by": 999},
        {"status": "approved"},
        {"submitted_at": "2026-01-01T00:00:00"},
        {"version": 3},
        {"surprise": 1},
    ],
)
def test_server_owned_and_unknown_fields_are_refused_on_create(web, db, pm, extra):
    assert _create(web, **extra).status_code == 422
    assert _audit(db) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"budget_low": "500.00", "budget_high": "100.00"},
        {"budget_low": "-1"},
        {"budget_low": "1.234"},
        {"budget_high": "12345678901.00"},
        {"title": ""},
        {"title": "x" * 201},
        {"asked_for": "x" * (LONG_TEXT_MAX + 1)},
        {"known_risks": "x" * (LONG_TEXT_MAX + 1)},
        {"problem": "   "},
        {"wanted_by": "next week"},
    ],
)
def test_bad_values_are_422(web, db, pm, changes):
    assert _create(web, **changes).status_code == 422
    assert _audit(db) == []


def test_text_at_the_limits_is_accepted(web, pm):
    body = _create(web, title="t" * 200, asked_for="a" * LONG_TEXT_MAX).json()
    assert len(body["title"]) == 200 and len(body["asked_for"]) == LONG_TEXT_MAX


def test_an_unknown_client_is_422(web, db, pm):
    assert _create(web, client_id=9999).status_code == 422
    assert _audit(db) == []


def test_a_draft_is_edited_submitted_and_withdrawn_with_one_audit_row_each(web, db, pm):
    rid = _ready(web, pm)

    edited = web.patch(f"{URL}/{rid}", json={"version": 1, "title": "Forecast pilot"})
    assert edited.status_code == 200, edited.text
    assert (edited.json()["title"], edited.json()["version"]) == ("Forecast pilot", 2)

    submitted = _move(web, rid, "submit", 2)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "submitted"
    assert submitted.json()["submitted_at"] is not None

    withdrawn = _move(web, rid, "withdraw", 3)
    assert withdrawn.status_code == 200
    assert (withdrawn.json()["status"], withdrawn.json()["version"]) == ("withdrawn", 4)

    rows = _audit(db)
    assert [r.action_taken["action"] for r in rows] == ["create", "update", "submit", "withdraw"]
    assert [r.version for r in rows] == [1, 2, 3, 4]
    assert all(r.actor_user_id == pm.id for r in rows)
    assert rows[1].signals["title"] == BODY["title"]
    assert rows[1].output["title"] == "Forecast pilot"
    assert (rows[2].signals["status"], rows[2].output["status"]) == ("draft", "submitted")
    assert (rows[3].signals["status"], rows[3].output["status"]) == ("submitted", "withdrawn")


def test_a_draft_can_be_withdrawn(web, db, pm):
    rid = _create(web).json()["id"]
    response = _move(web, rid, "withdraw", 1)
    assert response.status_code == 200
    assert response.json()["status"] == "withdrawn"
    assert response.json()["submitted_at"] is None


def test_an_edit_can_clear_optional_fields_and_change_the_client(web, db, pm, client_row):
    rid = _create(web, budget_low="1.00", known_risks="Some").json()["id"]
    body = {"version": 1, "budget_low": None, "known_risks": None, "client_id": client_row.id}
    response = web.patch(f"{URL}/{rid}", json=body)
    assert response.status_code == 200
    assert response.json()["budget_low"] is None
    assert response.json()["known_risks"] is None
    assert response.json()["client_name"] == client_row.name


def test_an_edit_is_checked_against_the_stored_request(web, db, pm):
    rid = _create(web, budget_high="100.00").json()["id"]
    response = web.patch(f"{URL}/{rid}", json={"version": 1, "budget_low": "200.00"})
    assert response.status_code == 422
    assert web.patch(f"{URL}/{rid}", json={"version": 1, "title": None}).status_code == 422
    assert web.patch(f"{URL}/{rid}", json={"version": 1, "client_id": 9999}).status_code == 422
    assert len(_audit(db)) == 1


@pytest.mark.parametrize("field", ["requested_by", "status", "submitted_at", "id"])
def test_an_edit_cannot_set_server_owned_fields(web, db, pm, field):
    rid = _create(web).json()["id"]
    response = web.patch(f"{URL}/{rid}", json={"version": 1, field: "submitted"})
    assert response.status_code == 422
    assert len(_audit(db)) == 1


def test_an_edit_that_changes_nothing_writes_no_audit_row(web, db, pm):
    rid = _create(web, budget_low="10").json()["id"]
    response = web.patch(
        f"{URL}/{rid}", json={"version": 1, "title": BODY["title"], "budget_low": "10.00"}
    )
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert len(_audit(db)) == 1


def test_another_project_manager_may_act_and_the_audit_row_names_them(web, db, pm, sign_in):
    rid = _ready(web, pm)
    other = TestClient(app, base_url="http://localhost")
    colleague = sign_in(other, db, "project_manager", email="colleague@pmo.example")
    assert other.patch(f"{URL}/{rid}", json={"version": 1, "title": "Handed over"}).status_code == (
        200
    )
    assert _move(other, rid, "submit", 2).status_code == 200
    rows = _audit(db)
    assert [r.actor_user_id for r in rows] == [pm.id, colleague.id, colleague.id]
    # The requester stays who created it.
    assert web.get(f"{URL}/{rid}").json()["requested_by"] == pm.id


# --- Refusals


def test_editing_a_submitted_request_is_409(web, db, pm):
    rid = _ready(web, pm)
    _move(web, rid, "submit", 1)
    _refused(web.patch(f"{URL}/{rid}", json={"version": 2, "title": "Late change"}), NOT_DRAFT_EDIT)
    assert len(_audit(db)) == 2


def test_submitting_twice_is_409(web, db, pm):
    rid = _ready(web, pm)
    _move(web, rid, "submit", 1)
    _refused(_move(web, rid, "submit", 2), NOT_DRAFT_SUBMIT)


@pytest.mark.parametrize("final", ["declined", "withdrawn"])
def test_a_final_request_refuses_every_change(web, db, pm, final):
    rid = _create(web).json()["id"]
    version = _set_status(db, rid, final)
    assert web.get(f"{URL}/{rid}").json()["version"] == version
    _refused(web.patch(f"{URL}/{rid}", json={"version": version, "title": "Again"}), FINAL)
    _refused(_move(web, rid, "submit", version), FINAL)
    _refused(_move(web, rid, "withdraw", version), FINAL)
    assert len(_audit(db)) == 1


def test_an_approved_request_cannot_be_edited_submitted_or_withdrawn(web, db, pm):
    rid = _create(web).json()["id"]
    version = _set_status(db, rid, "approved")
    assert web.get(f"{URL}/{rid}").json()["version"] == version
    _refused(web.patch(f"{URL}/{rid}", json={"version": version, "title": "Again"}), NOT_DRAFT_EDIT)
    _refused(_move(web, rid, "submit", version), NOT_DRAFT_SUBMIT)
    _refused(_move(web, rid, "withdraw", version), NOT_WITHDRAWABLE)
    assert len(_audit(db)) == 1


def test_a_stale_version_is_409_and_changes_nothing(web, db, pm):
    rid = _create(web).json()["id"]
    web.patch(f"{URL}/{rid}", json={"version": 1, "title": "First"})
    _refused(web.patch(f"{URL}/{rid}", json={"version": 1, "title": "Second"}), STALE)
    _refused(_move(web, rid, "submit", 1), STALE)
    _refused(_move(web, rid, "withdraw", 1), STALE)
    assert web.get(f"{URL}/{rid}").json()["title"] == "First"
    assert len(_audit(db)) == 2


@pytest.mark.parametrize("body", [{}, {"version": 0}, {"version": 1, "status": "draft"}])
def test_a_transition_needs_only_a_version(web, db, pm, body):
    rid = _create(web).json()["id"]
    assert web.post(f"{URL}/{rid}/submit", json=body).status_code == 422


def test_an_unknown_request_is_404(web, pm):
    assert web.get(f"{URL}/9999").status_code == 404
    assert web.get(f"{URL}/9999/history").status_code == 404
    assert web.patch(f"{URL}/9999", json={"version": 1, "title": "x"}).status_code == 404
    assert _move(web, 9999, "submit", 1).status_code == 404
    assert _move(web, 9999, "withdraw", 1).status_code == 404


def test_no_route_can_approve_decline_or_return_to_draft(web, db, pm):
    # Only submit and withdraw move a request, and no route takes a target status.
    request_routes = {
        (method, route.path)
        for route in iter_route_contexts(app.routes)
        if (route.path or "").startswith("/api/v1/requests")
        for method in route.methods
    }
    assert request_routes == {
        ("GET", "/api/v1/requests"),
        ("POST", "/api/v1/requests"),
        ("GET", "/api/v1/requests/{request_id}"),
        ("GET", "/api/v1/requests/{request_id}/history"),
        ("PATCH", "/api/v1/requests/{request_id}"),
        ("POST", "/api/v1/requests/{request_id}/submit"),
        ("POST", "/api/v1/requests/{request_id}/withdraw"),
        ("GET", "/api/v1/requests/{request_id}/value-case"),
        ("PUT", "/api/v1/requests/{request_id}/value-case"),
        ("GET", "/api/v1/requests/{request_id}/readiness"),
        ("PUT", "/api/v1/requests/{request_id}/readiness"),
    }
    rid = _ready(web, pm)
    _move(web, rid, "submit", 1)
    for action in ("approve", "decline", "return", "draft"):
        assert _move(web, rid, action, 2).status_code in (404, 405)
    assert web.patch(f"{URL}/{rid}", json={"version": 2, "status": "approved"}).status_code == 422
    assert web.get(f"{URL}/{rid}").json()["status"] == "submitted"


@pytest.mark.parametrize("role", ["sponsor", "auditor", "admin", "ethics_reviewer"])
def test_other_roles_read_the_queue_but_cannot_change_it(web, db, pm, sign_in, role):
    rid = _create(web).json()["id"]
    reader = TestClient(app, base_url="http://localhost")
    sign_in(reader, db, role)
    assert reader.get(URL).status_code == 200
    assert reader.get(f"{URL}/{rid}").status_code == 200
    assert reader.get(f"{URL}/{rid}/history").status_code == 200
    assert reader.post(URL, json=BODY).status_code == 403
    assert reader.patch(f"{URL}/{rid}", json={"version": 1, "title": "x"}).status_code == 403
    assert _move(reader, rid, "submit", 1).status_code == 403
    assert _move(reader, rid, "withdraw", 1).status_code == 403
    assert len(_audit(db)) == 1


# --- History


def test_history_is_this_requests_rows_newest_first_with_display_names(web, db, pm):
    rid = _ready(web, pm)
    other_id = _create(web, title="Another").json()["id"]
    _move(web, rid, "submit", 1)
    # An audit row for another subject with the same id is not this request's.
    with Session(engine) as session:
        session.add(
            AuditDecision(
                agent="risk_register",
                trigger="human",
                subject_type="risk",
                subject_id=str(rid),
                status="ok",
            )
        )
        session.commit()

    body = web.get(f"{URL}/{rid}/history").json()
    assert body["total"] == 2
    assert [e["action"] for e in body["items"]] == ["submit", "create"]
    assert all(e["actor_name"] == pm.display_name for e in body["items"])
    assert pm.email not in str(body)
    assert body["items"][1]["before"] is None
    assert body["items"][0]["before"]["status"] == "draft"
    assert body["items"][0]["after"]["status"] == "submitted"

    page = web.get(f"{URL}/{rid}/history?limit=1&offset=1").json()
    assert [e["action"] for e in page["items"]] == ["create"]
    assert web.get(f"{URL}/{other_id}/history").json()["total"] == 1


# --- The queue


def _queue(web: TestClient, query: str = "") -> list[str]:
    response = web.get(f"{URL}{query}")
    assert response.status_code == 200, response.text
    return [item["title"] for item in response.json()["items"]]


def _seed_queue(db: Session, pm: User, client: Client) -> None:
    rows = [
        ("A", "submitted", client.id, True, date(2027, 1, 10), "2026-10-01 09:00"),
        ("B", "submitted", None, False, None, "2026-10-03 09:00"),
        ("C", "draft", client.id, False, date(2026, 12, 1), None),
        ("D", "withdrawn", None, True, date(2027, 1, 10), "2026-10-02 09:00"),
        ("E", "draft", None, False, None, None),
    ]
    for title, status, client_id, uses_ai, wanted_by, submitted_at in rows:
        db.add(
            ProjectRequest(
                **{**BODY, "title": title},
                status=status,
                client_id=client_id,
                uses_ai=uses_ai,
                wanted_by=wanted_by,
                submitted_at=None if submitted_at is None else _dt(submitted_at),
                requested_by=pm.id,
            )
        )
    db.commit()


def _dt(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def test_queue_sorts_by_submitted_date_with_undated_last_both_ways(web, db, pm, client_row):
    _seed_queue(db, pm, client_row)
    assert _queue(web) == ["A", "D", "B", "C", "E"]
    assert _queue(web, "?sort=-submitted_at") == ["B", "D", "A", "C", "E"]


def test_queue_sorts_by_wanted_by_with_undated_last_and_id_tiebreak(web, db, pm, client_row):
    _seed_queue(db, pm, client_row)
    assert _queue(web, "?sort=wanted_by") == ["C", "A", "D", "B", "E"]
    assert _queue(web, "?sort=-wanted_by") == ["A", "D", "C", "B", "E"]


def test_queue_filters(web, db, pm, client_row):
    _seed_queue(db, pm, client_row)
    assert _queue(web, "?status=submitted") == ["A", "B"]
    assert _queue(web, f"?client_id={client_row.id}") == ["A", "C"]
    assert _queue(web, "?uses_ai=true") == ["A", "D"]
    assert _queue(web, "?uses_ai=false&status=draft") == ["C", "E"]


def test_queue_pages_with_a_stable_order(web, db, pm, client_row):
    _seed_queue(db, pm, client_row)
    first = web.get(f"{URL}?limit=2").json()
    second = web.get(f"{URL}?limit=2&offset=2").json()
    third = web.get(f"{URL}?limit=2&offset=4").json()
    assert first["total"] == 5
    titles = [i["title"] for page in (first, second, third) for i in page["items"]]
    assert titles == ["A", "D", "B", "C", "E"]


@pytest.mark.parametrize(
    "query", ["?status=open", "?sort=title", "?limit=0", "?limit=101", "?uses_ai=maybe"]
)
def test_queue_refuses_bad_parameters(web, pm, query):
    assert web.get(f"{URL}{query}").status_code == 422


def test_queue_shows_client_and_requester_names(web, db, pm, client_row):
    _seed_queue(db, pm, client_row)
    items = {i["title"]: i for i in web.get(URL).json()["items"]}
    assert items["A"]["client_name"] == client_row.name
    assert items["B"]["client_name"] is None
    assert items["A"]["requested_by_name"] == pm.display_name
