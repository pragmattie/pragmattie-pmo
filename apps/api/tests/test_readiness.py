"""The readiness check of a project request that uses AI (item 4.3)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_intake import (
    URL,
    _audit,
    _create,
    _move,
    _refused,
    _set_status,
    add_readiness,
    add_value_case,
)

from app.api.intake import (
    BAD_OWNER,
    FINAL,
    INCOMPLETE,
    MISSING_ACTION,
    MISSING_RATING,
    MISSING_READINESS,
    MISSING_READINESS_CONFIRMATION,
    MISSING_VALUE_CASE,
    NOT_AI,
    NOT_DRAFT_READINESS,
    STALE,
)
from app.db import engine
from app.main import app
from app.models import Base, ReadinessCheck, User
from app.models.intake import ACTION_MAX, ACTIONS_MAX, LONG_TEXT_MAX


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
    """A draft that uses AI, with a complete, confirmed value case so only readiness is tested."""
    request_id = _create(web, uses_ai=True).json()["id"]
    add_value_case(request_id, pm.id)
    return request_id


def _action(owner: int, **changes) -> dict:
    return {
        "description": "Compare with a reorder rule",
        "owner_id": owner,
        "due_date": "2026-12-01",
        **changes,
    }


def _area(rating: str, owner: int | None = None, note: str = "A note", **changes) -> dict:
    actions = [_action(owner)] if owner is not None else []
    return {"rating": rating, "note": note, "actions": actions, **changes}


def _body(owner: int, **areas) -> dict:
    return {
        "problem": _area("green"),
        "approach": _area("yellow", owner),
        "data": _area("green"),
        **areas,
    }


def _put(web: TestClient, request_id: int, version: int, owner: int, **areas):
    return web.put(
        f"{URL}/{request_id}/readiness", json={**_body(owner, **areas), "version": version}
    )


def _get(web: TestClient, request_id: int):
    return web.get(f"{URL}/{request_id}/readiness")


def _rows(db: Session):
    return [r for r in _audit(db) if r.action_taken["action"] == "readiness"]


def _missing(response) -> list[str]:
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["message"] == INCOMPLETE
    return detail["missing"]


# --- The overall result


@pytest.mark.parametrize(
    ("ratings", "overall"),
    [
        (("green", "green", "green"), "green"),
        (("green", "yellow", "green"), "yellow"),
        (("yellow", "yellow", "yellow"), "yellow"),
        (("red", "green", "green"), "red"),
        (("green", "yellow", "red"), "red"),
        (("yellow", "red", "yellow"), "red"),
    ],
)
def test_the_overall_result_is_the_worst_area(web, pm, rid, ratings, overall):
    areas = {
        area: _area(rating, pm.id if rating == "yellow" else None)
        for area, rating in zip(("problem", "approach", "data"), ratings, strict=True)
    }
    response = _put(web, rid, 1, pm.id, **areas)
    assert response.status_code == 200, response.text
    assert response.json()["overall"] == overall
    assert _get(web, rid).json()["overall"] == overall


def test_an_unconfirmed_agent_draft_is_unrated(web, pm, rid):
    add_readiness(rid, None, pm.id, data_rating="red")
    body = _get(web, rid).json()
    assert (body["source"], body["confirmed_by"], body["overall"]) == ("agent", None, "unrated")
    assert body["data"]["rating"] == "red"


def test_a_draft_with_an_area_left_unrated_is_unrated(db, pm, rid):
    add_readiness(rid, pm.id, pm.id, data_rating=None)
    check = db.query(ReadinessCheck).filter_by(request_id=rid).one()
    assert check.overall == "unrated"


# --- Create, read and replace


def test_there_is_no_check_until_one_is_saved(web, rid):
    assert _get(web, rid).status_code == 404


def test_a_check_is_saved_read_and_confirmed_by_the_person(web, db, pm, rid):
    response = _put(web, rid, 1, pm.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"] == rid and body["request_version"] == 2
    assert body["problem"] == {"rating": "green", "note": "A note", "actions": []}
    assert body["approach"]["actions"] == [
        {
            "description": "Compare with a reorder rule",
            "owner_id": pm.id,
            "owner_name": pm.display_name,
            "due_date": "2026-12-01",
        }
    ]
    assert body["source"] == "person"
    assert (body["confirmed_by"], body["confirmed_by_name"]) == (pm.id, pm.display_name)
    assert body["confirmed_at"] is not None
    assert _get(web, rid).json() == body
    assert web.get(f"{URL}/{rid}").json()["version"] == 2


def test_actions_are_allowed_on_any_area_and_replaced_in_order(web, db, pm, rid, sign_in):
    other = sign_in(TestClient(app), db, "sponsor", email="sponsor@pmo.example")
    two = [_action(pm.id, description="First"), _action(other.id, description="Second")]
    red = {"rating": "red", "note": "No data yet", "actions": two}
    green = _area("green", pm.id)
    assert _put(web, rid, 1, pm.id, data=red, problem=green).status_code == 200
    body = _get(web, rid).json()
    assert [a["description"] for a in body["data"]["actions"]] == ["First", "Second"]
    assert body["data"]["actions"][1]["owner_name"] == other.display_name
    assert len(body["problem"]["actions"]) == 1
    swapped = {**red, "actions": [two[1], {**two[0], "description": "Now last"}]}
    assert _put(web, rid, 2, pm.id, data=swapped).status_code == 200
    body = _get(web, rid).json()
    assert [a["description"] for a in body["data"]["actions"]] == ["Second", "Now last"]
    assert body["problem"]["actions"] == []
    assert body["request_version"] == 3


def test_a_past_due_date_is_allowed(web, pm, rid):
    past = {"rating": "yellow", "note": "Late", "actions": [_action(pm.id, due_date="2020-01-01")]}
    response = _put(web, rid, 1, pm.id, approach=past)
    assert response.status_code == 200, response.text
    assert response.json()["approach"]["actions"][0]["due_date"] == "2020-01-01"


def test_text_and_action_count_at_the_limits_are_accepted(web, pm, rid):
    actions = [_action(pm.id, description="a" * ACTION_MAX)] * ACTIONS_MAX
    area = {"rating": "yellow", "note": "n" * LONG_TEXT_MAX, "actions": actions}
    response = _put(web, rid, 1, pm.id, problem=area, approach=area, data=area)
    assert response.status_code == 200, response.text


# --- Validation


# Each is refused by validation, before any owner is looked up, so the owner id is arbitrary.
ANY = 1


@pytest.mark.parametrize(
    "changes",
    [
        {"approach": _area("yellow")},
        {"approach": _area("amber")},
        {"approach": _area("green", note="")},
        {"approach": _area("green", note="n" * (LONG_TEXT_MAX + 1))},
        {"approach": {"rating": "green", "note": "No actions field"}},
        {"approach": {"rating": "green", "actions": []}},
        {"approach": _area("yellow", actions=[_action(ANY, description=" ")])},
        {"approach": _area("yellow", actions=[_action(ANY, description="d" * (ACTION_MAX + 1))])},
        {
            "approach": _area(
                "yellow", actions=[{"description": "No owner", "due_date": "2026-12-01"}]
            )
        },
        {"approach": _area("yellow", actions=[{"description": "No date", "owner_id": ANY}])},
        {"approach": _area("yellow", actions=[_action(ANY, due_date="soon")])},
        {"approach": _area("yellow", actions=[_action(ANY)] * (ACTIONS_MAX + 1))},
        {"approach": _area("green", ANY, colour="green")},
        {"source": "agent"},
        {"confirmed_by": 1},
        {"overall": "green"},
    ],
)
def test_a_bad_check_is_422_and_saves_nothing(web, db, pm, rid, changes):
    assert _put(web, rid, 1, pm.id, **changes).status_code == 422
    assert _get(web, rid).status_code == 404
    assert _rows(db) == []


def test_every_area_is_required(web, db, pm, rid):
    body = {**_body(pm.id), "version": 1}
    del body["data"]
    assert web.put(f"{URL}/{rid}/readiness", json=body).status_code == 422
    assert web.put(f"{URL}/{rid}/readiness", json=_body(pm.id)).status_code == 422


def test_an_unknown_or_inactive_owner_is_422(web, db, pm, rid, sign_in):
    response = _put(web, rid, 1, pm.id, approach=_area("yellow", 9999))
    assert response.status_code == 422
    assert response.json()["detail"] == BAD_OWNER
    gone = sign_in(TestClient(app), db, "sponsor", email="gone@pmo.example")
    gone.is_active = False
    db.commit()
    assert _put(web, rid, 1, pm.id, data=_area("red", gone.id)).status_code == 422
    assert _rows(db) == []


# --- Only for a request that uses AI


def test_a_request_without_uses_ai_has_no_check(web, db, pm):
    plain = _create(web).json()["id"]
    response = _get(web, plain)
    assert response.status_code == 404 and response.json()["detail"] == NOT_AI
    assert _put(web, plain, 1, pm.id).status_code == 404
    assert _rows(db) == []


def test_turning_uses_ai_off_hides_the_check_and_on_brings_it_back(web, db, pm, rid):
    assert _put(web, rid, 1, pm.id).status_code == 200
    assert web.patch(f"{URL}/{rid}", json={"version": 2, "uses_ai": False}).status_code == 200
    assert _get(web, rid).status_code == 404
    assert db.query(ReadinessCheck).filter_by(request_id=rid).count() == 1
    assert web.patch(f"{URL}/{rid}", json={"version": 3, "uses_ai": True}).status_code == 200
    assert _get(web, rid).json()["approach"]["rating"] == "yellow"


def test_an_unknown_request_is_404(web, pm):
    assert _get(web, 9999).status_code == 404
    assert _put(web, 9999, 1, pm.id).status_code == 404


# --- Following the request's rules


def test_a_stale_version_is_409(web, db, pm, rid):
    web.patch(f"{URL}/{rid}", json={"version": 1, "title": "Renamed"})
    _refused(_put(web, rid, 1, pm.id), STALE)
    assert _put(web, rid, 2, pm.id).status_code == 200
    _refused(_put(web, rid, 2, pm.id, data=_area("red")), STALE)
    assert len(_rows(db)) == 1


def test_the_check_cannot_change_once_the_request_is_submitted(web, db, pm, rid):
    assert _put(web, rid, 1, pm.id).status_code == 200
    assert _move(web, rid, "submit", 2).status_code == 200
    _refused(_put(web, rid, 3, pm.id, data=_area("red")), NOT_DRAFT_READINESS)
    _refused(_put(web, rid, 3, pm.id), NOT_DRAFT_READINESS)
    assert len(_rows(db)) == 1


def test_an_approved_requests_check_cannot_change(web, db, pm, rid):
    version = _set_status(db, rid, "approved")
    _refused(_put(web, rid, version, pm.id), NOT_DRAFT_READINESS)


def test_a_withdrawn_requests_check_cannot_change(web, db, pm, rid):
    assert _move(web, rid, "withdraw", 1).status_code == 200
    _refused(_put(web, rid, 2, pm.id), FINAL)


@pytest.mark.parametrize("role", ["sponsor", "auditor", "admin", "ethics_reviewer"])
def test_other_roles_read_the_check_but_cannot_change_it(web, db, pm, rid, sign_in, role):
    assert _put(web, rid, 1, pm.id).status_code == 200
    reader = TestClient(app, base_url="http://localhost")
    sign_in(reader, db, role)
    assert _get(reader, rid).status_code == 200
    assert _put(reader, rid, 2, pm.id, data=_area("red")).status_code == 403
    assert len(_rows(db)) == 1


# --- Audit


def test_each_change_is_one_audit_row_with_before_and_after(web, db, pm, rid):
    _put(web, rid, 1, pm.id)
    _put(web, rid, 2, pm.id, data=_area("red", note="Data is thin"))
    first, second = _rows(db)
    assert first.subject_type == "project_request" and first.subject_id == str(rid)
    assert (first.version, second.version) == (2, 3)
    assert all(r.actor_user_id == pm.id and r.trigger == "human" for r in (first, second))
    assert first.signals is None
    assert first.output["overall"] == "yellow"
    assert first.output["approach"]["actions"] == [
        {"description": "Compare with a reorder rule", "owner_id": pm.id, "due_date": "2026-12-01"}
    ]
    assert first.output["confirmed_by"] == pm.id
    assert second.signals == first.output
    assert second.output["overall"] == "red"
    assert second.output["data"] == {"rating": "red", "note": "Data is thin", "actions": []}
    history = web.get(f"{URL}/{rid}/history").json()["items"]
    assert [e["action"] for e in history] == ["readiness", "readiness", "create"]


def test_a_save_that_changes_nothing_writes_nothing(web, db, pm, rid):
    first = _put(web, rid, 1, pm.id).json()
    response = _put(web, rid, 2, pm.id)
    assert response.status_code == 200
    assert response.json() == first
    assert web.get(f"{URL}/{rid}").json()["version"] == 2
    assert len(_rows(db)) == 1


def test_saving_an_agent_draft_unchanged_confirms_it_with_one_row(web, db, pm, rid):
    add_readiness(rid, None, pm.id)
    draft = _get(web, rid).json()
    body = {
        area: {
            "rating": draft[area]["rating"],
            "note": draft[area]["note"],
            "actions": [
                {k: a[k] for k in ("description", "owner_id", "due_date")}
                for a in draft[area]["actions"]
            ],
        }
        for area in ("problem", "approach", "data")
    }
    response = web.put(f"{URL}/{rid}/readiness", json={**body, "version": draft["request_version"]})
    assert response.status_code == 200, response.text
    confirmed = response.json()
    assert (confirmed["source"], confirmed["confirmed_by"]) == ("agent", pm.id)
    assert confirmed["overall"] == "yellow"
    (row,) = _rows(db)
    assert (row.signals["overall"], row.output["overall"]) == ("unrated", "yellow")
    assert row.signals["confirmed_by"] is None and row.output["confirmed_by"] == pm.id


# --- Submitting an AI request needs a complete, confirmed check


def test_an_ai_request_without_a_check_cannot_be_submitted(web, db, pm, rid):
    assert _missing(_move(web, rid, "submit", 1)) == [MISSING_READINESS]
    assert web.get(f"{URL}/{rid}").json()["status"] == "draft"


def test_value_case_and_readiness_gaps_are_listed_together(web, pm):
    bare = _create(web, uses_ai=True).json()["id"]
    assert _missing(_move(web, bare, "submit", 1)) == [MISSING_VALUE_CASE, MISSING_READINESS]


def test_an_unconfirmed_incomplete_draft_lists_every_gap(web, db, pm, rid):
    add_readiness(rid, None, pm.id, problem_rating=None, data_rating="yellow")
    assert _missing(_move(web, rid, "submit", 1)) == [
        MISSING_RATING,
        MISSING_ACTION,
        MISSING_READINESS_CONFIRMATION,
    ]


def test_a_confirmed_check_lets_the_request_be_submitted(web, db, pm, rid):
    assert _missing(_move(web, rid, "submit", 1)) == [MISSING_READINESS]
    assert _put(web, rid, 1, pm.id).status_code == 200
    response = _move(web, rid, "submit", 2)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "submitted"


def test_a_red_result_does_not_stop_submission(web, db, pm, rid):
    assert _put(web, rid, 1, pm.id, data=_area("red")).json()["overall"] == "red"
    assert _move(web, rid, "submit", 2).status_code == 200


def test_a_request_without_uses_ai_needs_no_check_whatever_rows_exist(web, db, pm):
    plain = _create(web).json()["id"]
    add_value_case(plain, pm.id)
    add_readiness(plain, None, pm.id, data_rating=None)
    assert _move(web, plain, "submit", 1).status_code == 200


def test_the_check_model_rates_the_worst_area_directly(db, pm, rid):
    add_readiness(rid, pm.id, pm.id, problem_rating="red")
    check = db.query(ReadinessCheck).filter_by(request_id=rid).one()
    assert check.overall == "red"
    assert [a.due_date for a in check.area_actions("approach")] == [date(2026, 12, 1)]
