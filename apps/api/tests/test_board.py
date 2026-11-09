"""The portfolio board (item 6.4): each column's rule, the card fields, what each card waits on
(the orchestrator's next steps), the summary, the filters and who may read it. No test calls a
model."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_intake import add_value_case
from test_orchestrator import (
    Calls,
    _complete_plan,
    _pass,
    _project,
    _proposal,
    _request,
    _risk,
    _risk_agent_ran,
    _sent,
    _user,
    _with,
    _world,
)

from app import board, orchestrator
from app import policy as policy_module
from app.board import Filters
from app.config import get_settings
from app.db import engine
from app.main import app
from app.models import (
    Base,
    Client,
    OrchestratorAction,
    PlanEstimateLine,
    Project,
    ProjectBrief,
    ProjectPlan,
    ProjectProposal,
    ProjectRequest,
    utcnow,
)
from app.orchestrator import StepAction, hold, next_steps
from app.risks import risk_policy

URL = "/api/v1/board"


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
def rules():
    return policy_module.get_autonomy()


@pytest.fixture
def actions(monkeypatch):
    def use(**fakes):
        registry = {step: StepAction(fake) for step, fake in fakes.items()}
        monkeypatch.setattr(orchestrator, "ACTIONS", registry)
        return fakes

    return use


def _board(db, rules, filters: Filters | None = None, agent_mode: str = "enforce"):
    db.expire_all()
    return board.board(
        db, rules, filters or Filters(), agent_mode=agent_mode, risk_rules=risk_policy()
    )


def _cards(db, rules, **filters) -> dict[str, board.Card]:
    found = _board(db, rules, Filters(**filters))
    return {card.key: card for cards in found.columns.values() for card in cards}


def _column(db, rules, key: str, **filters) -> str | None:
    card = _cards(db, rules, **filters).get(key)
    return None if card is None else card.column


def _owned(db: Session, client: Client, **fields) -> Project:
    """A project for `client`, run by the project manager Pat Lee."""
    project = _project(db, **fields)
    project.client_id, project.pm = client.id, "Pat Lee"
    db.commit()
    return project


def _get(web: TestClient, **params) -> dict:
    response = web.get(URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _api_cards(body: dict) -> dict[str, dict]:
    return {card["key"]: card for column in body["columns"] for card in column["cards"]}


# --- Columns


def test_columns_are_in_order(db, rules):
    found = _board(db, rules)
    assert list(found.columns) == [
        "requests",
        "approval_gate",
        "planning",
        "go_ahead_gate",
        "delivery",
        "closure",
        "closed",
    ]


def test_draft_and_submitted_requests_are_requests_until_every_approval_check_is_met(
    db, rules, gates_policy
):
    pm = _user(db)
    draft = _request(db, pm)
    unmet = _request(db, pm, status="submitted")
    met = _request(db, pm, status="submitted")
    add_value_case(met, pm.id)
    # Complete but not confirmed by a person: the check is still unmet.
    add_value_case(unmet, None)
    assert _column(db, rules, f"request-{draft}") == "requests"
    assert _column(db, rules, f"request-{unmet}") == "requests"
    assert _column(db, rules, f"request-{met}") == "approval_gate"
    # A draft request with a confirmed value case is still a request: only a submitted one waits
    # at the gate.
    add_value_case(draft, pm.id)
    assert _column(db, rules, f"request-{draft}") == "requests"


def test_an_ai_request_waits_at_the_gate_only_with_its_readiness_checks_met(
    db, rules, gates_policy
):
    pm = _user(db)
    request_id = _request(db, pm, status="submitted", uses_ai=True)
    add_value_case(request_id, pm.id)
    assert _column(db, rules, f"request-{request_id}") == "requests"


def test_planning_projects_wait_at_the_go_ahead_gate_once_every_check_is_met(db, rules):
    pm = _user(db)
    bare = _project(db)
    ready = _project(db)
    _complete_plan(db, ready, pm)
    _risk_agent_ran(db, ready)
    assert _column(db, rules, f"project-{bare.id}") == "planning"
    assert _column(db, rules, f"project-{ready.id}") == "go_ahead_gate"
    # One check unmet (the plan goes over the top budget) and it is back in planning.
    plan_id = db.query(ProjectPlan.id).filter(ProjectPlan.project_id == ready.id).scalar()
    db.add(PlanEstimateLine(plan_id=plan_id, description="More", category="other", amount=500))
    db.commit()
    assert _column(db, rules, f"project-{ready.id}") == "planning"


def test_later_stages_are_their_own_columns(db, rules):
    keys = {stage: f"project-{_project(db, stage=stage).id}" for stage in board.STAGE_COLUMNS}
    assert _column(db, rules, keys["intake"]) == "requests"
    assert _column(db, rules, keys["delivery"]) == "delivery"
    assert _column(db, rules, keys["closure"]) == "closure"
    assert _column(db, rules, keys["closed"]) == "closed"


def test_an_approved_request_is_shown_as_its_project(db, rules):
    pm = _user(db)
    project = _project(db)
    request_id = _request(db, pm, status="approved")
    db.get(ProjectRequest, request_id).project_id = project.id
    db.commit()
    keys = _cards(db, rules)
    assert f"request-{request_id}" not in keys
    assert f"project-{project.id}" in keys


def test_declined_withdrawn_and_cancelled_items_are_hidden_unless_asked_for(db, rules):
    pm = _user(db)
    declined = f"request-{_request(db, pm, status='declined')}"
    withdrawn = f"request-{_request(db, pm, status='withdrawn')}"
    cancelled = _project(db, status="cancelled")
    _complete_plan(db, cancelled, pm)
    live = _project(db)
    keys = _cards(db, rules)
    assert set(keys) == {f"project-{live.id}"}
    shown = _cards(db, rules, show_stopped=True)
    assert {declined, withdrawn, f"project-{cancelled.id}"} < set(shown)
    assert shown[declined].stopped and shown[withdrawn].stopped
    assert shown[declined].column == shown[withdrawn].column == "requests"
    # A cancelled project sits in its stage's column, never at a gate, and adds no value.
    assert shown[f"project-{cancelled.id}"].column == "planning"
    assert shown[f"project-{cancelled.id}"].stopped
    summary = _board(db, rules, Filters(show_stopped=True)).summary
    assert summary.stopped == 3
    assert summary.planning_value == Decimal("0.00")
    assert summary.counts["requests"] == 2


# --- Cards


def test_card_fields_come_from_the_data(db, rules, web, sign_in):
    pm = _user(db)
    client = Client(name="Harbour Foods")
    db.add(client)
    db.flush()
    project = _owned(db, client, stage="delivery", status="at_risk")
    plan = _complete_plan(db, project, pm)
    plan.contingency_pct = 10
    db.add(PlanEstimateLine(plan_id=plan.id, description="More", category="other", amount=99))
    db.query(ProjectBrief).filter(ProjectBrief.project_id == project.id).update(
        {"budget_high": Decimal("150.50")}
    )
    db.commit()
    _proposal(db, project, pm, "sent", True)
    for band, score in (("Low", 2), ("High", 16), ("Medium", 8)):
        _risk(db, project, status="open", band=band, score=score, probability=1, impact=1)
    _risk(db, project, status="closed", band="High", score=20, probability=4, impact=5)
    _risk(db, project)  # proposed: not open

    card = _cards(db, rules)[f"project-{project.id}"]
    assert (card.name, card.client_name, card.project_manager) == (
        "Pilot",
        "Harbour Foods",
        "Pat Lee",
    )
    assert card.health == "at_risk"
    assert card.proposal_status == "sent"
    assert (card.top_risk_band, card.open_risks, card.open_medium_high_risks) == ("High", 3, 2)
    assert card.plan_total == Decimal("110.00")
    assert card.top_budget == Decimal("150.50")

    sign_in(web, db, "auditor")
    api = _api_cards(_get(web))[f"project-{project.id}"]
    assert (api["plan_total"], api["top_budget"]) == ("110.00", "150.50")
    assert api["kind"] == "project" and api["column"] == "delivery"

    # A withdrawn proposal isn't shown.
    db.query(ProjectProposal).update({"status": "withdrawn"})
    db.commit()
    assert _cards(db, rules)[f"project-{project.id}"].proposal_status is None


def test_a_request_card_names_the_project_manager_who_raised_it(db, rules):
    raiser = _user(db, name="Robin")
    admin = _user(db, role="admin", name="Ada")
    by_pm = _request(db, raiser)
    by_admin = _request(db, admin)
    db.get(ProjectRequest, by_pm).budget_high = Decimal("2500")
    db.commit()
    keys = _cards(db, rules)
    assert keys[f"request-{by_pm}"].project_manager == "Robin"
    assert keys[f"request-{by_pm}"].top_budget == Decimal("2500.00")
    assert keys[f"request-{by_admin}"].project_manager is None
    assert keys[f"request-{by_pm}"].health is None
    assert keys[f"request-{by_pm}"].plan_total is None


# --- What a card waits on


def test_waiting_matches_the_orchestrators_next_steps(db, rules, actions):
    actions(draft_value_case=Calls(), propose_risks=Calls(), submit_proposal=Calls())
    _world(db)
    steps = next_steps(db, rules, risk_rules=risk_policy())
    found = _cards(db, rules)
    on_cards = sorted(
        (w.step, w.subject_type, w.subject_id) for c in found.values() for w in c.waiting
    )
    assert on_cards == sorted((s.step, s.subject_type, s.subject_id) for s in steps)
    by_step = {(s.step, s.subject_type, s.subject_id): s for s in steps}
    for card in found.values():
        for waiting in card.waiting:
            step = by_step[(waiting.step, waiting.subject_type, waiting.subject_id)]
            assert waiting.holder == ("person" if step.mode == "person" else "agent")
            if step.mode == "person":
                assert waiting.who == step.waits_on
                assert waiting.text.startswith(
                    f"Waiting on the {step.waits_on.replace('_', ' ')}: "
                )


def test_waiting_words(db, rules, actions, settings):
    actions(draft_value_case=Calls(), submit_proposal=Calls())
    pm = _user(db)
    request_id = _request(db, pm)
    ready = _project(db)
    _complete_plan(db, ready, pm)
    _risk_agent_ran(db, ready)
    gate_request = _request(db, pm, status="submitted")
    proposal_project = _project(db, stage="delivery", client=True)
    _proposal(db, proposal_project, pm, "draft", True)

    found = _cards(db, rules)
    assert [w.text for w in found[f"request-{request_id}"].waiting] == [
        "Agent drafting the value case"
    ]
    assert found[f"request-{request_id}"].held_by == "agent"
    assert [w.text for w in found[f"project-{ready.id}"].waiting] == [
        "Waiting on the sponsor: go-ahead gate"
    ]
    assert found[f"project-{ready.id}"].held_by == "person"
    assert [w.text for w in found[f"request-{gate_request}"].waiting] == [
        "Waiting on the sponsor: approval gate"
    ]
    # A window step before the orchestrator has started it, then with its due time.
    proposal_card = f"project-{proposal_project.id}"
    assert [w.text for w in found[proposal_card].waiting] == [
        "Agent will submit the proposal unless held"
    ]
    now = datetime(2026, 11, 7, 14, 5)
    _pass(db, settings, now=now)
    waiting = _cards(db, rules)[proposal_card].waiting
    assert [w.text for w in waiting] == ["Agent will submit the proposal at 14:20 UTC unless held"]
    assert waiting[0].due_at == now + timedelta(minutes=15)


def test_a_held_window_waits_on_a_person(db, rules, actions, settings):
    actions(submit_proposal=Calls())
    pm = _user(db)
    project = _project(db, stage="delivery", client=True)
    _proposal(db, project, pm, "draft", True)
    _pass(db, settings)
    action = db.query(OrchestratorAction).one()
    hold(db, action, user_id=pm.id, role="project_manager", reason="Wait for the client call")
    db.commit()
    card = _cards(db, rules)[f"project-{project.id}"]
    assert [w.text for w in card.waiting] == ["Waiting on the project manager: proposal to submit"]
    assert card.held_by == "person"


def test_with_the_agents_off_every_step_waits_on_a_person(db, rules, actions):
    actions(draft_value_case=Calls())
    pm = _user(db)
    request_id = _request(db, pm)
    found = board.board(
        db, rules, Filters(), agent_mode="shadow", risk_rules=risk_policy()
    ).columns["requests"]
    assert [w.text for w in found[0].waiting] == ["Waiting on the project manager: value case"]
    assert found[0].key == f"request-{request_id}"


def test_a_simulated_clients_step_is_labelled_simulated(db, rules, actions):
    actions(client_answer=Calls())
    pm = _user(db)
    project = _project(db, stage="delivery", client=True, client_email="buyer@client.example")
    proposal_id = _proposal(db, project, pm, "sent", True)
    # The simulated client answers only once its delay after the `sent` event is over.
    _sent(db, proposal_id, pm, utcnow() - timedelta(minutes=11))
    card = _cards(db, rules)[f"project-{project.id}"]
    assert [w.text for w in card.waiting] == ["Simulated client answering the proposal"]
    assert card.simulated and card.waiting[0].simulated
    windowed = _with(rules, client_answer=dict(mode="window", window_minutes=30))
    card = _cards(db, windowed)[f"project-{project.id}"]
    assert card.waiting[0].text == "Simulated client will answer the proposal unless held"
    # A person recording the answer is not a simulated step.
    by_person = _with(rules, client_answer=dict(mode="person", agent=None))
    card = _cards(db, by_person)[f"project-{project.id}"]
    assert card.waiting[0].text == "Waiting on the project manager: client's answer"
    assert not card.waiting[0].simulated
    # The card still says the client is simulated (item 6.8).
    assert card.client_simulated and card.simulated


# --- Summary and filters


def test_the_summary_adds_up_the_shown_cards(db, rules):
    pm = _user(db)
    _request(db, pm)
    _request(db, pm, status="submitted")
    planning = _project(db)
    _complete_plan(db, planning, pm)  # 1.00, with every go-ahead check met
    other = _project(db)
    other_plan = _complete_plan(db, other, pm)
    db.add(PlanEstimateLine(plan_id=other_plan.id, description="X", category="other", amount=5))
    delivery = _project(db, stage="delivery")
    delivery_plan = _complete_plan(db, delivery, pm)
    db.add(PlanEstimateLine(plan_id=delivery_plan.id, description="Y", category="other", amount=9))
    db.commit()
    for band, score in (("Low", 2), ("Medium", 8), ("High", 16)):
        _risk(db, delivery, status="open", band=band, score=score, probability=1, impact=1)
    found = _board(db, rules)
    summary = found.summary
    assert summary.counts == {
        "requests": 2,
        "approval_gate": 0,
        "planning": 0,
        "go_ahead_gate": 2,
        "delivery": 1,
        "closure": 0,
        "closed": 0,
    }
    # The planning value counts the go-ahead gate column: those projects are still planning.
    assert summary.planning_value == Decimal("7.00")
    assert summary.delivery_value == Decimal("10.00")
    assert summary.open_medium_high_risks == 2
    person_steps = [w for cards in found.columns.values() for c in cards for w in c.waiting]
    assert summary.decisions_waiting == sum(1 for w in person_steps if w.holder == "person")


def test_filters(db, rules, actions):
    actions(draft_value_case=Calls(), propose_risks=Calls())
    pm = _user(db, name="Robin")
    harbour = Client(name="Harbour Foods")
    db.add(harbour)
    db.flush()
    agent_card = f"request-{_request(db, pm)}"
    person_card = f"request-{_request(db, pm, status='submitted')}"
    theirs = _owned(db, harbour, stage="delivery")
    # A card waiting on a person and an agent at once is person-held, in the filter too.
    # Here the risk agent hasn't looked yet and a submitted proposal waits for approval.
    both = _project(db)
    both.pm = "Robin"
    _complete_plan(db, both, pm)
    _proposal(db, both, pm, "submitted", True)
    both_card = f"project-{both.id}"
    found = _cards(db, rules)
    assert {w.holder for w in found[both_card].waiting} == {"agent", "person"}
    assert found[both_card].held_by == "person"
    keys = set(found)
    assert set(_cards(db, rules, client_id=harbour.id)) == {f"project-{theirs.id}"}
    assert set(_cards(db, rules, project_manager="Pat Lee")) == {f"project-{theirs.id}"}
    assert set(_cards(db, rules, project_manager="Robin")) == {agent_card, person_card, both_card}
    assert set(_cards(db, rules, waiting="agent")) == {agent_card}
    assert set(_cards(db, rules, waiting="person")) == {person_card, both_card}
    assert keys == {agent_card, person_card, both_card, f"project-{theirs.id}"}
    # A filter never hides its own choices.
    narrowed = _board(db, rules, Filters(client_id=harbour.id))
    assert narrowed.clients == [(harbour.id, "Harbour Foods")]
    assert narrowed.project_managers == ["Pat Lee", "Robin"]
    assert narrowed.summary.counts["requests"] == 0


# --- The route


@pytest.mark.parametrize(
    "role", ["admin", "sponsor", "project_manager", "ethics_reviewer", "auditor"]
)
def test_every_role_with_project_read_sees_the_board(db, web, sign_in, role, settings):
    pm = _user(db)
    _request(db, pm)
    sign_in(web, db, role)
    body = _get(web)
    assert [c["id"] for c in body["columns"]][0] == "requests"
    assert body["columns"][0]["title"] == "Requests"
    assert body["summary"]["planning_value"] == "0.00"
    assert len(body["columns"][0]["cards"]) == 1


def test_the_route_filters_and_validates(db, web, sign_in, settings):
    pm = _user(db)
    _request(db, pm, status="withdrawn")
    sign_in(web, db, "sponsor")
    assert _get(web)["summary"]["counts"]["requests"] == 0
    shown = _get(web, show_stopped="true")
    assert shown["columns"][0]["cards"][0]["stopped"] is True
    assert web.get(URL, params={"waiting": "nobody"}).status_code == 422
    assert web.get(URL, params={"client_id": 0}).status_code == 422


def test_the_route_needs_a_session(db, web):
    assert web.get(URL).status_code == 401


def test_the_route_says_when_a_policy_is_missing(db, web, sign_in, monkeypatch):
    def broken():
        raise policy_module.PolicyError("no file")

    monkeypatch.setattr(policy_module, "get_autonomy", broken)
    sign_in(web, db, "sponsor")
    response = web.get(URL)
    assert response.status_code == 503
    assert "autonomy" in response.json()["detail"]


def test_a_real_clients_answer_is_a_persons_and_the_card_is_not_simulated(db, rules, actions):
    actions(client_answer=Calls())
    pm = _user(db)
    project = _project(db, stage="delivery", client=True, client_email="buyer@client.test")
    proposal_id = _proposal(db, project, pm, "sent", True)
    _sent(db, proposal_id, pm, utcnow() - timedelta(days=1))
    card = _cards(db, rules)[f"project-{project.id}"]
    assert [w.text for w in card.waiting] == ["Waiting on the project manager: client's answer"]
    assert not card.simulated and not card.client_simulated


def test_the_board_route_says_the_client_is_simulated(db, web, rules, sign_in):
    pm = _user(db)
    project = _project(db, stage="delivery", client=True, client_email="buyer@client.example")
    _proposal(db, project, pm, "accepted", True)
    sign_in(web, db, "auditor")
    found = web.get("/api/v1/board").json()
    cards = [c for column in found["columns"] for c in column["cards"]]
    [card] = [c for c in cards if c["key"] == f"project-{project.id}"]
    assert card["client_simulated"] is True and card["simulated"] is True
