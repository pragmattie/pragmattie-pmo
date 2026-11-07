"""The decision inbox (item 6.3): who sees which item, the facts and the coded recommendation,
window actions and holds, and deciding through the existing routes. No test calls a model."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_orchestrator import (
    Calls,
    _actions,
    _change,
    _complete_plan,
    _items,
    _pass,
    _project,
    _proposal,
    _request,
    _risk,
    _risk_agent_ran,
    _user,
    _with,
    _world,
)

from app import inbox, orchestrator
from app import policy as policy_module
from app.autonomy import STEPS, WINDOW
from app.config import get_settings
from app.db import engine
from app.inbox import Fact, recommend
from app.main import app
from app.models import Base, ChangeRequest, ProjectRequest, utcnow
from app.orchestrator import StepAction
from app.security import CSRF_COOKIE, SESSION_COOKIE, issue_token

URL = "/api/v1/inbox"
HIGH_RISK = {
    "status": "open",
    "probability": 4,
    "impact": 4,
    "score": 16,
    "band": "High",
    "escalated_to": "sponsor",
    "escalated_band": "High",
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
def web(db, gates_policy) -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def settings(monkeypatch):
    current = get_settings()
    monkeypatch.setattr(current, "orchestrator_mode", "enforce")
    monkeypatch.setattr(current, "agent_mode", "enforce")
    return current


@pytest.fixture
def actions(monkeypatch):
    def use(**fakes):
        registry = {step: StepAction(fake) for step, fake in fakes.items()}
        monkeypatch.setattr(orchestrator, "ACTIONS", registry)
        return fakes

    return use


def _inbox(web: TestClient, **params) -> dict:
    response = web.get(URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _steps(web: TestClient) -> list[tuple[str, str, int]]:
    return [(e["step"], e["subject_type"], e["subject_id"]) for e in _inbox(web)["items"]]


def _count(web: TestClient) -> int:
    response = web.get(f"{URL}/count")
    assert response.status_code == 200, response.text
    return response.json()["count"]


# --- Who sees what


def test_each_role_sees_only_its_own_items(db, web, settings, actions, sign_in):
    actions()
    ids = _world(db)
    _pass(db, settings)
    open_items = _items(db, "open")
    by_role = {}
    for role in ("sponsor", "project_manager", "ethics_reviewer", "admin", "auditor"):
        web.cookies.clear()
        sign_in(web, db, role, f"{role}.inbox@pmo.example")
        by_role[role] = _inbox(web)
        assert {e["who"] for e in by_role[role]["items"]} <= {role}
        assert _count(web) == by_role[role]["total"]
    sponsor_steps = {e["step"] for e in by_role["sponsor"]["items"]}
    assert {"pass_gate", "accept_risk", "approve_proposal"} <= sponsor_steps
    assert "decide_change_sponsor" in sponsor_steps
    assert by_role["admin"]["items"] == [] and by_role["auditor"]["items"] == []
    # The project manager signed in here didn't raise the world's draft requests, so the two
    # draft steps go to the one who did; the project manager's other items have no named person.
    pm_steps = {(e["step"], e["subject_id"]) for e in by_role["project_manager"]["items"]}
    assert ("draft_value_case", ids["draft_value_case"]) not in pm_steps
    assert ("decide_change_pm", ids["decide_change_pm"]) in pm_steps
    shown = sum(len(page["items"]) for page in by_role.values())
    assert shown == len(open_items) - 2


def test_a_draft_request_goes_to_the_project_manager_who_raised_it(
    db, web, settings, actions, sign_in
):
    actions()
    raiser = sign_in(web, db, "project_manager", "raiser@pmo.example")
    request_id = _request(db, raiser)
    _pass(db, settings)
    entry = next(e for e in _inbox(web)["items"] if e["subject_id"] == request_id)
    assert entry["step"] == "draft_value_case"
    assert (entry["named_user_id"], entry["named_user_name"]) == (raiser.id, raiser.display_name)
    web.cookies.clear()
    sign_in(web, db, "project_manager", "other.pm@pmo.example")
    assert _steps(web) == []


def test_a_draft_request_raised_by_someone_else_goes_to_any_project_manager(
    db, web, settings, actions, sign_in
):
    actions()
    request_id = _request(db, _user(db, role="sponsor", name="raiser"))
    _pass(db, settings)
    sign_in(web, db, "project_manager")
    assert _steps(web) == [("draft_value_case", "request", request_id)]


def test_a_sponsor_never_sees_another_projects_named_sponsor_items(db, web, settings, sign_in):
    a_sponsor = _user(db, role="sponsor", name="sponsor-a")
    b_sponsor = _user(db, role="sponsor", name="sponsor-b")
    a = _project(db, stage="delivery", sponsor_user_id=a_sponsor.id)
    b = _project(db, stage="delivery", sponsor_user_id=b_sponsor.id)
    unnamed = _project(db, stage="delivery")
    risks = {p.id: _risk(db, p, **HIGH_RISK).id for p in (a, b, unnamed)}
    _pass(db, settings)
    for sponsor, project in ((a_sponsor, a), (b_sponsor, b)):
        web.cookies.set(SESSION_COOKIE, issue_token(sponsor.id, "sponsor").token)
        web.cookies.set(CSRF_COOKIE, "test-csrf-value")
        steps = _steps(web)
        assert ("accept_risk", "risk", risks[project.id]) in steps
        other = b if project is a else a
        assert ("accept_risk", "risk", risks[other.id]) not in steps
        # A project that names no sponsor shows to every sponsor.
        assert ("accept_risk", "risk", risks[unnamed.id]) in steps
        named = next(e for e in _inbox(web)["items"] if e["subject_id"] == risks[project.id])
        assert named["named_user_name"] == sponsor.display_name
        assert named["project_name"] == "Pilot"


def test_an_item_follows_its_steps_who_when_a_risk_is_re_escalated(
    db, web, settings, actions, sign_in
):
    actions()
    sponsor = _user(db, role="sponsor", name="re-escalated-sponsor")
    project = _project(db, stage="delivery", sponsor_user_id=sponsor.id)
    risk = _risk(db, project, **HIGH_RISK)
    _pass(db, settings)
    [old] = _items(db, "open")
    assert old.who == "sponsor"
    web.cookies.set(SESSION_COOKIE, issue_token(sponsor.id, "sponsor").token)
    web.cookies.set(CSRF_COOKIE, "test-csrf-value")
    assert _steps(web) == [("accept_risk", "risk", risk.id)]

    risk.escalated_to = "ethics_reviewer"
    db.commit()
    # At once, before the next pass: the sponsor stops seeing it, the ethics reviewer sees it.
    assert _steps(web) == [] and _count(web) == 0
    web.cookies.clear()
    sign_in(web, db, "ethics_reviewer")
    [entry] = _inbox(web)["items"]
    assert (entry["step"], entry["subject_id"], entry["who"]) == (
        "accept_risk",
        risk.id,
        "ethics_reviewer",
    )
    assert entry["reason"] == (
        "the risk is escalated to the ethics_reviewer and waiting for a decision"
    )
    assert entry["handoff"] is None and entry["named_user_id"] is None
    assert _count(web) == 1

    # The next pass closes the sponsor's item and opens one for the ethics reviewer.
    _pass(db, settings)
    db.expire_all()
    closed = db.get(type(old), old.id)
    assert (closed.status, closed.closed_reason) == ("done", orchestrator.WHO_CHANGED)
    [new] = _items(db, "open")
    assert (new.step, new.who) == ("accept_risk", "ethics_reviewer")
    assert _steps(web) == [("accept_risk", "risk", risk.id)]


# --- The case: facts and the recommendation


def test_each_item_shows_its_reason_facts_and_recommendation(db, web, settings, actions, sign_in):
    actions()
    project = _project(db, stage="delivery")
    risk = _risk(db, project, **HIGH_RISK)
    _pass(db, settings)
    sign_in(web, db, "sponsor")
    [entry] = _inbox(web)["items"]
    assert entry["kind"] == "item" and entry["subject_title"] == risk.title
    assert entry["reason"] == "the risk is escalated to the sponsor and waiting for a decision"
    assert entry["handoff"] is None
    facts = {f["key"]: f["value"] for f in entry["facts"]}
    assert (facts["score"], facts["band"]) == ("16", "High")
    expected = recommend("accept_risk", [Fact(**f) for f in entry["facts"]])
    assert entry["recommendation"] == expected
    assert "High risk scored 16" in expected


def test_a_gate_item_shows_its_checks(db, web, settings, actions, sign_in):
    actions()
    pm = _user(db)
    request_id = _request(db, pm, status="submitted")
    _pass(db, settings)
    sign_in(web, db, "sponsor")
    [entry] = _inbox(web)["items"]
    assert (entry["step"], entry["subject_id"]) == ("pass_gate", request_id)
    checks = [f for f in entry["facts"] if f["met"] is not None]
    assert checks and any(f["met"] is False for f in checks)
    assert entry["recommendation"].startswith("Return it:")


def test_a_handoff_shows_why_it_went_to_a_person(db, web, settings, actions, sign_in):
    actions()
    _project(db, stage="planning")
    _pass(db, settings)
    sign_in(web, db, "project_manager")
    entries = {e["step"]: e for e in _inbox(web)["items"]}
    assert entries["draft_plan"]["reason"] == "the plan is incomplete: the project has no plan"
    assert entries["draft_plan"]["handoff"] == orchestrator.NO_AGENT
    assert entries["draft_plan"]["recommendation"] == (
        "Complete the plan: the project has no plan."
    )


def test_an_agent_that_failed_hands_off_with_that_reason(db, web, settings, actions, sign_in):
    actions(draft_value_case=Calls(fail=RuntimeError("the model was down")))
    pm = sign_in(web, db, "project_manager")
    _request(db, pm)
    start = utcnow()
    for minutes in (0, 5, 10, 15):
        _pass(db, settings, now=start + timedelta(minutes=minutes))
    [entry] = _inbox(web)["items"]
    assert entry["reason"] == "the request has no value case"
    assert entry["handoff"] == "the intake_agent failed after 3 retries"


def test_every_step_has_a_recommendation():
    assert set(inbox.RECOMMENDERS) == set(STEPS)
    assert recommend("unknown_step", []) == inbox.DEFAULT_RECOMMENDATION


def _facts(**values) -> list[Fact]:
    return [Fact(key, key, str(value)) for key, value in values.items()]


@pytest.mark.parametrize(
    ("step", "facts", "expected"),
    [
        (
            "pass_gate",
            [Fact("a", "Plan complete", "ok", True), Fact("b", "Within budget", "over", False)],
            "Return it: 1 of 2 checks are not met (Within budget).",
        ),
        (
            "pass_gate",
            [Fact("a", "Plan complete", "ok", True)],
            "Pass it if the person criteria hold: all 1 checks are met.",
        ),
        ("pass_gate", [], "No automatic checks apply: decide on the person criteria."),
        (
            "accept_risk",
            _facts(score=12, band="Medium", response="reduce"),
            "Accept it only if the project can carry a Medium risk scored 12; "
            "otherwise ask for a stronger response than 'reduce'.",
        ),
        (
            "open_low_risk",
            _facts(score=2, band="Low"),
            "Open it in the risk register: scored 2, Low, which a project manager owns.",
        ),
        (
            "score_risk",
            _facts(needs_judgement="yes"),
            "Score it yourself: it needs a person's judgement, so no agent may score it.",
        ),
        (
            "score_risk",
            _facts(needs_judgement="no"),
            "Score its probability and impact so it gets a band and an owner.",
        ),
        ("approve_proposal", _facts(flags=2), "Return it: the checker has 2 flag(s) open."),
        (
            "approve_proposal",
            _facts(flags=0, price="1200.00"),
            "Approve it if the price (1200.00) and terms are right: no flags.",
        ),
        (
            "send_proposal",
            _facts(flags=1),
            "Clear the 1 open flag(s) first: it can't be marked sent while they're open.",
        ),
        (
            "send_proposal",
            _facts(flags=0),
            "Send it to the client and mark it sent: the checker has no flags open.",
        ),
        (
            "submit_proposal",
            _facts(flags=3),
            "Clear the 3 checker flag(s), then submit it for approval.",
        ),
        (
            "submit_proposal",
            _facts(flags=0),
            "Submit it for approval: every section is written and reviewed, with no flags open.",
        ),
        (
            "draft_proposal",
            _facts(empty_sections="summary, terms"),
            "Write the empty sections: summary, terms.",
        ),
        ("client_answer", [], "Record the client's answer when it arrives."),
        (
            "decide_change_sponsor",
            _facts(cost_difference="500.00 (10.00%)", escalations="cost above limit"),
            "Weigh it with care: it goes beyond the limits (cost above limit); "
            "cost 500.00 (10.00%).",
        ),
        (
            "decide_change_pm",
            _facts(cost_difference="10.00 (1.00%)", later_working_days=2, escalations="none"),
            "Approve it if the reason holds: within the limits (cost 10.00 (1.00%), 2 days later).",
        ),
        (
            "draft_value_case",
            _facts(readiness="none"),
            "Write the value case and, as the request uses AI, the readiness check.",
        ),
        (
            "draft_value_case",
            _facts(readiness="not needed"),
            "Write the value case: the request has none.",
        ),
        (
            "draft_readiness",
            [],
            "Complete the readiness check: the request uses AI and has none.",
        ),
        (
            "draft_plan",
            _facts(plan_gaps="no milestones"),
            "Complete the plan: no milestones.",
        ),
        (
            "propose_risks",
            _facts(risks=3),
            "Look for the project's risks and add them to the risk register (it holds 3).",
        ),
    ],
)
def test_each_steps_recommendation_comes_from_its_facts(step, facts, expected):
    assert recommend(step, facts) == expected


def test_facts_for_each_kind_of_subject(db, settings, actions):
    """Every step in a world of subjects gets facts and a recommendation without error."""
    actions()
    _world(db)
    rules = policy_module.get_autonomy()
    for step in orchestrator.next_steps(db, rules):
        facts = inbox.facts_for(db, step.step, step.subject_type, step.subject_id)
        assert facts, step
        assert recommend(step.step, facts)


def test_a_proposal_shows_its_flags(db, settings, actions):
    actions()
    pm = _user(db)
    project = _project(db, stage="delivery", client=True)
    _complete_plan(db, project, pm)
    proposal_id = _proposal(db, project, pm, "submitted", filled=True)
    facts = inbox.facts_for(db, "approve_proposal", "proposal", proposal_id)
    flags = next(f for f in facts if f.key == "flags")
    # The proposal copied nothing from the plan, so the checker flags it.
    assert flags.met is False and int(flags.value) > 0
    assert recommend("approve_proposal", facts).startswith("Return it:")


def test_a_change_shows_its_effect(db):
    pm = _user(db)
    project = _project(db, stage="delivery")
    change_id = _change(db, project, pm, "sponsor")
    change = db.get(ChangeRequest, change_id)
    change.effect = {
        "cost_difference": "500.00",
        "cost_difference_pct": "10.00",
        "max_later_working_days": 3,
        "scope_changes": False,
        "escalations": ["cost_above_limit"],
    }
    db.commit()
    facts = {
        f.key: f for f in inbox.facts_for(db, "decide_change_sponsor", "change_request", change_id)
    }
    assert facts["cost_difference"].value == "500.00 (10.00%)"
    assert facts["escalations"].value == "cost above limit" and facts["escalations"].met is False


# --- Deciding in place


def test_a_decided_item_leaves_at_once_and_a_second_decision_is_refused(
    db, web, settings, actions, sign_in
):
    actions()
    pm = _user(db)
    request_id = _request(db, pm, status="submitted")
    _pass(db, settings)
    sign_in(web, db, "sponsor")
    assert _count(web) == 1
    body = {
        "version": db.get(ProjectRequest, request_id).version,
        "outcome": "return",
        "reason": "The value case needs measurable benefits first",
        "criteria": [],
    }
    response = web.post(f"/api/v1/requests/{request_id}/gate", json=body)
    assert response.status_code == 200, response.text
    # No pass has run: the item is still open, but the inbox no longer shows it.
    assert [i.status for i in _items(db)] == ["open"]
    assert _inbox(web)["items"] == [] and _count(web) == 0
    # Deciding again through the same route is refused as it is anywhere else.
    assert web.post(f"/api/v1/requests/{request_id}/gate", json=body).status_code == 409
    # The next pass closes it, and opens the returned draft's next step.
    _pass(db, settings)
    assert [(i.step, i.status) for i in _items(db)] == [
        ("pass_gate", "done"),
        ("draft_value_case", "open"),
    ]


def test_a_role_that_may_not_decide_gets_the_routes_403(db, web, settings, actions, sign_in):
    actions()
    project = _project(db, stage="delivery")
    risk = _risk(db, project, **HIGH_RISK)
    _pass(db, settings)
    sign_in(web, db, "project_manager")
    body = {
        "version": risk.version,
        "reason": "We can live with this one",
        "review_date": "2099-01-01",
    }
    response = web.post(f"/api/v1/projects/{project.id}/risks/{risk.id}/accept", json=body)
    assert response.status_code == 403


# --- Window actions and holds


def _window(rules):
    return _with(rules, score_risk={"mode": WINDOW, "window_minutes": 15})


def test_a_window_action_shows_with_its_due_time_and_holds_for_its_role_only(
    db, web, settings, actions, sign_in
):
    actions(score_risk=Calls())
    risk = _risk(db, _project(db, stage="delivery"))
    rules = _window(policy_module.get_autonomy())
    start = utcnow()
    _pass(db, settings, now=start, rules=rules)
    sponsor = sign_in(web, db, "sponsor", "sponsor.hold@pmo.example")
    assert _steps(web) == []
    [action] = _actions(db)
    hold = f"/api/v1/orchestrator/actions/{action.id}/hold"
    assert web.post(hold, json={"reason": "Not my call to hold"}).status_code == 403
    web.cookies.clear()
    pm = sign_in(web, db, "project_manager", "pm.hold@pmo.example")
    [entry] = _inbox(web)["items"]
    assert (entry["kind"], entry["step"], entry["subject_id"]) == ("window", "score_risk", risk.id)
    assert entry["action_id"] == action.id
    assert entry["due_at"].startswith((start + timedelta(minutes=15)).isoformat()[:16])
    response = web.post(hold, json={"reason": "I want to score this one myself"})
    assert response.status_code == 200, response.text
    [entry] = _inbox(web)["items"]
    assert entry["kind"] == "item" and entry["id"] == response.json()["inbox_item_id"]
    assert entry["handoff"] == f"held by {pm.display_name}: I want to score this one myself"
    assert sponsor.id != pm.id


# --- Paging


def test_the_inbox_is_paged_oldest_first_in_a_stable_order(db, web, settings, actions, sign_in):
    actions()
    pm = _user(db)
    request_ids = [_request(db, pm, status="submitted") for _ in range(5)]
    _pass(db, settings)
    sign_in(web, db, "sponsor")
    first = _inbox(web, limit=2, offset=0)
    second = _inbox(web, limit=2, offset=2)
    third = _inbox(web, limit=2, offset=4)
    assert first["total"] == second["total"] == third["total"] == 5
    seen = [e["subject_id"] for page in (first, second, third) for e in page["items"]]
    assert seen == request_ids
    stamps = [e["created_at"] for page in (first, second, third) for e in page["items"]]
    assert stamps == sorted(stamps)


def test_the_inbox_needs_a_session(db, web):
    assert web.get(URL).status_code == 401
    assert web.get(f"{URL}/count").status_code == 401


def test_a_missing_autonomy_policy_is_a_503(db, web, sign_in, monkeypatch):
    def broken():
        raise policy_module.PolicyError("no file")

    monkeypatch.setattr(policy_module, "get_autonomy", broken)
    sign_in(web, db, "sponsor")
    assert web.get(URL).status_code == 503
    assert web.get(f"{URL}/count").status_code == 503


def test_the_risk_agent_ran_helper_keeps_propose_risks_out(db, web, settings, actions, sign_in):
    """A project the risk agent has looked at has no `propose_risks` item."""
    actions()
    project = _project(db, stage="planning")
    _risk_agent_ran(db, project)
    _pass(db, settings)
    sign_in(web, db, "project_manager")
    assert "propose_risks" not in {e["step"] for e in _inbox(web)["items"]}
