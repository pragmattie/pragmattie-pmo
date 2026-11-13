"""The autonomy policy (item 6.1): the loader, the floors, the off switches, the R9 rule for
`score_risk`, and the read-only route."""

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from policy_files import autonomy_dir
from sqlalchemy.orm import Session

from app import policy
from app.authz import Action
from app.autonomy import (
    AGENTS,
    FLAG_WHO,
    FLOOR_STEPS,
    STEP_AGENTS,
    STEPS,
    effective_mode,
    who_permission,
)
from app.db import engine
from app.main import app
from app.models import Base
from app.policy import PolicyError, load_autonomy, parse_autonomy


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


def _proposed() -> dict:
    return yaml.safe_load((autonomy_dir() / "autonomy.yaml").read_text())


def _with(change) -> dict:
    data = copy.deepcopy(_proposed())
    change(data)
    return data


def _refused(data: dict, message: str) -> None:
    with pytest.raises(PolicyError, match="autonomy.yaml") as raised:
        parse_autonomy(data)
    assert message in str(raised.value)


# --- The proposed file


def test_the_loader_accepts_the_proposed_file():
    loaded = load_autonomy(autonomy_dir())
    assert tuple(loaded.steps) == STEPS
    raw = (autonomy_dir() / "autonomy.yaml").read_bytes()
    assert loaded.digest == hashlib.sha256(raw).hexdigest()
    steps = loaded.steps
    for step in ("draft_value_case", "draft_readiness", "draft_plan", "draft_proposal"):
        assert steps[step].mode == "agent"
    for step in ("score_risk", "open_low_risk", "submit_proposal"):
        assert steps[step].mode == "window"
        assert steps[step].window_minutes == 15
        assert steps[step].who == "project_manager"
    assert steps["client_answer"].mode == "agent"
    assert steps["client_answer"].agent == "simulated_client"
    for step in FLOOR_STEPS:
        assert steps[step].mode == "person"
    assert dict(loaded.enabled) == dict.fromkeys(AGENTS, True)


APPLIED = Path(__file__).resolve().parents[3] / "policies"


def test_the_simulated_clients_answer_is_optional_and_defaults_to_accepted():
    """Item 6.8: `agents.simulated_client.answer` is optional."""
    assert load_autonomy(autonomy_dir()).simulated_answer == "accepted"

    def declined(data):
        data["agents"]["simulated_client"]["answer"] = "declined"

    assert parse_autonomy(_with(declined)).simulated_answer == "declined"

    def missing(data):
        data["agents"]["simulated_client"].pop("answer", None)

    assert parse_autonomy(_with(missing)).simulated_answer == "accepted"


@pytest.mark.parametrize("value", ["maybe", "", None, True, ["accepted"]])
def test_a_bad_simulated_answer_is_refused(value):
    def bad(data):
        data["agents"]["simulated_client"]["answer"] = value

    _refused(_with(bad), "agents.simulated_client.answer")


def test_only_the_simulated_client_has_an_answer():
    def misplaced(data):
        data["agents"]["risk_agent"]["answer"] = "accepted"

    _refused(_with(misplaced), "unknown keys: answer")


def test_the_proposed_file_is_todays_file_plus_client_acceptance():
    """Item 7.6: the draft changes nothing in today's file; it adds the `client_acceptance` step
    and the simulated client's `acceptance_answer`, drafted as `accepted`."""
    proposed = Path(__file__).resolve().parents[3] / "ci/proposed/policies/autonomy.yaml"
    if not proposed.exists():
        pytest.skip("the proposed autonomy file has been applied")
    today = yaml.safe_load((APPLIED / "autonomy.yaml").read_text())
    draft = yaml.safe_load(proposed.read_text())
    assert draft["version"] == today["version"]
    assert draft["steps"] == {
        **today["steps"],
        "client_acceptance": {
            "mode": "agent",
            "agent": "simulated_client",
            "who": "project_manager",
        },
    }
    agents = copy.deepcopy(today["agents"])
    agents["simulated_client"]["acceptance_answer"] = "accepted"
    assert draft["agents"] == agents


def test_the_acceptance_answer_is_drafted_as_accepted_and_may_be_not_accepted():
    """Item 7.6: `agents.simulated_client.acceptance_answer`, kept apart from `answer`."""
    loaded = load_autonomy(autonomy_dir())
    assert loaded.acceptance_answer == "accepted"
    assert loaded.steps["client_acceptance"].agent == "simulated_client"
    assert loaded.steps["client_acceptance"].who == "project_manager"

    def not_accepted(data):
        data["agents"]["simulated_client"]["acceptance_answer"] = "not_accepted"

    found = parse_autonomy(_with(not_accepted))
    assert (found.acceptance_answer, found.simulated_answer) == ("not_accepted", "accepted")


@pytest.mark.parametrize("value", ["declined", "maybe", "", None, True, ["accepted"]])
def test_a_bad_acceptance_answer_is_refused(value):
    def bad(data):
        data["agents"]["simulated_client"]["acceptance_answer"] = value

    _refused(_with(bad), "agents.simulated_client.acceptance_answer")


def test_only_the_simulated_client_answers_an_offered_deliverable():
    def other_agent(data):
        data["steps"]["client_acceptance"]["agent"] = "proposal_agent"

    _refused(_with(other_agent), "proposal_agent can't do client_acceptance")

    def misplaced(data):
        data["agents"]["risk_agent"]["acceptance_answer"] = "accepted"

    _refused(_with(misplaced), "unknown keys: acceptance_answer")


def test_the_live_file_without_client_acceptance_is_refused():
    """Until a person applies the draft, the live loader refuses the file (a 503 for the
    orchestrator and the Inbox, as for 7.5)."""
    if (APPLIED / "autonomy.yaml").read_text().count("client_acceptance:"):
        pytest.skip("the proposed autonomy file has been applied")
    with pytest.raises(PolicyError, match="missing steps: client_acceptance"):
        load_autonomy(APPLIED)


def test_floor_steps_are_fixed_in_code():
    assert FLOOR_STEPS == {
        "approve_proposal",
        "send_proposal",
        "accept_risk",
        "pass_gate",
        "decide_change_sponsor",
        "publish_status_report",
    }


def test_report_flags_are_person_steps_with_a_fixed_who():
    assert FLAG_WHO == {
        "report_overdue_pm": "project_manager",
        "report_overdue_sponsor": "sponsor",
        "report_unread": "sponsor",
    }
    assert all(STEP_AGENTS[step] == () for step in FLAG_WHO)
    loaded = load_autonomy(autonomy_dir())
    for step, who in FLAG_WHO.items():
        assert loaded.steps[step].mode == "person"
        assert loaded.steps[step].who == who


@pytest.mark.parametrize("step", sorted(FLAG_WHO))
@pytest.mark.parametrize("mode", ["agent", "window"])
def test_a_report_flag_below_person_is_refused(step, mode):
    def change(data):
        data["steps"][step] = {
            "mode": mode,
            "agent": "status_report_agent",
            "who": FLAG_WHO[step],
            **({"window_minutes": 15} if mode == "window" else {}),
        }

    _refused(_with(change), f"{step} must be decided by a person (mode person), not {mode}")


@pytest.mark.parametrize(
    ("step", "role"),
    [
        ("report_overdue_sponsor", "project_manager"),
        ("report_unread", "project_manager"),
        ("report_unread", "ethics_reviewer"),
    ],
)
def test_a_report_flag_for_another_role_is_refused(step, role):
    data = _with(lambda d: _set(d, role, "steps", step, "who"))
    _refused(data, f"{role} can't take {step}: it always goes to the {FLAG_WHO[step]}")


def test_publishing_a_status_report_is_a_persons_step_with_report_publish():
    assert STEP_AGENTS["publish_status_report"] == ()
    assert who_permission("publish_status_report") == "report:publish"
    assert STEP_AGENTS["draft_status_report"] == ("status_report_agent",)
    loaded = load_autonomy(autonomy_dir())
    assert loaded.steps["draft_status_report"].mode == "agent"
    assert loaded.steps["draft_status_report"].who == "project_manager"
    assert loaded.steps["publish_status_report"].mode == "person"


# --- Refusing bad files


@pytest.mark.parametrize("step", sorted(FLOOR_STEPS))
@pytest.mark.parametrize("mode", ["agent", "window"])
def test_a_floor_step_below_person_is_refused(step, mode):
    def change(data):
        data["steps"][step] = {"mode": mode, "agent": "risk_agent", "who": "sponsor"}
        if mode == "window":
            data["steps"][step]["window_minutes"] = 15

    _refused(_with(change), f"{step} must be decided by a person")


def _drop(data, *path):
    *parents, last = path
    for key in parents:
        data = data[key]
    del data[last]


def _set(data, value, *path):
    *parents, last = path
    for key in parents:
        data = data[key]
    data[last] = value


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: _set(d, {"mode": "person", "who": "sponsor"}, "steps", "sing"), "unknown steps"),
        (lambda d: _drop(d, "steps", "draft_plan"), "missing steps: draft_plan"),
        (lambda d: _set(d, "robot", "steps", "draft_plan", "agent"), "unknown agent 'robot'"),
        (lambda d: _set(d, "intern", "steps", "pass_gate", "who"), "unknown role 'intern'"),
        (lambda d: _drop(d, "steps", "pass_gate", "who"), "missing keys: who"),
        (lambda d: _drop(d, "steps", "score_risk", "window_minutes"), "needs its minutes"),
        (lambda d: _set(d, 0, "steps", "score_risk", "window_minutes"), "from 1 to 1440"),
        (lambda d: _set(d, 1441, "steps", "score_risk", "window_minutes"), "from 1 to 1440"),
        (lambda d: _set(d, 1.5, "steps", "score_risk", "window_minutes"), "from 1 to 1440"),
        (lambda d: _set(d, True, "steps", "score_risk", "window_minutes"), "from 1 to 1440"),
        (
            lambda d: _set(d, 15, "steps", "draft_plan", "window_minutes"),
            "only a window step has minutes",
        ),
        (lambda d: _set(d, "sometimes", "steps", "draft_plan", "mode"), "unknown mode"),
        (lambda d: _drop(d, "steps", "draft_plan", "agent"), "must name its agent"),
        (
            lambda d: _set(d, "risk_agent", "steps", "decide_change_pm", "agent"),
            "only an agent or window step names an agent",
        ),
        (
            lambda d: _set(d, "intake_agent", "steps", "score_risk", "agent"),
            "intake_agent can't do score_risk; agents that may: risk_agent",
        ),
        (
            lambda d: _set(
                d,
                {"mode": "agent", "agent": "risk_agent", "who": "project_manager"},
                "steps",
                "decide_change_pm",
            ),
            "risk_agent can't do decide_change_pm; agents that may: none",
        ),
        (lambda d: _set(d, "x", "steps", "draft_plan", "note"), "unknown keys: note"),
        (lambda d: _set(d, {"enabled": True}, "agents", "ghost"), "unknown agents: ghost"),
        (lambda d: _drop(d, "agents", "risk_agent"), "missing agents: risk_agent"),
        (lambda d: _set(d, "no", "agents", "risk_agent", "enabled"), "expected true or false"),
        (lambda d: _set(d, 1, "agents", "risk_agent", "limit"), "unknown keys: limit"),
        (lambda d: _set(d, {}, "extra"), "unknown keys: extra"),
        (lambda d: _drop(d, "agents"), "missing keys: agents"),
    ],
)
def test_the_loader_refuses_a_bad_file_clearly(change, message):
    _refused(_with(change), message)


def test_each_step_names_the_permission_its_who_needs():
    special = {
        "pass_gate": "gate:decide",
        "decide_change_sponsor": "gate:decide",
        "approve_proposal": "proposal:approve",
        "accept_risk": "risk:accept",
        "publish_status_report": "report:publish",
        "report_overdue_pm": "report:publish",
        "report_overdue_sponsor": "project:read",
        "report_unread": "project:read",
    }
    for step in STEPS:
        assert who_permission(step) == special.get(step, "project:write")
        assert who_permission(step) in {action.value for action in Action}


@pytest.mark.parametrize(
    ("step", "role", "permission"),
    [
        ("pass_gate", "project_manager", "gate:decide"),
        ("decide_change_sponsor", "project_manager", "gate:decide"),
        ("approve_proposal", "project_manager", "proposal:approve"),
        ("accept_risk", "ethics_reviewer", "risk:accept"),
        ("draft_plan", "sponsor", "project:write"),
        ("score_risk", "ethics_reviewer", "project:write"),
        ("send_proposal", "sponsor", "project:write"),
        ("decide_change_pm", "sponsor", "project:write"),
        ("publish_status_report", "sponsor", "report:publish"),
        ("draft_status_report", "sponsor", "project:write"),
    ],
)
def test_a_who_without_the_steps_permission_is_refused(step, role, permission):
    data = _with(lambda d: _set(d, role, "steps", step, "who"))
    _refused(data, f"steps.{step}.who: {role} can't take {step}: it needs {permission}")


@pytest.mark.parametrize("role", ["auditor", "admin"])
@pytest.mark.parametrize("step", STEPS)
def test_a_read_only_role_can_never_be_who(step, role):
    _refused(_with(lambda d: _set(d, role, "steps", step, "who")), f"{role} can't take {step}")


def test_a_missing_or_wrong_version_file_is_refused(tmp_path: Path):
    with pytest.raises(PolicyError, match="autonomy.yaml: policy file not found"):
        load_autonomy(tmp_path)
    (tmp_path / "autonomy.yaml").write_text("version: 2\n")
    with pytest.raises(PolicyError, match="version must be 1"):
        load_autonomy(tmp_path)


# --- Effective modes


def test_a_disabled_agent_sends_its_steps_to_a_person():
    policy = parse_autonomy(_with(lambda d: _set(d, False, "agents", "risk_agent", "enabled")))
    for step in ("propose_risks", "score_risk", "open_low_risk"):
        assert effective_mode(policy, step) == "person"
    assert effective_mode(policy, "draft_proposal") == "agent"
    assert effective_mode(policy, "submit_proposal") == "window"


def test_score_risk_never_applies_to_a_needs_judgement_risk():
    def agent_scores(data):
        data["steps"]["score_risk"] = {
            "mode": "agent",
            "agent": "risk_agent",
            "who": "project_manager",
        }

    policy = parse_autonomy(_with(agent_scores))
    assert effective_mode(policy, "score_risk", SimpleNamespace(needs_judgement=False)) == "agent"
    assert effective_mode(policy, "score_risk", SimpleNamespace(needs_judgement=True)) == "person"
    proposed = parse_autonomy(_proposed())
    judged = SimpleNamespace(needs_judgement=True)
    assert effective_mode(proposed, "score_risk", judged) == "person"


# --- The route


def test_the_route_returns_the_policy(db, sign_in):
    web = TestClient(app, base_url="http://localhost")
    sign_in(web, db, "auditor")
    response = web.get("/api/v1/autonomy")
    assert response.status_code == 200
    body = response.json()
    loaded = load_autonomy(autonomy_dir())
    assert body["digest"] == loaded.digest
    assert [step["id"] for step in body["steps"]] == list(STEPS)
    score = next(step for step in body["steps"] if step["id"] == "score_risk")
    assert score == {
        "id": "score_risk",
        "mode": "window",
        "effective_mode": "window",
        "agent": "risk_agent",
        "who": "project_manager",
        "window_minutes": 15,
        "person_only": False,
    }
    gate = next(step for step in body["steps"] if step["id"] == "pass_gate")
    assert gate["person_only"] is True and gate["agent"] is None
    assert body["agents"] == [{"name": name, "enabled": True} for name in AGENTS]


def test_the_route_shows_the_effective_mode_after_an_off_switch(db, sign_in, monkeypatch):
    rules = parse_autonomy(_with(lambda d: _set(d, False, "agents", "risk_agent", "enabled")))
    monkeypatch.setattr(policy, "get_autonomy", lambda: rules)
    web = TestClient(app, base_url="http://localhost")
    sign_in(web, db, "project_manager")
    body = web.get("/api/v1/autonomy").json()
    score = next(step for step in body["steps"] if step["id"] == "score_risk")
    assert (score["mode"], score["effective_mode"]) == ("window", "person")
    assert {"name": "risk_agent", "enabled": False} in body["agents"]


def test_without_the_policy_the_route_is_503(db, sign_in, monkeypatch):
    def missing():
        raise PolicyError("autonomy.yaml: policy file not found")

    monkeypatch.setattr(policy, "get_autonomy", missing)
    web = TestClient(app, base_url="http://localhost")
    sign_in(web, db, "sponsor")
    response = web.get("/api/v1/autonomy")
    assert response.status_code == 503
    assert "apply the autonomy policy file" in response.json()["detail"]


def test_the_route_needs_a_login():
    web = TestClient(app, base_url="http://localhost")
    assert web.get("/api/v1/autonomy").status_code == 401
