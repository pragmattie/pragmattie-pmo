"""The value case of a project request (item 4.2)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_intake import BODY, URL, _audit, _create, _move, _refused, _set_status, add_value_case

from app.api.intake import (
    FINAL,
    INCOMPLETE,
    MISSING_BENEFIT,
    MISSING_CONFIRMATION,
    MISSING_COST,
    MISSING_VALUE_CASE,
    NOT_DRAFT_VALUE_CASE,
    STALE,
)
from app.db import engine
from app.main import app
from app.models import Base, User, ValueCase
from app.models.intake import BENEFITS_MAX, LONG_TEXT_MAX, MEASURE_MAX, TARGET_MAX

BENEFIT = {
    "description": "Fewer stock-outs",
    "measure": "Stock-outs a quarter",
    "current_value": "12",
    "target": "6",
    "target_date": "2027-12-31",
}
VALUE_CASE = {
    "benefits": [BENEFIT],
    "cost_low": "10000",
    "cost_high": "25000.5",
    "cost_method": "Two weeks of analysis at the day rate",
    "strategic_fit": "The client's goal of fewer stock-outs",
    "cost_of_not_doing": "Lost sales each quarter",
}


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
def rid(web, pm) -> int:
    return _create(web).json()["id"]


def _put(web: TestClient, request_id: int, version: int, **changes):
    return web.put(
        f"{URL}/{request_id}/value-case", json={**VALUE_CASE, **changes, "version": version}
    )


def _get(web: TestClient, request_id: int):
    return web.get(f"{URL}/{request_id}/value-case")


def _value_case_rows(db: Session):
    return [r for r in _audit(db) if r.action_taken["action"] == "value_case"]


def _missing(response) -> list[str]:
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["message"] == INCOMPLETE
    return detail["missing"]


# --- Create, read and replace


def test_there_is_no_value_case_until_one_is_saved(web, rid):
    assert _get(web, rid).status_code == 404


def test_a_value_case_is_saved_read_and_confirmed_by_the_person(web, db, pm, rid):
    response = _put(web, rid, 1)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"] == rid
    assert body["request_version"] == 2
    assert body["benefits"] == [BENEFIT]
    assert (body["cost_low"], body["cost_high"]) == ("10000.00", "25000.50")
    assert body["strategic_fit"] == VALUE_CASE["strategic_fit"]
    assert body["source"] == "person"
    assert (body["confirmed_by"], body["confirmed_by_name"]) == (pm.id, pm.display_name)
    assert body["confirmed_at"] is not None
    assert _get(web, rid).json() == body
    # The value case is part of the request, so the request's version moves with it.
    assert web.get(f"{URL}/{rid}").json()["version"] == 2


def test_a_put_replaces_the_benefits_and_clears_optional_fields(web, db, pm, rid):
    three = [{**BENEFIT, "description": f"Benefit {n}"} for n in range(3)]
    assert _put(web, rid, 1, benefits=three).status_code == 200
    one = [{"description": "Only this one"}]
    response = _put(web, rid, 2, benefits=one, cost_of_not_doing=None, cost_method=None)
    assert response.status_code == 200, response.text
    body = _get(web, rid).json()
    assert [b["description"] for b in body["benefits"]] == ["Only this one"]
    assert body["benefits"][0]["measure"] is None
    assert body["cost_of_not_doing"] is None and body["cost_method"] is None
    assert body["request_version"] == 3
    reordered = [{**BENEFIT, "description": "Second"}, {**BENEFIT, "description": "First"}]
    assert _put(web, rid, 3, benefits=reordered).status_code == 200
    assert [b["description"] for b in _get(web, rid).json()["benefits"]] == ["Second", "First"]


def test_text_and_benefit_count_at_the_limits_are_accepted(web, rid):
    # Every field at its longest still fits in the API's body limit.
    longest = {
        **BENEFIT,
        "description": "d" * LONG_TEXT_MAX,
        "measure": "m" * MEASURE_MAX,
        "current_value": "c" * TARGET_MAX,
        "target": "t" * TARGET_MAX,
    }
    text = {
        name: name[0] * LONG_TEXT_MAX
        for name in ("cost_method", "strategic_fit", "cost_of_not_doing")
    }
    response = _put(web, rid, 1, benefits=[longest] * BENEFITS_MAX, **text)
    assert response.status_code == 200, response.text
    assert len(response.json()["benefits"]) == BENEFITS_MAX


# --- Validation


@pytest.mark.parametrize(
    "changes",
    [
        {"cost_low": "200.00", "cost_high": "100.00"},
        {"cost_low": "-1.00"},
        {"cost_high": "12.345"},
        {"cost_low": "ten"},
        {"cost_high": "10000000000.00"},
        {"benefits": []},
        {"benefits": [BENEFIT] * (BENEFITS_MAX + 1)},
        {"benefits": [{**BENEFIT, "description": " "}]},
        {"benefits": [{**BENEFIT, "target": "t" * 201}]},
        {"benefits": [{**BENEFIT, "owner": "Pat"}]},
        {"strategic_fit": ""},
        {"strategic_fit": "s" * (LONG_TEXT_MAX + 1)},
        {"source": "agent"},
        {"confirmed_by": 1},
    ],
)
def test_a_bad_value_case_is_422_and_saves_nothing(web, db, pm, rid, changes):
    assert _put(web, rid, 1, **changes).status_code == 422
    assert _get(web, rid).status_code == 404
    assert _value_case_rows(db) == []


def test_a_put_needs_the_requests_version(web, db, pm, rid):
    body = {**VALUE_CASE}
    assert web.put(f"{URL}/{rid}/value-case", json=body).status_code == 422


# --- Following the request's rules


def test_a_stale_version_is_409(web, db, pm, rid):
    web.patch(f"{URL}/{rid}", json={"version": 1, "title": "Renamed"})
    _refused(_put(web, rid, 1), STALE)
    assert _put(web, rid, 2).status_code == 200
    _refused(_put(web, rid, 2, strategic_fit="Changed"), STALE)
    assert len(_value_case_rows(db)) == 1


def test_the_value_case_cannot_change_once_the_request_is_submitted(web, db, pm, rid):
    assert _put(web, rid, 1).status_code == 200
    assert _move(web, rid, "submit", 2).status_code == 200
    _refused(_put(web, rid, 3, strategic_fit="Too late"), NOT_DRAFT_VALUE_CASE)
    # Not even a save that changes nothing.
    _refused(_put(web, rid, 3), NOT_DRAFT_VALUE_CASE)
    assert _get(web, rid).json()["strategic_fit"] == VALUE_CASE["strategic_fit"]
    assert len(_value_case_rows(db)) == 1


def test_an_approved_requests_value_case_cannot_change(web, db, pm, rid):
    version = _set_status(db, rid, "approved")
    _refused(_put(web, rid, version), NOT_DRAFT_VALUE_CASE)


@pytest.mark.parametrize("final", ["withdrawn", "declined"])
def test_a_final_requests_value_case_cannot_change(web, db, pm, rid, final):
    assert _put(web, rid, 1).status_code == 200
    if final == "withdrawn":
        assert _move(web, rid, "withdraw", 2).status_code == 200
        version = 3
    else:
        version = _set_status(db, rid, final)
    _refused(_put(web, rid, version, strategic_fit="Again"), FINAL)
    assert len(_value_case_rows(db)) == 1


def test_an_unknown_request_is_404(web, pm):
    assert _get(web, 9999).status_code == 404
    assert _put(web, 9999, 1).status_code == 404


@pytest.mark.parametrize("role", ["sponsor", "auditor", "admin", "ethics_reviewer"])
def test_other_roles_read_the_value_case_but_cannot_change_it(web, db, pm, rid, sign_in, role):
    assert _put(web, rid, 1).status_code == 200
    reader = TestClient(app, base_url="http://localhost")
    sign_in(reader, db, role)
    assert _get(reader, rid).status_code == 200
    assert _put(reader, rid, 2, strategic_fit="Mine").status_code == 403
    assert len(_value_case_rows(db)) == 1


# --- Audit


def test_each_change_is_one_audit_row_with_before_and_after(web, db, pm, rid):
    _put(web, rid, 1)
    _put(web, rid, 2, cost_high="30000", benefits=[BENEFIT, {"description": "Happier staff"}])
    rows = _value_case_rows(db)
    assert len(rows) == 2
    first, second = rows
    assert first.subject_type == "project_request" and first.subject_id == str(rid)
    assert (first.version, second.version) == (2, 3)
    assert all(r.actor_user_id == pm.id and r.trigger == "human" for r in rows)
    assert first.signals is None
    assert first.output["cost_low"] == "10000.00"
    assert first.output["benefits"] == [BENEFIT]
    assert first.output["confirmed_by"] == pm.id
    assert second.signals == first.output
    assert second.output["cost_high"] == "30000.00"
    assert [b["description"] for b in second.output["benefits"]] == [
        "Fewer stock-outs",
        "Happier staff",
    ]
    history = web.get(f"{URL}/{rid}/history").json()["items"]
    assert [e["action"] for e in history] == ["value_case", "value_case", "create"]
    assert history[0]["before"]["cost_high"] == "25000.50"
    assert history[0]["after"]["cost_high"] == "30000.00"


def test_a_save_that_changes_nothing_on_a_confirmed_value_case_writes_nothing(web, db, pm, rid):
    first = _put(web, rid, 1).json()
    # "10000" and "10000.00" are the same amount.
    response = _put(web, rid, 2, cost_low="10000.00")
    assert response.status_code == 200
    assert response.json() == first
    assert web.get(f"{URL}/{rid}").json()["version"] == 2
    assert len(_value_case_rows(db)) == 1


def test_saving_an_agent_draft_unchanged_confirms_it_with_one_row(web, db, pm, rid):
    add_value_case(rid, None)
    draft = _get(web, rid).json()
    assert (draft["source"], draft["confirmed_by"]) == ("agent", None)
    body = {
        name: draft[name]
        for name in ("benefits", "cost_low", "cost_high", "cost_method", "strategic_fit")
    }
    response = _put(web, rid, draft["request_version"], **body, cost_of_not_doing=None)
    assert response.status_code == 200, response.text
    confirmed = response.json()
    # It stays an agent's draft, now confirmed by a named person.
    assert (confirmed["source"], confirmed["confirmed_by"]) == ("agent", pm.id)
    assert confirmed["benefits"] == draft["benefits"]
    (row,) = _value_case_rows(db)
    assert row.signals["confirmed_by"] is None
    assert row.output["confirmed_by"] == pm.id
    assert {k: v for k, v in row.signals.items() if not k.startswith("confirmed")} == {
        k: v for k, v in row.output.items() if not k.startswith("confirmed")
    }


def test_another_person_editing_becomes_the_one_who_confirmed_it(web, db, pm, rid, sign_in):
    _put(web, rid, 1)
    other = TestClient(app, base_url="http://localhost")
    colleague = sign_in(other, db, "project_manager", email="colleague@pmo.example")
    response = _put(other, rid, 2, strategic_fit="The practice's growth goal")
    assert response.status_code == 200
    assert response.json()["confirmed_by"] == colleague.id
    assert _value_case_rows(db)[-1].actor_user_id == colleague.id


# --- Submitting needs a complete, confirmed value case


def test_a_request_without_a_value_case_cannot_be_submitted(web, db, pm, rid):
    assert _missing(_move(web, rid, "submit", 1)) == [MISSING_VALUE_CASE]
    assert web.get(f"{URL}/{rid}").json()["status"] == "draft"
    assert [r.action_taken["action"] for r in _audit(db)] == ["create"]


@pytest.mark.parametrize(
    "benefits",
    [
        [{"description": "No measure or target"}],
        [{**BENEFIT, "measure": None}],
        [{**BENEFIT, "target": None}],
        [{**BENEFIT, "target": None}, {**BENEFIT, "measure": None}],
    ],
)
def test_a_benefit_needs_a_measure_and_a_target(web, db, pm, rid, benefits):
    _put(web, rid, 1, benefits=benefits)
    assert _missing(_move(web, rid, "submit", 2)) == [MISSING_BENEFIT]


@pytest.mark.parametrize(
    "changes",
    [
        {"cost_low": None},
        {"cost_high": None},
        {"cost_method": None},
        {"cost_low": None, "cost_high": None, "cost_method": None},
    ],
)
def test_a_cost_estimate_is_needed(web, db, pm, rid, changes):
    _put(web, rid, 1, **changes)
    assert _missing(_move(web, rid, "submit", 2)) == [MISSING_COST]


def test_an_unconfirmed_agent_draft_cannot_be_submitted(web, db, pm, rid):
    add_value_case(rid, None, cost_method=None)
    assert _missing(_move(web, rid, "submit", 1)) == [MISSING_COST, MISSING_CONFIRMATION]


def test_everything_missing_is_listed_together(web, db, pm, rid):
    add_value_case(rid, None, cost_low=None)
    with Session(engine) as session:
        value_case = session.query(ValueCase).filter_by(request_id=rid).one()
        value_case.benefits[0].measure = None
        session.commit()
    assert _missing(_move(web, rid, "submit", 1)) == [
        MISSING_BENEFIT,
        MISSING_COST,
        MISSING_CONFIRMATION,
    ]


def test_a_complete_confirmed_value_case_lets_the_request_be_submitted(web, db, pm, rid):
    _put(web, rid, 1, benefits=[{"description": "Vague"}, BENEFIT], cost_of_not_doing=None)
    response = _move(web, rid, "submit", 2)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "submitted"


def test_a_confirmed_agent_draft_lets_the_request_be_submitted(web, db, pm, rid):
    add_value_case(rid, None)
    draft = _get(web, rid).json()
    body = {k: draft[k] for k in ("benefits", "cost_low", "cost_high", "cost_method")}
    _put(web, rid, 1, **body)
    assert _move(web, rid, "submit", 2).status_code == 200


def test_the_request_budget_and_the_cost_estimate_are_not_linked(web, db, pm):
    # The request's budget is what the client has in mind; the estimate is ours.
    rid = _create(web, budget_low="1.00", budget_high="2.00").json()["id"]
    response = _put(web, rid, 1, cost_low="50000.00", cost_high="90000.00")
    assert response.status_code == 200
    assert web.get(f"{URL}/{rid}").json()["budget_high"] == "2.00"
    assert BODY["title"] == web.get(f"{URL}/{rid}").json()["title"]
