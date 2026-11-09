"""Roles, the permissions table and route guards (item 2.2).

The matrix is built from the app's own route list, so a new route is covered as soon as it is
added, and a route without a guard fails `test_every_route_has_a_guard`.
"""

from datetime import date, datetime

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_intake import add_readiness, add_value_case

import app.api.users as users_api
from app.authz import (
    OPEN_PATHS,
    PERMISSIONS,
    READ_ACTIONS,
    Action,
    Role,
    allowed_actions,
    not_own_submission,
    require,
)
from app.baselines import freeze
from app.config import get_settings
from app.db import engine
from app.main import app, create_app
from app.models import (
    AuditDecision,
    Base,
    ChangeRequest,
    Client,
    GateDecision,
    OrchestratorAction,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectAssumption,
    ProjectBrief,
    ProjectPlan,
    ProjectProposal,
    ProjectRequest,
    ProjectStakeholder,
    ProposalVersion,
    Risk,
    User,
)
from app.proposals import plan_facts, refresh_from_plan, values_of
from app.security import CSRF_COOKIE, SESSION_COOKIE, issue_token

CSRF = {"X-CSRF-Token": "test-csrf-value"}


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


def _target(db: Session, role: str = "project_manager", email: str = "target@pmo.example") -> User:
    user = User(email=email, display_name="Target", password_hash="not-a-real-hash", role=role)
    db.add(user)
    db.commit()
    return user


# --- The table


def test_permissions_table_is_exactly_the_agreed_one():
    a = Action
    assert PERMISSIONS == {
        Role.ADMIN: {
            a.USER_ADMIN,
            a.AUDIT_READ,
            a.PROJECT_READ,
            a.STANDARDS_READ,
            a.ORCHESTRATOR_RUN,
        },
        Role.SPONSOR: {
            a.PROJECT_READ,
            a.INTAKE_DECIDE,
            a.GATE_DECIDE,
            a.RISK_ACCEPT,
            a.PROPOSAL_APPROVE,
            a.STANDARDS_READ,
        },
        Role.PROJECT_MANAGER: {
            a.PROJECT_READ,
            a.PROJECT_WRITE,
            a.RISK_ACCEPT,
            a.REPORT_PUBLISH,
            a.STANDARDS_READ,
        },
        Role.ETHICS_REVIEWER: {a.PROJECT_READ, a.GATE_DECIDE, a.AUDIT_READ, a.STANDARDS_READ},
        Role.AUDITOR: {a.PROJECT_READ, a.AUDIT_READ, a.STANDARDS_READ},
    }


def test_roles_and_actions_are_the_spec_values():
    assert [r.value for r in Role] == [
        "admin",
        "sponsor",
        "project_manager",
        "ethics_reviewer",
        "auditor",
    ]
    assert {a.value for a in Action} == {
        "project:read",
        "project:write",
        "intake:decide",
        "risk:accept",
        "gate:decide",
        "audit:read",
        "user:admin",
        "report:publish",
        "proposal:approve",
        "standards:read",
        "orchestrator:run",
    }


def test_auditor_is_read_only_and_every_role_reads_standards():
    assert PERMISSIONS[Role.AUDITOR] <= READ_ACTIONS
    assert all(Action.STANDARDS_READ in actions for actions in PERMISSIONS.values())


def test_admin_holds_no_decision_rights():
    decisions = {
        Action.INTAKE_DECIDE,
        Action.GATE_DECIDE,
        Action.RISK_ACCEPT,
        Action.PROPOSAL_APPROVE,
        Action.REPORT_PUBLISH,
    }
    assert not PERMISSIONS[Role.ADMIN] & decisions


def test_allowed_actions_are_sorted_and_empty_for_an_unknown_role():
    assert allowed_actions("auditor") == ["audit:read", "project:read", "standards:read"]
    assert allowed_actions("pm") == []


def test_not_own_submission():
    not_own_submission(1, 2)
    not_own_submission(1, None)
    with pytest.raises(HTTPException) as caught:
        not_own_submission(3, 3)
    assert caught.value.status_code == 403


# --- Every route has a guard


def _dependency_calls(dependant) -> list:
    calls = []
    for dep in dependant.dependencies:
        calls.append(dep.call)
        calls += _dependency_calls(dep)
    return calls


def _routes(application: FastAPI) -> list:
    """Every route with its full path and effective dependencies (router-level ones included)."""
    return list(iter_route_contexts(application.routes))


def _guard(route):
    """The route's guard (an Action, or "signed_in"), or None."""
    for call in _dependency_calls(route.dependant):
        guard = getattr(call, "pmo_guard", None)
        if guard is not None:
            return guard
    return None


def unguarded_routes(application: FastAPI) -> list[str]:
    offenders = []
    for route in _routes(application):
        path = route.path or repr(route)
        if path in OPEN_PATHS:
            continue
        if not isinstance(route.original_route, APIRoute) or _guard(route) is None:
            offenders.append(path)
    return offenders


def test_every_route_has_a_guard():
    assert unguarded_routes(app) == []


def test_an_unguarded_route_fails_the_check():
    probe = create_app()
    probe.add_api_route("/api/v1/forgotten", lambda: {"ok": True})
    probe.add_api_route(
        "/api/v1/guarded",
        lambda: {"ok": True},
        dependencies=[Depends(require(Action.PROJECT_READ))],
    )
    assert unguarded_routes(probe) == ["/api/v1/forgotten"]


def test_open_paths_are_only_health_login_logout_and_docs():
    assert OPEN_PATHS == {
        "/api/v1/health",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/docs",
        "/redoc",
        "/openapi.json",
    }


@pytest.mark.parametrize(("env", "served"), [("dev", True), ("test", True), ("prod", False)])
def test_docs_are_served_only_in_dev_and_test(monkeypatch, env, served):
    monkeypatch.setattr(get_settings(), "app_env", env)
    web = TestClient(create_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (web.get(path).status_code == 200) is served, path
    assert web.get("/docs/oauth2-redirect").status_code == 404


# --- The matrix: every role x every guarded route


def _guarded_routes() -> list[tuple[str, str, object]]:
    rows = []
    for route in _routes(app):
        if isinstance(route.original_route, APIRoute) and route.path not in OPEN_PATHS:
            for method in sorted(route.methods):
                rows.append((method, route.path, _guard(route)))
    return rows


GUARDED = _guarded_routes()

RISK_BODY = {
    "kind": "threat",
    "title": "Model drift",
    "cause": "Training data ages",
    "effect": "Answers get worse",
    "category": "ai",
    "objectives": ["quality"],
    "probability": 3,
    "impact": 4,
    "response": "reduce",
    "response_plan": "Retrain every quarter",
}


def _project_with_risk(**changes) -> tuple[int, int]:
    """A project holding one risk, for the risk routes' samples."""
    with Session(engine) as session:
        project = Project(name="Matrix", stage="delivery", sponsor="S", pm="P", status="on_track")
        session.add(project)
        session.flush()
        values = {"status": "open", "source": "person", **RISK_BODY, **changes}
        risk = Risk(project_id=project.id, **values)
        session.add(risk)
        session.commit()
        return project.id, risk.id


def _risk_sample(item: bool, body: dict | None = None):
    def sample(_target: int) -> dict:
        project_id, risk_id = _project_with_risk()
        url = f"/api/v1/projects/{project_id}/risks" + (f"/{risk_id}" if item else "")
        return {"url": url, "json": body} if body is not None else {"url": url}

    return sample


REQUEST_BODY = {
    "title": "Warehouse forecasting pilot",
    "asked_for": "A demand forecast for two warehouses",
    "problem": "Stock-outs every quarter",
    "outcome": "Fewer stock-outs",
}


def _project_request(
    requested_by: int,
    status: str = "draft",
    value_case: bool = False,
    uses_ai: bool = False,
    readiness: bool = False,
) -> int:
    """A project request, for the intake routes' samples; with a complete, confirmed value case
    if asked, so it can be submitted, and a confirmed readiness check if asked."""
    with Session(engine) as session:
        request = ProjectRequest(
            requested_by=requested_by, status=status, uses_ai=uses_ai, **REQUEST_BODY
        )
        session.add(request)
        session.commit()
        request_id = request.id
    if value_case:
        add_value_case(request_id, requested_by)
    if readiness:
        add_readiness(request_id, requested_by, requested_by)
    return request_id


def _intake_sample(
    suffix: str = "",
    body: dict | None = None,
    value_case: bool = False,
    uses_ai: bool = False,
    readiness: bool = False,
    status: str = "draft",
):
    def sample(target: int) -> dict:
        rid = _project_request(
            target, status, value_case=value_case, uses_ai=uses_ai, readiness=readiness
        )
        url = f"/api/v1/requests/{rid}{suffix}"
        if callable(body):
            return {"url": url, "json": body(target)}
        return {"url": url, "json": body} if body is not None else {"url": url}

    return sample


def _readiness_body(owner: int) -> dict:
    action = {"description": "Compare with a reorder rule", "owner_id": owner}
    action["due_date"] = "2026-12-01"
    return {
        "version": 1,
        "problem": {"rating": "green", "note": "Worth solving", "actions": []},
        "approach": {"rating": "yellow", "note": "Maybe rules", "actions": [action]},
        "data": {"rating": "red", "note": "No history yet", "actions": []},
    }


def _planning_sample(suffix: str, body: dict | None = None):
    """A project with a brief, a stakeholder and an assumption owned by the target, for the
    project routes' samples. `{stakeholder}` and `{assumption}` in `suffix` take their ids."""

    def sample(target: int) -> dict:
        with Session(engine) as session:
            project = Project(name="Plan", stage="planning", sponsor="S", pm="P", status="on_track")
            session.add(project)
            session.flush()
            stakeholder = ProjectStakeholder(
                project_id=project.id, name="Sponsor", role_or_interest="Sponsor"
            )
            assumption = ProjectAssumption(
                project_id=project.id,
                text="Data exists",
                owner_user_id=target,
                confirm_by=date(2026, 12, 1),
            )
            brief = ProjectBrief(
                project_id=project.id, purpose="Why", objectives="What", decision_rights=[]
            )
            session.add_all([stakeholder, assumption, brief])
            session.commit()
            path = suffix.format(stakeholder=stakeholder.id, assumption=assumption.id)
            url = f"/api/v1/projects/{project.id}{path}"
        if callable(body):
            return {"url": url, "json": body(target)}
        return {"url": url, "json": body} if body is not None else {"url": url}

    return sample


def _plan_sample(suffix: str, body: dict | None = None):
    """A project in planning with a plan holding one milestone, deliverable and estimate line,
    for the plan routes' samples. `{milestone}`, `{deliverable}` and `{line}` in `suffix` take
    their ids. Every item and the plan are at version 1."""

    def sample(target: int) -> dict:
        with Session(engine) as session:
            project = Project(name="Plan", stage="planning", sponsor="S", pm="P", status="on_track")
            session.add(project)
            session.flush()
            plan = ProjectPlan(project_id=project.id, contingency_pct=0)
            session.add(plan)
            session.flush()
            milestone = PlanMilestone(plan_id=plan.id, name="Pilot", target_date=date(2027, 1, 1))
            line = PlanEstimateLine(
                plan_id=plan.id, description="Team", category="people", amount=1
            )
            session.add_all([milestone, line])
            session.flush()
            deliverable = PlanDeliverable(plan_id=plan.id, name="Model", milestone_id=milestone.id)
            session.add(deliverable)
            session.commit()
            path = suffix.format(milestone=milestone.id, deliverable=deliverable.id, line=line.id)
            url = f"/api/v1/projects/{project.id}/plan{path}"
        if callable(body):
            return {"url": url, "json": body(target)}
        return {"url": url, "json": body} if body is not None else {"url": url}

    return sample


PLAN = "/api/v1/projects/{project_id}/plan"
PROPOSAL = "/api/v1/projects/{project_id}/proposal"


def _proposal_sample(
    suffix: str, proposal_status: str | None = "draft", body=None, agent_summary: bool = False
):
    """A client project with a plan and, unless `proposal_status` is None, a proposal in that
    status refreshed from the plan, so the checker has no flags. A proposal past draft has a
    frozen copy submitted by the target project manager, so a sponsor deciding it is not
    deciding their own. With `agent_summary`, its summary is an unreviewed agent draft."""

    def sample(target: int) -> dict:
        with Session(engine) as session:
            client = Client(name="Matrix client")
            session.add(client)
            session.flush()
            project = Project(
                name="Offer",
                client_id=client.id,
                stage="planning",
                sponsor="S",
                pm="P",
                status="on_track",
            )
            session.add(project)
            session.flush()
            plan = ProjectPlan(project_id=project.id, contingency_pct=0)
            session.add(plan)
            session.flush()
            milestone = PlanMilestone(plan_id=plan.id, name="Pilot", target_date=date(2027, 1, 1))
            line = PlanEstimateLine(
                plan_id=plan.id, description="Team", category="people", amount=1
            )
            session.add_all([milestone, line])
            session.flush()
            session.add(PlanDeliverable(plan_id=plan.id, name="Model", milestone_id=milestone.id))
            session.flush()
            if proposal_status is not None:
                proposal = ProjectProposal(
                    project_id=project.id, status=proposal_status, created_by=target
                )
                refresh_from_plan(proposal, plan_facts(session, plan))
                if agent_summary:
                    proposal.summary = "A pilot"
                    proposal.section_sources = {
                        "summary": {"source": "agent", "reviewed_by": None, "reviewed_at": None}
                    }
                session.add(proposal)
                session.flush()
                if proposal_status != "draft":
                    session.add(
                        ProposalVersion(
                            proposal_id=proposal.id,
                            number=1,
                            content=values_of(proposal),
                            submitted_by=target,
                        )
                    )
            session.commit()
            url = f"/api/v1/projects/{project.id}/proposal{suffix}"
        return {"url": url, "json": body} if body is not None else {"url": url}

    return sample


DECISION = {"version": 1, "reason": "Matches the plan and the client's brief"}


def _go_ahead_sample(body: dict | None = None):
    """A project in planning, managed by the target project manager (so a sponsor deciding it is
    not deciding their own), with no client and a complete plan within the brief's top budget, so
    every go-ahead check is met."""

    def sample(target: int) -> dict:
        with Session(engine) as session:
            project = Project(
                name="Go",
                stage="planning",
                sponsor="S",
                pm="P",
                pm_user_id=target,
                status="on_track",
            )
            session.add(project)
            session.flush()
            session.add(
                ProjectBrief(
                    project_id=project.id,
                    purpose="Why",
                    objectives="What",
                    decision_rights=[],
                    budget_high=100,
                )
            )
            plan = ProjectPlan(
                project_id=project.id,
                scope_in="The pilot",
                contingency_pct=0,
                report_period="weekly",
                report_due_day=1,
            )
            session.add(plan)
            session.flush()
            milestone = PlanMilestone(
                plan_id=plan.id, name="Pilot", target_date=date(2027, 1, 1), owner_user_id=target
            )
            session.add_all(
                [
                    milestone,
                    PlanEstimateLine(
                        plan_id=plan.id, description="Team", category="people", amount=1
                    ),
                ]
            )
            session.flush()
            session.add(PlanDeliverable(plan_id=plan.id, name="Model", milestone_id=milestone.id))
            session.commit()
            url = f"/api/v1/projects/{project.id}/gate"
        return {"url": url, "json": body} if body is not None else {"url": url}

    return sample


GO_AHEAD = "/api/v1/projects/{project_id}/gate"
CHANGES = "/api/v1/projects/{project_id}/changes"
CHANGE = f"{CHANGES}/{{change_id}}"
CHANGE_SET = {
    "scope_in": None,
    "scope_out": None,
    "add_deliverables": [],
    "remove_deliverables": [],
    "move_milestones": [],
    "add_lines": [{"description": "More", "category": "people", "amount": "500.00"}],
    "remove_lines": [],
    "change_lines": [],
    "contingency_pct": None,
}


def _change_sample(suffix: str = "", body: dict | None = None, *, status: str | None = None):
    """A project in delivery managed by the target project manager, with baseline 1 (total 1.00)
    made at the go-ahead gate. With `status`, it has a sponsor-level change request in that status
    raised by the caller (a draft or submitted one) or by the target (a submitted one to decide),
    so whoever may act on it isn't acting on their own."""

    def sample(target: int) -> dict:
        _go_ahead_sample()(target)
        with Session(engine) as session:
            project = session.scalar(select(Project).order_by(Project.id.desc()).limit(1))
            project.stage = "delivery"
            plan = session.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
            decision = GateDecision(
                gate_id="go_ahead",
                subject_type="project",
                subject_id=project.id,
                outcome="pass",
                decided_by=target,
                reason="The plan is complete and inside the budget",
                criteria=[],
                policy_version="test",
            )
            session.add(decision)
            session.flush()
            freeze(session, plan, decision.id)
            url = f"/api/v1/projects/{project.id}/changes"
            if status is not None:
                caller = session.scalar(select(User.id).order_by(User.id.desc()).limit(1))
                change = ChangeRequest(
                    project_id=project.id,
                    title="More analysis",
                    reason="The client asked for more",
                    changes=CHANGE_SET,
                    effect={"escalations": ["cost_above_limit"]},
                    decider="sponsor",
                    measured_baseline_number=1,
                    status=status,
                    raised_by=target if suffix == "/decide" else caller,
                )
                session.add(change)
                session.flush()
                url += f"/{change.id}{suffix}"
            session.commit()
        return {"url": url, "json": body} if body is not None else {"url": url}

    return sample


HOLD = "/api/v1/orchestrator/actions/{action_id}/hold"


def _hold_sample(_target: int) -> dict:
    """A pending window action on a proposed risk, held by the project manager (its `who`)."""
    with Session(engine) as session:
        project_id, risk_id = _project_with_risk(status="proposed")
        action = OrchestratorAction(
            step="score_risk",
            subject_type="risk",
            subject_id=str(risk_id),
            mode="window",
            agent="risk_agent",
            who="project_manager",
            reason="the risk is proposed and not scored",
            status="pending",
            due_at=datetime(2030, 1, 1),
            policy_digest="test",
            open_key=f"score_risk:risk:{risk_id}",
        )
        session.add(action)
        session.commit()
        url = f"/api/v1/orchestrator/actions/{action.id}/hold"
    return {"url": url, "json": {"reason": "I will score this risk myself"}}


# Routes with path parameters or a body need a sample request that succeeds when allowed. Each
# takes the id of a project-manager user that the caller may act on.
SAMPLES = {
    ("GET", "/api/v1/projects/{project_id}/risks"): _risk_sample(item=False),
    ("POST", "/api/v1/projects/{project_id}/risks"): _risk_sample(False, RISK_BODY),
    ("GET", "/api/v1/projects/{project_id}/risks/{risk_id}"): _risk_sample(item=True),
    ("PATCH", "/api/v1/projects/{project_id}/risks/{risk_id}"): _risk_sample(
        True, {"version": 1, "status": "closed"}
    ),
    ("GET", "/api/v1/projects/{project_id}/risks/heatmap"): lambda _target: {
        "url": f"/api/v1/projects/{_project_with_risk()[0]}/risks/heatmap",
    },
    ("GET", "/api/v1/projects/{project_id}/risks/{risk_id}/history"): lambda _target: {
        "url": "/api/v1/projects/{}/risks/{}/history".format(*_project_with_risk()),
    },
    ("POST", "/api/v1/projects/{project_id}/risks/from-library"): lambda _target: {
        "url": f"/api/v1/projects/{_project_with_risk()[0]}/risks/from-library",
        "json": {"template_ids": ["ai-model-drift", "project-scope-creep"]},
    },
    # A Low risk, so every role holding risk:accept may accept it.
    ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/accept"): lambda _target: {
        "url": "/api/v1/projects/{}/risks/{}/accept".format(
            *_project_with_risk(probability=1, impact=2, score=2, band="Low")
        ),
        "json": {
            "version": 1,
            "reason": "Small and well understood; we will watch it",
            "review_date": date.today().isoformat(),
        },
    },
    ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/open"): lambda _target: {
        "url": "/api/v1/projects/{}/risks/{}/open".format(*_project_with_risk(status="proposed")),
        "json": {"version": 1},
    },
    ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/reject"): lambda _target: {
        "url": "/api/v1/projects/{}/risks/{}/reject".format(*_project_with_risk(status="proposed")),
        "json": {"version": 1, "reason": "Already covered by another risk"},
    },
    # AGENT_MODE is off in tests, so an allowed caller gets the refusal (409), never a call.
    ("POST", "/api/v1/projects/{project_id}/risks/suggest"): lambda _target: {
        "url": f"/api/v1/projects/{_project_with_risk()[0]}/risks/suggest?dry_run=true",
    },
    # The same here: an allowed caller gets the 409 for AGENT_MODE=off, and nothing is called.
    ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/suggest-score"): lambda _target: {
        "url": "/api/v1/projects/{}/risks/{}/suggest-score?dry_run=true".format(
            *_project_with_risk(status="proposed", probability=None, impact=None)
        ),
    },
    ("POST", "/api/v1/requests"): lambda _target: {
        "url": "/api/v1/requests",
        "json": REQUEST_BODY,
    },
    ("GET", "/api/v1/requests/{request_id}"): _intake_sample(),
    ("GET", "/api/v1/requests/{request_id}/history"): _intake_sample("/history"),
    ("PATCH", "/api/v1/requests/{request_id}"): _intake_sample(
        "", {"version": 1, "title": "Renamed"}
    ),
    ("POST", "/api/v1/requests/{request_id}/submit"): _intake_sample(
        "/submit", {"version": 1}, value_case=True
    ),
    ("GET", "/api/v1/requests/{request_id}/value-case"): _intake_sample(
        "/value-case", value_case=True
    ),
    ("PUT", "/api/v1/requests/{request_id}/value-case"): _intake_sample(
        "/value-case",
        {
            "version": 1,
            "benefits": [{"description": "Fewer stock-outs", "measure": "Count", "target": "6"}],
            "cost_low": "100.00",
            "cost_high": "200.00",
            "cost_method": "Estimate",
            "strategic_fit": "Client goal",
        },
    ),
    ("POST", "/api/v1/requests/{request_id}/withdraw"): _intake_sample("/withdraw", {"version": 1}),
    ("GET", "/api/v1/requests/{request_id}/readiness"): _intake_sample(
        "/readiness", uses_ai=True, readiness=True
    ),
    ("PUT", "/api/v1/requests/{request_id}/readiness"): _intake_sample(
        "/readiness", _readiness_body, uses_ai=True
    ),
    # AGENT_MODE is off in tests, so an allowed caller gets the refusal (409), never a call.
    ("POST", "/api/v1/requests/{request_id}/draft"): _intake_sample("/draft?dry_run=true"),
    ("GET", "/api/v1/requests/{request_id}/gate"): _intake_sample("/gate", value_case=True),
    # Raised by the target project manager, so a sponsor deciding it is not deciding their own.
    # The target project manager is also the new project's project manager.
    ("POST", "/api/v1/requests/{request_id}/gate"): _intake_sample(
        "/gate",
        lambda target: {
            "version": 1,
            "outcome": "pass",
            "reason": "Worth doing and we have the people for it",
            "criteria": [
                {"id": "value_justifies_cost", "met": True, "note": "Pays back in a year"},
                {"id": "people_available", "met": True, "note": "Two analysts free"},
            ],
            "project_manager_id": target,
        },
        value_case=True,
        status="submitted",
    ),
    ("GET", "/api/v1/projects/{project_id}"): _planning_sample(""),
    ("GET", "/api/v1/projects/{project_id}/brief"): _planning_sample("/brief"),
    ("PATCH", "/api/v1/projects/{project_id}/brief"): _planning_sample(
        "/brief", {"version": 1, "scope_out": "Stores outside the pilot"}
    ),
    ("GET", "/api/v1/projects/{project_id}/stakeholders"): _planning_sample("/stakeholders"),
    ("POST", "/api/v1/projects/{project_id}/stakeholders"): _planning_sample(
        "/stakeholders", {"name": "Store managers", "role_or_interest": "Use the forecast"}
    ),
    ("PATCH", "/api/v1/projects/{project_id}/stakeholders/{stakeholder_id}"): _planning_sample(
        "/stakeholders/{stakeholder}", {"version": 1, "how_often": "Weekly"}
    ),
    ("DELETE", "/api/v1/projects/{project_id}/stakeholders/{stakeholder_id}"): _planning_sample(
        "/stakeholders/{stakeholder}?version=1"
    ),
    ("GET", "/api/v1/projects/{project_id}/assumptions"): _planning_sample("/assumptions"),
    ("POST", "/api/v1/projects/{project_id}/assumptions"): _planning_sample(
        "/assumptions",
        lambda target: {
            "text": "Two years of sales history exist",
            "owner_user_id": target,
            "confirm_by": "2026-12-01",
        },
    ),
    ("PATCH", "/api/v1/projects/{project_id}/assumptions/{assumption_id}"): _planning_sample(
        "/assumptions/{assumption}", {"version": 1, "status": "confirmed"}
    ),
    ("DELETE", "/api/v1/projects/{project_id}/assumptions/{assumption_id}"): _planning_sample(
        "/assumptions/{assumption}?version=1"
    ),
    ("GET", PLAN): _plan_sample(""),
    ("PATCH", PLAN): _plan_sample("", {"version": 1, "contingency_pct": 10}),
    ("GET", f"{PLAN}/milestones"): _plan_sample("/milestones"),
    ("POST", f"{PLAN}/milestones"): _plan_sample(
        "/milestones",
        lambda target: {
            "version": 1,
            "name": "Go live",
            "target_date": "2027-06-01",
            "owner_user_id": target,
        },
    ),
    ("PATCH", f"{PLAN}/milestones/{{milestone_id}}"): _plan_sample(
        "/milestones/{milestone}", lambda target: {"version": 1, "owner_user_id": target}
    ),
    ("DELETE", f"{PLAN}/milestones/{{milestone_id}}"): _plan_sample(
        "/milestones/{milestone}?version=1"
    ),
    ("GET", f"{PLAN}/deliverables"): _plan_sample("/deliverables"),
    ("POST", f"{PLAN}/deliverables"): _plan_sample("/deliverables", {"version": 1, "name": "Doc"}),
    ("PATCH", f"{PLAN}/deliverables/{{deliverable_id}}"): _plan_sample(
        "/deliverables/{deliverable}", {"version": 1, "description": "Weekly"}
    ),
    ("DELETE", f"{PLAN}/deliverables/{{deliverable_id}}"): _plan_sample(
        "/deliverables/{deliverable}?version=1"
    ),
    ("GET", f"{PLAN}/estimate-lines"): _plan_sample("/estimate-lines"),
    ("POST", f"{PLAN}/estimate-lines"): _plan_sample(
        "/estimate-lines",
        {"version": 1, "description": "Licence", "category": "software", "amount": "250.00"},
    ),
    ("PATCH", f"{PLAN}/estimate-lines/{{line_id}}"): _plan_sample(
        "/estimate-lines/{line}", {"version": 1, "amount": "2.00"}
    ),
    ("DELETE", f"{PLAN}/estimate-lines/{{line_id}}"): _plan_sample(
        "/estimate-lines/{line}?version=1"
    ),
    ("GET", PROPOSAL): _proposal_sample(""),
    ("POST", PROPOSAL): _proposal_sample("", None),
    ("PATCH", PROPOSAL): _proposal_sample("", body={"version": 1, "summary": "A pilot"}),
    ("POST", f"{PROPOSAL}/refresh"): _proposal_sample("/refresh", body={"version": 1}),
    ("POST", f"{PROPOSAL}/submit"): _proposal_sample("/submit", body={"version": 1}),
    ("POST", f"{PROPOSAL}/approve"): _proposal_sample("/approve", "submitted", DECISION),
    ("POST", f"{PROPOSAL}/return"): _proposal_sample("/return", "submitted", DECISION),
    ("POST", f"{PROPOSAL}/sent"): _proposal_sample(
        "/sent",
        "approved",
        {
            "version": 1,
            "sent_to": "Client contact",
            "sent_on": date.today().isoformat(),
            "means": "Email",
        },
    ),
    ("POST", f"{PROPOSAL}/answer"): _proposal_sample(
        "/answer",
        "sent",
        {"version": 1, "answer": "accepted", "answered_on": date.today().isoformat()},
    ),
    ("POST", f"{PROPOSAL}/withdraw"): _proposal_sample("/withdraw", body=DECISION),
    ("GET", f"{PROPOSAL}/versions"): _proposal_sample("/versions", "submitted"),
    ("POST", f"{PROPOSAL}/draft"): _proposal_sample("/draft?dry_run=true"),
    ("POST", f"{PLAN}/draft"): _plan_sample("/draft?dry_run=true"),
    ("POST", f"{PLAN}/confirm"): _plan_sample("/confirm", {"version": 1}),
    ("POST", f"{PROPOSAL}/sections/{{section}}/confirm"): _proposal_sample(
        "/sections/summary/confirm", body={"version": 1}, agent_summary=True
    ),
    ("GET", GO_AHEAD): _go_ahead_sample(),
    ("POST", GO_AHEAD): _go_ahead_sample(
        {
            "version": 1,
            "outcome": "pass",
            "reason": "The plan is complete and inside the budget",
            "criteria": [
                {"id": "team_and_dates_realistic", "met": True, "note": "Owners agreed dates"},
                {"id": "risks_acceptable", "met": True, "note": "Nothing open we can't carry"},
            ],
        }
    ),
    ("GET", CHANGES): _change_sample(),
    ("POST", CHANGES): _change_sample(
        "",
        {
            "title": "More analysis",
            "reason": "The client asked for more",
            "changes": {"contingency_pct": 5},
        },
    ),
    ("PATCH", CHANGE): _change_sample("", {"version": 1, "title": "Renamed"}, status="draft"),
    ("POST", f"{CHANGE}/submit"): _change_sample("/submit", {"version": 1}, status="draft"),
    ("POST", f"{CHANGE}/withdraw"): _change_sample("/withdraw", {"version": 1}, status="submitted"),
    ("POST", f"{CHANGE}/decide"): _change_sample(
        "/decide",
        {"version": 1, "outcome": "reject", "reason": "Not worth it for this pilot phase"},
        status="submitted",
    ),
    ("POST", HOLD): _hold_sample,
    # ORCHESTRATOR_MODE is off in tests, so an allowed caller gets an empty pass, never a call.
    ("POST", "/api/v1/orchestrator/run"): lambda _target: {"url": "/api/v1/orchestrator/run"},
    ("PATCH", "/api/v1/users/{user_id}/role"): lambda target: {
        "url": f"/api/v1/users/{target}/role",
        "json": {"role": "auditor"},
    },
    ("POST", "/api/v1/users/{user_id}/unlock"): lambda target: {
        "url": f"/api/v1/users/{target}/unlock",
    },
}


def _request(web: TestClient, method: str, path: str, target: int):
    sample = SAMPLES.get((method, path))
    if sample is None:
        assert "{" not in path and method == "GET", f"add a sample request for {method} {path}"
        return web.request(method, path)
    return web.request(method, **sample(target))


# Allowed requests that create something answer 201 rather than 200.
CREATED = {
    ("POST", "/api/v1/projects/{project_id}/risks"),
    ("POST", "/api/v1/projects/{project_id}/risks/from-library"),
    ("POST", "/api/v1/requests"),
    ("POST", "/api/v1/projects/{project_id}/stakeholders"),
    ("POST", "/api/v1/projects/{project_id}/assumptions"),
    ("POST", f"{PLAN}/milestones"),
    ("POST", f"{PLAN}/deliverables"),
    ("POST", f"{PLAN}/estimate-lines"),
    ("POST", PROPOSAL),
    ("POST", CHANGES),
}

# Allowed deletes answer 204.
DELETED = {
    ("DELETE", "/api/v1/projects/{project_id}/stakeholders/{stakeholder_id}"),
    ("DELETE", "/api/v1/projects/{project_id}/assumptions/{assumption_id}"),
    ("DELETE", f"{PLAN}/milestones/{{milestone_id}}"),
    ("DELETE", f"{PLAN}/deliverables/{{deliverable_id}}"),
    ("DELETE", f"{PLAN}/estimate-lines/{{line_id}}"),
}


# Allowed requests refused for another reason: the risk, intake, proposal and planner agents are
# off in tests.
REFUSED = {
    ("POST", "/api/v1/projects/{project_id}/risks/suggest"),
    ("POST", "/api/v1/requests/{request_id}/draft"),
    ("POST", f"{PROPOSAL}/draft"),
    ("POST", f"{PLAN}/draft"),
}

# Routes that also need a role the gates policy lists: other roles holding the action get 403.
# A sponsor-level change request is decided under the go-ahead gate's rules.
GATE_DECIDERS = {
    ("POST", "/api/v1/requests/{request_id}/gate"): {Role.SPONSOR},
    ("POST", GO_AHEAD): {Role.SPONSOR},
    ("POST", f"{CHANGE}/decide"): {Role.SPONSOR},
    # Only the step's `who` may hold it: the project manager, for score_risk.
    ("POST", HOLD): {Role.PROJECT_MANAGER},
}


def _holds(guard, role: Role) -> bool:
    """Whether `role` passes the guard: one action, or any of a set of them."""
    if guard == "signed_in":
        return True
    if isinstance(guard, frozenset):
        return bool(guard & PERMISSIONS[role])
    return guard in PERMISSIONS[role]


def _expected(guard, role: Role, method: str = "GET", path: str = "") -> int:
    if not _holds(guard, role):
        return 403
    if role not in GATE_DECIDERS.get((method, path), set(Role)):
        return 403
    if (method, path) in REFUSED:
        return 409
    if (method, path) in DELETED:
        return 204
    return 201 if (method, path) in CREATED else 200


def test_matrix_covers_the_existing_routes():
    paths = {(method, path) for method, path, _ in GUARDED}
    assert {
        ("GET", "/api/v1/audit"),
        ("GET", "/api/v1/clients"),
        ("GET", "/api/v1/projects"),
        ("GET", "/api/v1/standards"),
        ("GET", "/api/v1/autonomy"),
        ("GET", "/api/v1/auth/me"),
        ("PATCH", "/api/v1/users/{user_id}/role"),
        ("POST", "/api/v1/users/{user_id}/unlock"),
        ("GET", "/api/v1/projects/{project_id}/risks"),
        ("POST", "/api/v1/projects/{project_id}/risks"),
        ("GET", "/api/v1/projects/{project_id}/risks/{risk_id}"),
        ("PATCH", "/api/v1/projects/{project_id}/risks/{risk_id}"),
        ("GET", "/api/v1/risk-library"),
        ("POST", "/api/v1/projects/{project_id}/risks/from-library"),
        ("GET", "/api/v1/projects/{project_id}/risks/heatmap"),
        ("GET", "/api/v1/projects/{project_id}/risks/{risk_id}/history"),
        ("GET", "/api/v1/users"),
        ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/accept"),
        ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/open"),
        ("POST", "/api/v1/projects/{project_id}/risks/{risk_id}/reject"),
        ("GET", "/api/v1/risks/escalations"),
        ("POST", "/api/v1/projects/{project_id}/risks/suggest"),
        ("GET", "/api/v1/requests"),
        ("POST", "/api/v1/requests"),
        ("GET", "/api/v1/requests/{request_id}"),
        ("PATCH", "/api/v1/requests/{request_id}"),
        ("GET", "/api/v1/requests/{request_id}/history"),
        ("POST", "/api/v1/requests/{request_id}/submit"),
        ("POST", "/api/v1/requests/{request_id}/withdraw"),
        ("GET", "/api/v1/requests/{request_id}/value-case"),
        ("PUT", "/api/v1/requests/{request_id}/value-case"),
        ("GET", "/api/v1/requests/{request_id}/readiness"),
        ("PUT", "/api/v1/requests/{request_id}/readiness"),
        ("GET", "/api/v1/requests/{request_id}/gate"),
        ("POST", "/api/v1/requests/{request_id}/gate"),
        ("POST", "/api/v1/requests/{request_id}/draft"),
        ("GET", "/api/v1/projects/{project_id}"),
        ("GET", "/api/v1/projects/{project_id}/brief"),
        ("PATCH", "/api/v1/projects/{project_id}/brief"),
        ("GET", "/api/v1/projects/{project_id}/stakeholders"),
        ("POST", "/api/v1/projects/{project_id}/stakeholders"),
        ("PATCH", "/api/v1/projects/{project_id}/stakeholders/{stakeholder_id}"),
        ("DELETE", "/api/v1/projects/{project_id}/stakeholders/{stakeholder_id}"),
        ("GET", "/api/v1/projects/{project_id}/assumptions"),
        ("POST", "/api/v1/projects/{project_id}/assumptions"),
        ("PATCH", "/api/v1/projects/{project_id}/assumptions/{assumption_id}"),
        ("DELETE", "/api/v1/projects/{project_id}/assumptions/{assumption_id}"),
        ("GET", GO_AHEAD),
        ("POST", GO_AHEAD),
        ("GET", CHANGES),
        ("POST", CHANGES),
        ("PATCH", CHANGE),
        ("POST", f"{CHANGE}/submit"),
        ("POST", f"{CHANGE}/withdraw"),
        ("POST", f"{CHANGE}/decide"),
        ("POST", "/api/v1/orchestrator/run"),
        ("POST", HOLD),
        ("GET", "/api/v1/inbox"),
        ("GET", "/api/v1/inbox/count"),
        ("GET", "/api/v1/board"),
        ("POST", f"{PLAN}/draft"),
        ("POST", f"{PLAN}/confirm"),
    } <= paths
    guards = {path: guard for _, path, guard in GUARDED}
    assert guards["/api/v1/audit"] == Action.AUDIT_READ
    assert guards["/api/v1/clients"] == guards["/api/v1/projects"] == Action.PROJECT_READ
    assert guards["/api/v1/standards"] == Action.STANDARDS_READ
    assert guards["/api/v1/autonomy"] == Action.PROJECT_READ
    assert guards["/api/v1/users/{user_id}/role"] == Action.USER_ADMIN
    assert guards["/api/v1/users/{user_id}/unlock"] == Action.USER_ADMIN
    assert guards["/api/v1/auth/me"] == "signed_in"
    by_method = {(method, path): guard for method, path, guard in GUARDED}
    risks, risk = (
        "/api/v1/projects/{project_id}/risks",
        "/api/v1/projects/{project_id}/risks/{risk_id}",
    )
    assert by_method[("GET", risks)] == by_method[("GET", risk)] == Action.PROJECT_READ
    assert by_method[("POST", risks)] == by_method[("PATCH", risk)] == Action.PROJECT_WRITE
    assert by_method[("GET", "/api/v1/risk-library")] == Action.PROJECT_READ
    assert by_method[("POST", f"{risks}/from-library")] == Action.PROJECT_WRITE
    assert by_method[("GET", f"{risks}/heatmap")] == Action.PROJECT_READ
    assert by_method[("GET", f"{risk}/history")] == Action.PROJECT_READ
    assert by_method[("GET", "/api/v1/users")] == Action.PROJECT_READ
    assert by_method[("POST", f"{risk}/accept")] == Action.RISK_ACCEPT
    assert by_method[("POST", f"{risk}/open")] == Action.PROJECT_WRITE
    assert by_method[("POST", f"{risk}/reject")] == Action.PROJECT_WRITE
    assert by_method[("GET", "/api/v1/risks/escalations")] == Action.PROJECT_READ
    assert by_method[("POST", f"{risks}/suggest")] == Action.PROJECT_WRITE
    requests, request = "/api/v1/requests", "/api/v1/requests/{request_id}"
    for path in (
        requests,
        request,
        f"{request}/history",
        f"{request}/value-case",
        f"{request}/readiness",
        f"{request}/gate",
    ):
        assert by_method[("GET", path)] == Action.PROJECT_READ
    assert by_method[("POST", f"{request}/gate")] == Action.GATE_DECIDE
    for method, path in (
        ("POST", requests),
        ("PATCH", request),
        ("POST", f"{request}/submit"),
        ("POST", f"{request}/withdraw"),
        ("PUT", f"{request}/value-case"),
        ("PUT", f"{request}/readiness"),
        ("POST", f"{request}/draft"),
    ):
        assert by_method[(method, path)] == Action.PROJECT_WRITE
    project = "/api/v1/projects/{project_id}"
    for path in (project, f"{project}/brief", f"{project}/stakeholders", f"{project}/assumptions"):
        assert by_method[("GET", path)] == Action.PROJECT_READ
    for method, path in (
        ("PATCH", f"{project}/brief"),
        ("POST", f"{project}/stakeholders"),
        ("PATCH", f"{project}/stakeholders/{{stakeholder_id}}"),
        ("DELETE", f"{project}/stakeholders/{{stakeholder_id}}"),
        ("POST", f"{project}/assumptions"),
        ("PATCH", f"{project}/assumptions/{{assumption_id}}"),
        ("DELETE", f"{project}/assumptions/{{assumption_id}}"),
    ):
        assert by_method[(method, path)] == Action.PROJECT_WRITE
    lists = ("milestones", "deliverables", "estimate-lines")
    items = ("milestone_id", "deliverable_id", "line_id")
    for path in (PLAN, *(f"{PLAN}/{name}" for name in lists)):
        assert by_method[("GET", path)] == Action.PROJECT_READ
    assert by_method[("PATCH", PLAN)] == Action.PROJECT_WRITE
    assert by_method[("POST", f"{PLAN}/confirm")] == Action.PROJECT_WRITE
    for name, item in zip(lists, items, strict=True):
        assert by_method[("POST", f"{PLAN}/{name}")] == Action.PROJECT_WRITE
        for method in ("PATCH", "DELETE"):
            assert by_method[(method, f"{PLAN}/{name}/{{{item}}}")] == Action.PROJECT_WRITE
    for path in (PROPOSAL, f"{PROPOSAL}/versions"):
        assert by_method[("GET", path)] == Action.PROJECT_READ
    assert by_method[("POST", PROPOSAL)] == by_method[("PATCH", PROPOSAL)] == Action.PROJECT_WRITE
    for name in ("refresh", "submit", "sent", "answer", "withdraw", "draft"):
        assert by_method[("POST", f"{PROPOSAL}/{name}")] == Action.PROJECT_WRITE
    assert by_method[("POST", f"{PROPOSAL}/sections/{{section}}/confirm")] == Action.PROJECT_WRITE
    for name in ("approve", "return"):
        assert by_method[("POST", f"{PROPOSAL}/{name}")] == Action.PROPOSAL_APPROVE
    assert by_method[("GET", GO_AHEAD)] == Action.PROJECT_READ
    assert by_method[("POST", GO_AHEAD)] == Action.GATE_DECIDE
    assert by_method[("GET", CHANGES)] == Action.PROJECT_READ
    for method, path in (
        ("POST", CHANGES),
        ("PATCH", CHANGE),
        ("POST", f"{CHANGE}/submit"),
        ("POST", f"{CHANGE}/withdraw"),
    ):
        assert by_method[(method, path)] == Action.PROJECT_WRITE
    assert by_method[("POST", "/api/v1/orchestrator/run")] == Action.ORCHESTRATOR_RUN
    # Every role reads its own inbox; an admin's or auditor's is always empty.
    assert by_method[("GET", "/api/v1/inbox")] == Action.PROJECT_READ
    assert by_method[("GET", "/api/v1/inbox/count")] == Action.PROJECT_READ
    # Every role that reads projects sees the whole board.
    assert by_method[("GET", "/api/v1/board")] == Action.PROJECT_READ
    # Any role that can be a step's `who` gets past the guard; the route checks it is this one's.
    assert by_method[("POST", HOLD)] == frozenset(
        {Action.PROJECT_WRITE, Action.GATE_DECIDE, Action.PROPOSAL_APPROVE, Action.RISK_ACCEPT}
    )
    # A project manager decides a small change and a sponsor a large one: the route checks which.
    assert by_method[("POST", f"{CHANGE}/decide")] == frozenset(
        {Action.GATE_DECIDE, Action.PROJECT_WRITE}
    )


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize(
    ("method", "path", "guard"), GUARDED, ids=[f"{m} {p}" for m, p, _ in GUARDED]
)
def test_matrix(web, db, sign_in, role, method, path, guard):
    target = _target(db)
    sign_in(web, db, role)
    response = _request(web, method, path, target.id)
    assert response.status_code == _expected(guard, role, method, path), response.text


@pytest.mark.parametrize(
    ("method", "path", "guard"), GUARDED, ids=[f"{m} {p}" for m, p, _ in GUARDED]
)
def test_matrix_without_a_session_is_401(web, db, method, path, guard):
    target = _target(db)
    # A valid CSRF pair, so only the missing session is wrong.
    web.cookies.set(CSRF_COOKIE, CSRF["X-CSRF-Token"])
    web.headers.update(CSRF)
    assert _request(web, method, path, target.id).status_code == 401


def test_a_deactivated_user_is_refused_everywhere(web, db, sign_in):
    user = sign_in(web, db, "admin")
    user.is_active = False
    db.commit()
    for method, path, _ in GUARDED:
        assert _request(web, method, path, user.id).status_code == 401, path


def test_me_lists_the_roles_actions(web, db, sign_in):
    sign_in(web, db, "ethics_reviewer")
    body = web.get("/api/v1/auth/me").json()
    assert body["role"] == "ethics_reviewer"
    assert body["actions"] == ["audit:read", "gate:decide", "project:read", "standards:read"]


# --- Changing a role


def _role_change(web: TestClient, user_id: int, role: str):
    return web.patch(f"/api/v1/users/{user_id}/role", json={"role": role})


def _audit_rows(db: Session) -> list[AuditDecision]:
    db.expire_all()
    return list(db.scalars(select(AuditDecision).order_by(AuditDecision.id)))


def test_admin_changes_a_role_and_the_change_is_one_audit_row(web, db, sign_in):
    admin = sign_in(web, db, "admin")
    target = _target(db)
    response = _role_change(web, target.id, "sponsor")
    assert response.status_code == 200
    assert response.json()["role"] == "sponsor"
    assert "proposal:approve" in response.json()["actions"]
    db.refresh(target)
    assert target.role == "sponsor"

    (row,) = _audit_rows(db)
    assert (row.agent, row.trigger, row.subject_type, row.subject_id) == (
        "auth",
        "human",
        "user",
        str(target.id),
    )
    assert (row.version, row.attempt, row.status) == (1, 1, "ok")
    assert row.actor_user_id == admin.id
    assert row.action_taken == {"action": "role_change", "from": "project_manager", "to": "sponsor"}

    # A second change is the next attempt for the same user.
    assert _role_change(web, target.id, "auditor").status_code == 200
    assert [r.attempt for r in _audit_rows(db)] == [1, 2]


def test_the_new_role_applies_on_the_next_request(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db, role="auditor")
    other = TestClient(app, base_url="http://localhost")
    other.cookies.set(SESSION_COOKIE, issue_token(target.id, "auditor").token)
    assert other.get("/api/v1/audit").status_code == 200
    assert _role_change(web, target.id, "sponsor").status_code == 200
    assert other.get("/api/v1/audit").status_code == 403


def test_nobody_can_change_their_own_role(web, db, sign_in):
    admin = sign_in(web, db, "admin")
    _target(db, role="admin", email="second.admin@pmo.example")
    response = _role_change(web, admin.id, "auditor")
    assert response.status_code == 403
    db.refresh(admin)
    assert admin.role == "admin"
    assert _audit_rows(db) == []


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.ADMIN])
def test_only_admin_can_change_a_role(web, db, sign_in, role):
    sign_in(web, db, role)
    target = _target(db)
    assert _role_change(web, target.id, "sponsor").status_code == 403
    db.refresh(target)
    assert target.role == "project_manager"
    assert _audit_rows(db) == []


@pytest.mark.parametrize("body", [{"role": "pm"}, {"role": "superuser"}, {}, {"role": None}])
def test_an_unknown_role_is_422(web, db, sign_in, body):
    sign_in(web, db, "admin")
    target = _target(db)
    assert web.patch(f"/api/v1/users/{target.id}/role", json=body).status_code == 422
    assert _audit_rows(db) == []


def test_extra_fields_are_refused(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    body = {"role": "sponsor", "is_active": False}
    assert web.patch(f"/api/v1/users/{target.id}/role", json=body).status_code == 422


def test_an_unknown_user_is_404(web, db, sign_in):
    sign_in(web, db, "admin")
    assert _role_change(web, 9999, "sponsor").status_code == 404


def test_the_same_role_again_changes_nothing(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    assert _role_change(web, target.id, "project_manager").status_code == 200
    assert _audit_rows(db) == []


def test_role_change_needs_the_csrf_header(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    del web.headers["X-CSRF-Token"]
    assert _role_change(web, target.id, "sponsor").status_code == 403
    assert _audit_rows(db) == []


def test_admin_can_demote_another_admin_while_one_remains(web, db, sign_in):
    sign_in(web, db, "admin")
    other = _target(db, role="admin", email="second.admin@pmo.example")
    assert _role_change(web, other.id, "auditor").status_code == 200


def test_a_change_that_would_leave_no_active_admin_is_409(web, db, sign_in, monkeypatch):
    # Two admins demote each other at once: the actor is demoted by the other admin after its
    # own request has passed the guard. Its change would leave no admin, so it is refused.
    actor = sign_in(web, db, "admin")
    other = _target(db, role="admin", email="second.admin@pmo.example")
    real = users_api._active_admin_ids

    def demoted_meanwhile(session):
        with Session(engine) as concurrent:
            concurrent.get(User, actor.id).role = "auditor"
            concurrent.commit()
        return real(session)

    monkeypatch.setattr(users_api, "_active_admin_ids", demoted_meanwhile)
    response = _role_change(web, other.id, "auditor")
    assert response.status_code == 409
    db.refresh(other)
    assert other.role == "admin"
    assert _audit_rows(db) == []


def test_no_secret_in_the_role_change_audit_row(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    _role_change(web, target.id, "sponsor")
    (row,) = _audit_rows(db)
    text = repr([getattr(row, c) for c in AuditDecision.__table__.columns.keys()])
    secrets = (web.cookies[SESSION_COOKIE], CSRF["X-CSRF-Token"], target.password_hash)
    for secret in secrets:
        assert secret not in text
    assert row.action_taken == {"action": "role_change", "from": "project_manager", "to": "sponsor"}


# --- Unlocking an account


def _locked_target(db: Session) -> User:
    target = _target(db)
    target.locked_until = datetime(2999, 1, 1)
    target.failed_logins = 3
    target.failed_window_started_at = datetime(2026, 11, 1)
    db.commit()
    return target


def test_admin_unlocks_an_account_and_the_unlock_is_one_audit_row(web, db, sign_in):
    admin = sign_in(web, db, "admin")
    target = _locked_target(db)
    response = web.post(f"/api/v1/users/{target.id}/unlock")
    assert response.status_code == 200
    db.refresh(target)
    assert target.locked_until is None
    assert target.failed_logins == 0
    assert target.failed_window_started_at is None

    (row,) = _audit_rows(db)
    assert (row.agent, row.trigger, row.subject_type, row.subject_id, row.status) == (
        "auth",
        "human",
        "user",
        str(target.id),
        "ok",
    )
    assert row.actor_user_id == admin.id
    assert row.signals == {"event": "unlock"}
    assert row.action_taken == {"action": "unlock", "was_locked": True}


def test_unlocking_an_expired_lock_records_was_locked_false(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    target.locked_until = datetime(2000, 1, 1)
    db.commit()
    assert web.post(f"/api/v1/users/{target.id}/unlock").status_code == 200
    (row,) = _audit_rows(db)
    assert row.action_taken == {"action": "unlock", "was_locked": False}


def test_unlocking_an_account_that_was_never_locked_records_was_locked_false(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _target(db)
    assert web.post(f"/api/v1/users/{target.id}/unlock").status_code == 200
    (row,) = _audit_rows(db)
    assert row.action_taken == {"action": "unlock", "was_locked": False}


def test_unlock_and_role_change_share_the_attempt_sequence(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _locked_target(db)
    assert web.post(f"/api/v1/users/{target.id}/unlock").status_code == 200
    assert _role_change(web, target.id, "auditor").status_code == 200
    assert [r.attempt for r in _audit_rows(db)] == [1, 2]


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.ADMIN])
def test_only_admin_can_unlock(web, db, sign_in, role):
    sign_in(web, db, role)
    target = _locked_target(db)
    assert web.post(f"/api/v1/users/{target.id}/unlock").status_code == 403
    db.refresh(target)
    assert target.locked_until is not None
    assert _audit_rows(db) == []


def test_an_admin_cannot_unlock_their_own_account(web, db, sign_in):
    admin = sign_in(web, db, "admin")
    admin.locked_until = datetime(2999, 1, 1)
    db.commit()
    assert web.post(f"/api/v1/users/{admin.id}/unlock").status_code == 403
    db.refresh(admin)
    assert admin.locked_until is not None
    assert _audit_rows(db) == []


def test_unlock_of_an_unknown_user_is_404(web, db, sign_in):
    sign_in(web, db, "admin")
    assert web.post("/api/v1/users/9999/unlock").status_code == 404


def test_unlock_needs_the_csrf_header(web, db, sign_in):
    sign_in(web, db, "admin")
    target = _locked_target(db)
    del web.headers["X-CSRF-Token"]
    assert web.post(f"/api/v1/users/{target.id}/unlock").status_code == 403
    assert _audit_rows(db) == []
