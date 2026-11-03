"""The approval gate (item 4.4): its criteria, a sponsor's decision, and every refusal audited."""

import hashlib

import pytest
from fastapi.testclient import TestClient
from policy_files import gates_file
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_intake import URL, _create, _move, _set_status, add_readiness, add_value_case

from app.api.gates import GATES_UNAVAILABLE, NOT_SUBMITTED, UNMET
from app.api.intake import FINAL, STALE
from app.authz import FORBIDDEN
from app.db import engine
from app.main import app
from app.models import AuditDecision, Base, GateDecision, ProjectRequest, ReadinessCheck, User
from app.models.gate import MIN_REASON
from app.security import CSRF_COOKIE

REASON = "The forecast pays for itself within a year"
TICKS = [
    {"id": "value_justifies_cost", "met": True, "note": "Payback inside a year"},
    {"id": "people_available", "met": True, "note": "Two analysts are free from January"},
]


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


@pytest.fixture
def web(db, gates_policy) -> TestClient:
    """The project manager's client."""
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def pm(web, db, sign_in) -> User:
    return sign_in(web, db, "project_manager")


def _client(db, sign_in, role: str, email: str | None = None) -> tuple[TestClient, User]:
    other = TestClient(app, base_url="http://localhost")
    return other, sign_in(other, db, role, email)


@pytest.fixture
def sponsor(db, sign_in, pm) -> TestClient:
    return _client(db, sign_in, "sponsor")[0]


def _submitted(web: TestClient, pm: User, uses_ai: bool = False, **readiness) -> int:
    """A submitted request (version 2) with a confirmed value case, and a confirmed readiness
    check if it uses AI (problem green, approach yellow with an owned action, data green)."""
    rid = _create(web, uses_ai=uses_ai).json()["id"]
    add_value_case(rid, pm.id)
    if uses_ai:
        add_readiness(rid, pm.id, pm.id, **readiness)
    assert _move(web, rid, "submit", 1).status_code == 200
    return rid


def _decide(client: TestClient, rid: int, outcome: str = "pass", **changes):
    body = {"version": 2, "outcome": outcome, "reason": REASON, "criteria": TICKS, **changes}
    return client.post(f"{URL}/{rid}/gate", json=body)


def _gate_rows(db: Session, rid: int) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(
        AuditDecision.agent == "gate", AuditDecision.subject_id == str(rid)
    )
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def _decisions(db: Session) -> list[GateDecision]:
    db.expire_all()
    return list(db.scalars(select(GateDecision).order_by(GateDecision.id)))


def _status(db: Session, rid: int) -> str:
    db.expire_all()
    return db.get(ProjectRequest, rid).status


def _unmet_ids(response) -> list[str]:
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["message"] == UNMET
    return [c["id"] for c in response.json()["detail"]["unmet"]]


# --- Reading the gate


def test_get_shows_the_gate_and_each_criterion(web, pm):
    rid = _submitted(web, pm)
    body = web.get(f"{URL}/{rid}/gate").json()
    assert body["gate"] == {
        "id": "approval",
        "name": "Approval gate",
        "from_stage": "intake",
        "to_stage": "planning",
        "applies_to": "request",
        "deciders": ["sponsor"],
    }
    assert body["request_status"] == "submitted"
    assert body["request_version"] == 2
    assert body["requested_by"] == pm.id
    assert body["checks_met"] is True
    assert body["latest_decision"] is None
    assert body["policy_version"] == hashlib.sha256(gates_file().read_bytes()).hexdigest()
    results = {c["id"]: (c["kind"], c["met"]) for c in body["criteria"]}
    assert results == {
        "value_case_confirmed": ("check", True),
        "readiness_confirmed": ("check", True),
        "readiness_not_red": ("check", True),
        "yellow_actions_owned": ("check", True),
        # Person criteria are ticked only with a decision, so they show unticked.
        "value_justifies_cost": ("person", None),
        "people_available": ("person", None),
    }
    readiness = next(c for c in body["criteria"] if c["id"] == "readiness_not_red")
    assert readiness["detail"] == "not needed: this request doesn't use AI"


def test_get_shows_a_red_readiness_area(web, pm):
    rid = _submitted(web, pm, uses_ai=True, data_rating="red")
    body = web.get(f"{URL}/{rid}/gate").json()
    red = next(c for c in body["criteria"] if c["id"] == "readiness_not_red")
    assert (red["met"], red["detail"]) == (False, "rated red: data")
    assert body["checks_met"] is False


@pytest.mark.parametrize("role", ["sponsor", "project_manager", "auditor", "ethics_reviewer"])
def test_every_reader_sees_the_gate(web, db, sign_in, pm, role):
    rid = _submitted(web, pm)
    reader, _ = _client(db, sign_in, role, f"reader.{role}@pmo.example")
    assert reader.get(f"{URL}/{rid}/gate").status_code == 200


def test_an_unknown_request_is_404(web, sponsor, pm):
    assert web.get(f"{URL}/9999/gate").status_code == 404
    assert _decide(sponsor, 9999).status_code == 404


# --- Passing


def test_a_sponsor_passes_a_request_when_every_criterion_is_met(web, db, pm, sponsor):
    rid = _submitted(web, pm)
    response = _decide(sponsor, rid)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_status"] == "approved"
    assert body["request_version"] == 3
    assert body["latest_decision"]["outcome"] == "pass"
    assert body["latest_decision"]["decided_by_name"] == "Sponsor"
    assert _status(db, rid) == "approved"

    [decision] = _decisions(db)
    assert (decision.gate_id, decision.subject_type, decision.subject_id) == (
        "approval",
        "request",
        rid,
    )
    assert decision.reason == REASON
    assert decision.policy_version == hashlib.sha256(gates_file().read_bytes()).hexdigest()
    [row] = _gate_rows(db, rid)
    assert (row.status, row.trigger, row.version) == ("ok", "human", 3)
    assert row.action_taken["action"] == "gate_pass"
    assert row.action_taken["decision_id"] == decision.id
    assert row.signals["status"] == "submitted"
    assert row.output["status"] == "approved"


def test_an_ai_request_passes_with_green_and_owned_yellow(web, db, pm, sponsor):
    rid = _submitted(web, pm, uses_ai=True)
    assert _decide(sponsor, rid).status_code == 200
    assert _status(db, rid) == "approved"


def test_a_red_readiness_check_blocks_pass_with_a_409_naming_it(web, db, pm, sponsor):
    rid = _submitted(web, pm, uses_ai=True, approach_rating="red")
    response = _decide(sponsor, rid)
    assert _unmet_ids(response) == ["readiness_not_red"]
    [unmet] = response.json()["detail"]["unmet"]
    assert unmet["text"] == "For a request that uses AI, no readiness area is rated red"
    assert unmet["detail"] == "rated red: approach"
    assert _status(db, rid) == "submitted"
    assert _decisions(db) == []
    [row] = _gate_rows(db, rid)
    assert (row.status, row.version) == ("rejected", 2)
    assert row.action_taken["refusal"] == "unmet_criteria"
    assert [c["id"] for c in row.action_taken["unmet"]] == ["readiness_not_red"]
    assert row.output["status"] == "submitted"


def test_an_unowned_yellow_area_blocks_pass(web, db, pm, sponsor):
    rid = _submitted(web, pm, uses_ai=True)
    pm.is_active = False  # the action's owner leaves
    db.commit()
    assert _unmet_ids(_decide(sponsor, rid)) == ["yellow_actions_owned"]


@pytest.mark.parametrize(
    ("ticks", "unmet"),
    [
        ([], ["value_justifies_cost", "people_available"]),
        ([TICKS[0]], ["people_available"]),
        ([TICKS[0], {"id": "people_available", "met": False}], ["people_available"]),
    ],
)
def test_an_unticked_person_criterion_blocks_pass(web, db, pm, sponsor, ticks, unmet):
    rid = _submitted(web, pm)
    assert _unmet_ids(_decide(sponsor, rid, criteria=ticks)) == unmet
    assert _status(db, rid) == "submitted"
    assert len(_gate_rows(db, rid)) == 1


def test_repeated_refusals_are_each_one_row(web, db, pm, sponsor):
    rid = _submitted(web, pm)
    for _ in range(3):
        assert _decide(sponsor, rid, criteria=[]).status_code == 409
    rows = _gate_rows(db, rid)
    assert [(r.version, r.attempt) for r in rows] == [(2, 1), (2, 2), (2, 3)]
    # Then a pass with the ticks still works.
    assert _decide(sponsor, rid).status_code == 200


def test_a_tick_needs_a_note(web, pm, sponsor):
    rid = _submitted(web, pm)
    bad = [{"id": "value_justifies_cost", "met": True}, TICKS[1]]
    assert _decide(sponsor, rid, criteria=bad).status_code == 422


@pytest.mark.parametrize("criterion", ["value_case_confirmed", "made_up"])
def test_only_person_criteria_can_be_ticked(web, pm, sponsor, criterion):
    rid = _submitted(web, pm)
    ticks = [*TICKS, {"id": criterion, "met": True, "note": "Looks fine"}]
    response = _decide(sponsor, rid, criteria=ticks)
    assert response.status_code == 422
    assert criterion in response.json()["detail"]


def test_a_criterion_is_answered_once(web, pm, sponsor):
    rid = _submitted(web, pm)
    assert _decide(sponsor, rid, criteria=[TICKS[0], TICKS[0]]).status_code == 422


# --- Return and decline


def test_return_sends_the_request_back_to_draft(web, db, pm, sponsor):
    rid = _submitted(web, pm, uses_ai=True, data_rating="red")
    # A return doesn't need the criteria met, or the ticks.
    response = _decide(sponsor, rid, "return", criteria=[])
    assert response.status_code == 200, response.text
    assert _status(db, rid) == "draft"
    [decision] = _decisions(db)
    assert decision.outcome == "return"
    # The project manager can work on it again and resubmit it.
    assert web.patch(f"{URL}/{rid}", json={"version": 3, "title": "Revised"}).status_code == 200
    assert _move(web, rid, "submit", 4).status_code == 200


def test_decline_is_final(web, db, pm, sponsor):
    rid = _submitted(web, pm)
    assert _decide(sponsor, rid, "decline", criteria=[]).status_code == 200
    assert _status(db, rid) == "declined"
    again = _decide(sponsor, rid, "pass", version=3)
    assert again.status_code == 409
    assert again.json()["detail"] == FINAL
    assert _move(web, rid, "withdraw", 3).json()["detail"] == FINAL


def test_each_decision_stores_the_criteria_as_they_were(web, db, pm, sponsor):
    rid = _submitted(web, pm, uses_ai=True, data_rating="red")
    note = {"id": "value_justifies_cost", "met": False, "note": "Not until the data is fixed"}
    assert _decide(sponsor, rid, "return", criteria=[note]).status_code == 200
    [decision] = _decisions(db)
    stored = {c["id"]: c for c in decision.criteria}
    assert stored["readiness_not_red"]["met"] is False
    assert stored["readiness_not_red"]["detail"] == "rated red: data"
    assert stored["value_case_confirmed"]["met"] is True
    assert (stored["value_justifies_cost"]["met"], stored["value_justifies_cost"]["note"]) == (
        False,
        "Not until the data is fixed",
    )
    assert stored["people_available"]["met"] is False
    # Fixing the data later changes the gate's results now, not what was recorded.
    with Session(engine) as session:
        check = session.scalars(select(ReadinessCheck).where(ReadinessCheck.request_id == rid))
        check.one().data_rating = "green"
        session.commit()
    now = {c["id"]: c["met"] for c in web.get(f"{URL}/{rid}/gate").json()["criteria"]}
    assert now["readiness_not_red"] is True
    assert {c["id"]: c for c in _decisions(db)[0].criteria} == stored
    [row] = _gate_rows(db, rid)
    assert row.action_taken["criteria"] == decision.criteria


# --- Refusals


@pytest.mark.parametrize("role", ["project_manager", "auditor", "admin"])
def test_roles_without_gate_decide_are_refused_and_audited(web, db, sign_in, pm, role):
    rid = _submitted(web, pm)
    other, user = _client(db, sign_in, role, f"other.{role}@pmo.example")
    response = _decide(other, rid)
    assert response.status_code == 403
    assert response.json()["detail"] == FORBIDDEN
    [row] = _gate_rows(db, rid)
    assert (row.status, row.actor_user_id, row.version) == ("rejected", user.id, 2)
    assert row.action_taken["refusal"] == "no_permission"
    assert row.action_taken["actor_role"] == role
    assert _status(db, rid) == "submitted"
    assert _decisions(db) == []


def test_an_ethics_reviewer_is_refused_by_the_gates_role_list(web, db, sign_in, pm):
    rid = _submitted(web, pm)
    reviewer, user = _client(db, sign_in, "ethics_reviewer")
    response = _decide(reviewer, rid)
    assert response.status_code == 403
    assert response.json()["detail"] == "the approval gate is decided only by: sponsor"
    [row] = _gate_rows(db, rid)
    assert (row.status, row.actor_user_id) == ("rejected", user.id)
    assert row.action_taken["refusal"] == "role_not_a_decider"
    assert row.action_taken["outcome"] == "pass"
    assert _status(db, rid) == "submitted"


def test_a_sponsor_cannot_decide_their_own_submission(web, db, sign_in, pm):
    # A request the sponsor raised (put straight into the database: sponsors can't create one).
    sponsor_client, sponsor = _client(db, sign_in, "sponsor")
    rid = _submitted(web, pm)
    with Session(engine) as session:
        session.get(ProjectRequest, rid).requested_by = sponsor.id
        session.commit()
    response = _decide(sponsor_client, rid, version=3)
    assert response.status_code == 403
    assert response.json()["detail"] == "you cannot decide on your own submission"
    [row] = _gate_rows(db, rid)
    assert row.action_taken["refusal"] == "own_submission"
    assert _status(db, rid) == "submitted"


def test_an_unknown_request_leaves_no_refusal_row(web, db, sign_in, pm):
    other, _ = _client(db, sign_in, "auditor", "other.auditor@pmo.example")
    assert _decide(other, 9999).status_code == 403
    db.expire_all()
    assert list(db.scalars(select(AuditDecision).where(AuditDecision.agent == "gate"))) == []


def test_nobody_signed_in_is_401_without_a_row(web, db, pm):
    rid = _submitted(web, pm)
    anonymous = TestClient(app, base_url="http://localhost")
    anonymous.cookies.set(CSRF_COOKIE, "x")
    anonymous.headers["X-CSRF-Token"] = "x"
    assert _decide(anonymous, rid).status_code == 401
    assert _gate_rows(db, rid) == []


@pytest.mark.parametrize("status", ["draft", "approved"])
def test_only_a_submitted_request_is_decided(web, db, pm, sponsor, status):
    rid = _create(web).json()["id"]
    version = _set_status(db, rid, status)
    response = _decide(sponsor, rid, version=version)
    assert response.status_code == 409
    assert response.json()["detail"] == NOT_SUBMITTED


def test_a_stale_version_is_a_409(web, db, pm, sponsor):
    rid = _submitted(web, pm)
    response = _decide(sponsor, rid, version=1)
    assert response.status_code == 409
    assert response.json()["detail"] == STALE
    assert _status(db, rid) == "submitted"


def test_a_short_reason_is_refused(web, db, pm, sponsor):
    rid = _submitted(web, pm)
    response = _decide(sponsor, rid, reason="x" * (MIN_REASON - 1))
    assert response.status_code == 422
    assert _decide(sponsor, rid, reason="   " + "x" * (MIN_REASON - 1) + "   ").status_code == 422
    assert _status(db, rid) == "submitted"


@pytest.mark.parametrize("outcome", ["approve", "draft", ""])
def test_an_unknown_outcome_is_refused(web, pm, sponsor, outcome):
    rid = _submitted(web, pm)
    assert _decide(sponsor, rid, outcome).status_code == 422


def test_the_history_shows_the_decision(web, pm, sponsor):
    rid = _submitted(web, pm)
    _decide(sponsor, rid, criteria=[])
    _decide(sponsor, rid)
    actions = [e["action"] for e in web.get(f"{URL}/{rid}/history").json()["items"]]
    assert actions == ["gate_pass", "gate_refused", "submit", "create"]


# --- Before the gates file is applied


@pytest.fixture
def placeholder(tmp_path, monkeypatch):
    """The live placeholder gates file: no gate defined yet."""
    from policy_files import APPLIED_POLICIES

    from app.config import get_settings
    from app.policy import get_policies

    monkeypatch.setattr(get_settings(), "policies_dir", APPLIED_POLICIES)
    get_policies.cache_clear()
    yield
    get_policies.cache_clear()


def test_the_routes_answer_503_until_the_gates_file_is_applied(web, db, pm, sponsor, placeholder):
    from app.policy import get_policies

    if get_policies().gates.gates:
        pytest.skip("the gates file has been applied")
    rid = _submitted(web, pm)
    for response in (web.get(f"{URL}/{rid}/gate"), _decide(sponsor, rid)):
        assert response.status_code == 503
        assert response.json()["detail"] == GATES_UNAVAILABLE
    assert _status(db, rid) == "submitted"


def test_the_code_implements_exactly_the_checks_the_loader_knows():
    from app.gates import CHECKS
    from app.policy import KNOWN_CHECKS

    assert tuple(CHECKS) == KNOWN_CHECKS
