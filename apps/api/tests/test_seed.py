import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import AuditDecision, Base, Client, Project, Risk
from app.models.portfolio import STAGES, STATUSES
from app.policy import load_policies, score_risk
from app.schemas.risk import RiskFields
from app.seed import (
    BRIEFS,
    CHANGE,
    CLIENTS,
    PLANS,
    PROJECTS,
    PROPOSALS,
    REQUESTS,
    RISKS,
    SEEDED_AT,
    clear_seeded,
    demo_users,
    main,
    seed,
)

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1]
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[\w-]+")
URL = re.compile(r"https?://([\w-]+\.)+[\w-]+")


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


def _snapshot(db: Session) -> list[tuple]:
    """Every seeded row's values, but not `version`: it counts a row's changes, so restoring a
    drifted row moves it on."""
    clients = db.execute(select(Client).order_by(Client.id)).scalars()
    projects = db.execute(select(Project).order_by(Project.id)).scalars()
    risks = db.execute(select(Risk).order_by(Risk.id)).scalars()
    return [
        tuple(getattr(row, col) for col in row.__table__.columns.keys() if col != "version")
        for row in [*clients, *projects, *risks]
    ]


def _risk_audit(db: Session) -> list[AuditDecision]:
    stmt = select(AuditDecision).where(AuditDecision.subject_type == "risk")
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


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
    assert _count(db, Project) == len(PROJECTS)
    sync = db.scalar(select(Project).where(Project.name == "PragMattie Sync"))
    assert sync is not None and sync.client_id is None
    client_projects = db.scalars(select(Project).where(Project.client_id.is_not(None))).all()
    assert len({p.client_id for p in client_projects}) == 3


def test_seeded_projects_use_known_stages_and_statuses():
    for project in PROJECTS:
        assert project["stage"] in STAGES, project["seed_key"]
        assert project["status"] in STATUSES, project["seed_key"]


def _strings(value) -> list[str]:
    """Every string in a seed constant, however deeply it is nested."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list | tuple | set):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


def test_every_email_and_website_is_on_example(db):
    seed(db)
    db.commit()
    # Check the data itself, every seed constant (so an address added to any of them later fails
    # here), and what was stored, including any address hidden in a text field.
    constants = (CLIENTS, PROJECTS, REQUESTS, BRIEFS, PLANS, PROPOSALS, CHANGE, RISKS, demo_users())
    texts = _strings(constants)
    texts += [str(v) for row in _snapshot(db) for v in row if isinstance(v, str)]
    emails = [m.group(0) for t in texts for m in EMAIL.finditer(t)]
    assert emails, "seed should include contact emails"
    assert all(email.endswith(".example") for email in emails), emails
    user_emails = [user["email"] for user in demo_users()]
    assert len(user_emails) >= 2 and all(e.endswith(".example") for e in user_emails), user_emails
    hosts = [m.group(0) for t in texts for m in URL.finditer(t)]
    assert hosts, "seed should include client websites"
    assert all(host.endswith(".example") for host in hosts), hosts
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


# --- Seeded risks


def test_every_project_with_seeded_risks_has_ai_and_ordinary_ones(db):
    seed(db)
    db.commit()
    keys = {values["project"] for values in RISKS}
    assert len(keys) == 4
    for project in db.scalars(select(Project).where(Project.seed_key.in_(keys))):
        risks = db.scalars(select(Risk).where(Risk.project_id == project.id)).all()
        assert len(risks) >= 2, project.name
        assert {risk.category for risk in risks} == {"ai", "project"}, project.name


def test_every_band_has_a_seeded_risk(db):
    seed(db)
    db.commit()
    bands = [band.name for band in load_policies(REPO_ROOT / "policies").risk.priority_bands]
    assert bands == ["Low", "Medium", "High"]
    assert set(db.scalars(select(Risk.band))) == set(bands)


def test_seeded_risks_are_scored_from_the_policy_and_valid(db):
    policy = load_policies(REPO_ROOT / "policies").risk
    seed(db)
    db.commit()
    for risk in db.scalars(select(Risk)):
        assert (risk.score, risk.band) == score_risk(policy, risk.probability, risk.impact)
        assert (risk.status, risk.source, risk.needs_judgement) == ("open", "person", False)
        RiskFields.model_validate({name: getattr(risk, name) for name in RiskFields.model_fields})
    assert len({values["seed_key"] for values in RISKS}) == len(RISKS)


def test_seeding_risks_is_idempotent_and_audited(db):
    seed(db)
    db.commit()
    first = _snapshot(db)
    created = _risk_audit(db)
    assert len(created) == len(RISKS)
    assert {row.action_taken["action"] for row in created} == {"create"}
    assert all(row.actor_user_id is None for row in created)

    # Unchanged: no new rows and no new audit rows.
    seed(db)
    db.commit()
    assert _snapshot(db) == first
    assert len(_risk_audit(db)) == len(RISKS)

    # A drifted seeded risk is set back, with one audit row.
    drifted = db.scalar(select(Risk).where(Risk.band == "High").order_by(Risk.id))
    drifted.probability, drifted.impact, drifted.score, drifted.band = 1, 1, 1, "Low"
    db.commit()
    seed(db)
    db.commit()
    db.refresh(drifted)
    assert drifted.band == "High"
    rows = _risk_audit(db)
    assert len(rows) == len(RISKS) + 1
    assert rows[-1].subject_id == str(drifted.id)
    assert rows[-1].action_taken == {"action": "seed"}
    assert rows[-1].signals["band"] == "Low" and rows[-1].output["band"] == "High"


def test_seeding_leaves_a_persons_risk_alone(db):
    seed(db)
    project = db.scalar(select(Project).where(Project.seed_key == "project-pragmattie-sync"))
    own = Risk(
        project_id=project.id,
        kind="threat",
        title="A person's own risk",
        cause="c",
        effect="e",
        category="project",
        objectives=["cost"],
        response="accept",
        response_plan="p",
        status="proposed",
        source="person",
    )
    db.add(own)
    db.commit()
    seed(db)
    db.commit()
    db.refresh(own)
    assert own.seed_key is None and own.status == "proposed" and own.version == 1

    # --reset clears everything on a seeded project, whoever added it, so it restores exactly
    # the seeded state.
    cleared = clear_seeded(db)
    assert (cleared.clients, cleared.projects, cleared.requests) == (3, len(PROJECTS), 2)
    db.commit()
    assert _count(db, Risk) == 0
    seed(db)
    db.commit()
    assert _count(db, Project) == len(PROJECTS) and _count(db, Risk) == len(RISKS)


def test_reset_clears_only_seeded_rows(db):
    seed(db)
    own_client = Client(name="A Person's Client Ltd", website="https://own.example")
    db.add(own_client)
    db.flush()
    db.add(_own_project(own_client.id))
    db.commit()

    cleared = clear_seeded(db)
    assert (cleared.clients, cleared.projects, cleared.requests) == (3, len(PROJECTS), 2)
    db.commit()
    assert db.scalars(select(Client.name)).all() == ["A Person's Client Ltd"]
    assert db.scalars(select(Project.name)).all() == ["Own"]


def _plans_per_project(db: Session) -> dict[int, int]:
    from app.models import ProjectPlan

    counts = dict(
        db.execute(
            select(ProjectPlan.project_id, func.count()).group_by(ProjectPlan.project_id)
        ).all()
    )
    return {project_id: counts.get(project_id, 0) for project_id in db.scalars(select(Project.id))}


def test_every_seeded_project_has_exactly_one_plan(db):
    """The seed gives each project its plan through `new_plan`, as for any new project (5.1):
    complete where the data has one, otherwise empty with its scope from the brief, if any."""
    from app.models import ProjectPlan
    from app.plans import missing

    seed(db)
    db.commit()
    counts = _plans_per_project(db)
    assert len(counts) == len(PROJECTS)
    assert set(counts.values()) == {1}
    for project in db.scalars(select(Project)):
        plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
        brief = BRIEFS.get(project.seed_key, {})
        assert plan.scope_in == brief.get("scope_in"), project.seed_key
        if project.seed_key in PLANS:
            assert missing(db, plan) == [], project.seed_key
        else:
            assert (plan.contingency_pct, plan.report_period) == (0, None), project.seed_key

    seed(db)  # Seeding again adds no second plan.
    db.commit()
    assert set(_plans_per_project(db).values()) == {1}

    seed(db, reset=True)
    db.commit()
    counts = _plans_per_project(db)
    assert len(counts) == len(PROJECTS)
    assert set(counts.values()) == {1}


def test_a_seeded_projects_plan_is_prefilled_from_its_brief(db):
    from app.models import ProjectBrief, ProjectPlan

    seed(db)
    db.flush()
    # A project in closure: the seed gives it no brief.
    project = db.scalar(select(Project).where(Project.seed_key == "project-quillmere-yard"))
    db.execute(delete(ProjectPlan).where(ProjectPlan.project_id == project.id))
    db.add(
        ProjectBrief(
            project_id=project.id,
            purpose="Plan better",
            objectives="Forecast weekly",
            scope_in="Forecasts",
            scope_out="Billing",
            decision_rights=[],
        )
    )
    db.commit()
    seed(db)
    db.commit()
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    assert (plan.scope_in, plan.scope_out) == ("Forecasts", "Billing")


def test_reset_also_removes_the_plans_of_the_projects_it_deletes(db):
    """Every seeded project has a plan (item 5.1); a reset that deletes it deletes the plan."""
    from datetime import date

    from app.models import PlanDeliverable, PlanEstimateLine, PlanMilestone, ProjectPlan

    seed(db)
    db.flush()
    plan = db.scalars(select(ProjectPlan).order_by(ProjectPlan.id)).first()
    milestone = PlanMilestone(plan_id=plan.id, name="Pilot", target_date=date(2027, 3, 1))
    db.add(milestone)
    db.flush()
    db.add_all(
        [
            PlanDeliverable(plan_id=plan.id, name="Forecast", milestone_id=milestone.id),
            PlanEstimateLine(plan_id=plan.id, description="Team", category="people", amount=1),
        ]
    )
    db.commit()

    clear_seeded(db)
    db.commit()
    for model in (ProjectPlan, PlanMilestone, PlanDeliverable, PlanEstimateLine):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__


def test_reset_removes_a_persons_proposal_on_a_seeded_project(db):
    """A reset restores exactly the seeded state: a proposal a person added to the seeded project
    whose plan waits for one is removed, so the orchestrator drafts it again."""
    from app.models import ProjectPlan, ProjectProposal, User
    from app.proposals import plan_facts, refresh_from_plan

    seed(db)
    db.flush()
    person = User(email="pm@pmo.example", display_name="PM", password_hash="x", role="sponsor")
    project = db.scalar(select(Project).where(Project.seed_key == "project-brackenfold-intake"))
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    db.add(person)
    db.flush()
    proposal = ProjectProposal(project_id=project.id, status="draft", created_by=person.id)
    refresh_from_plan(proposal, plan_facts(db, plan))
    db.add(proposal)
    db.commit()

    seed(db, reset=True)
    db.commit()
    intake = db.scalar(select(Project).where(Project.seed_key == "project-brackenfold-intake"))
    proposals = select(ProjectProposal).where(ProjectProposal.project_id == intake.id)
    assert db.scalar(proposals) is None
    assert db.get(User, person.id) is not None


def test_reset_keeps_a_seeded_client_that_a_persons_project_uses(db):
    seed(db)
    db.commit()
    quillmere = db.scalar(select(Client).where(Client.seed_key == "client-quillmere"))
    db.add(_own_project(quillmere.id))
    db.commit()

    cleared = clear_seeded(db)
    assert (cleared.clients, cleared.projects) == (2, len(PROJECTS))
    db.commit()
    assert db.scalars(select(Client.seed_key)).all() == ["client-quillmere"]
    # Seeding again reuses that client rather than duplicating it.
    seed(db, reset=True)
    db.commit()
    assert _count(db, Client) == 3 and _count(db, Project) == len(PROJECTS) + 1


def test_reset_then_seed_gives_the_same_data(db):
    seed(db)
    db.commit()
    names = sorted(db.scalars(select(Project.name)))
    result = seed(db, reset=True)
    db.commit()
    assert (result.cleared.clients, result.cleared.projects) == (3, len(PROJECTS))
    assert result.cleared.requests == len(REQUESTS)
    assert sorted(db.scalars(select(Project.name))) == names
    assert _count(db, Client) == 3


def test_main_seeds_and_resets(db, capsys):
    assert main([]) == 0
    assert main([]) == 0
    assert main(["--reset"]) == 0
    out = capsys.readouterr().out
    assert f"Cleared 3 clients, {len(PROJECTS)} projects, {len(REQUESTS)} requests, " in out
    assert "0 orchestrator actions and 0 inbox items." in out
    seeded = f"Seeded 3 clients, {len(PROJECTS)} projects, {len(REQUESTS)} requests and "
    assert f"{seeded}{len(RISKS)} risks." in out
    assert _count(db, Client) == 3 and _count(db, Project) == len(PROJECTS)
    assert _count(db, Risk) == len(RISKS)


def test_main_reports_a_policy_failure_without_writing(db, capsys, monkeypatch):
    import app.seed as seed_module
    from app.policy import PolicyError

    def broken():
        raise PolicyError("risk.yaml: not valid YAML")

    monkeypatch.setattr(seed_module, "risk_policy", broken)
    assert main([]) == 1
    assert "a policy could not be loaded" in capsys.readouterr().err
    assert _count(db, Client) == 0 and _count(db, Risk) == 0


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
    for offset in range(0, len(PROJECTS), 2):
        page = client.get("/api/v1/projects", params={"limit": 2, "offset": offset}).json()
        assert page["total"] == len(PROJECTS)
        seen += [p["id"] for p in page["items"]]
    assert len(seen) == len(set(seen)) == len(PROJECTS)
    sync = next(p for p in client.get("/api/v1/projects").json()["items"] if p["client_id"] is None)
    assert sync["name"] == "PragMattie Sync"


@pytest.mark.parametrize("path", ["/api/v1/clients", "/api/v1/projects"])
def test_api_lists_are_read_only_and_validate_paging(db, client, sign_in, path):
    sign_in(client, db, "auditor")
    assert client.get(path, params={"limit": 101}).status_code == 422
    assert client.get(path, params={"offset": -1}).status_code == 422
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(path).status_code == 405


# --- The agentic flow (item 6.9): board columns, next steps, two project managers, reset


def _seed_key_of(db: Session, subject_type: str, subject_id: int) -> str | None:
    """The seed_key of a next step's request or project; a proposal or change request gives its
    project's."""
    from app.models import ChangeRequest, ProjectProposal, ProjectRequest

    if subject_type == "request":
        return db.get(ProjectRequest, subject_id).seed_key
    if subject_type in ("proposal", "change_request"):
        model = ProjectProposal if subject_type == "proposal" else ChangeRequest
        subject_id = db.get(model, subject_id).project_id
    elif subject_type != "project":
        return None
    return db.get(Project, subject_id).seed_key


def _steps_by_seed_key(db: Session) -> dict[str, set[tuple[str, str]]]:
    from app import policy as policy_module
    from app.orchestrator import next_steps
    from app.risks import risk_policy

    db.expire_all()
    rules = policy_module.get_autonomy()
    found: dict[str, set[tuple[str, str]]] = {}
    for step in next_steps(db, rules, agent_mode="enforce", risk_rules=risk_policy()):
        key = _seed_key_of(db, step.subject_type, step.subject_id)
        found.setdefault(key, set()).add((step.subject_type, step.step))
    return found


def _board_columns(db: Session) -> dict[str, list[str]]:
    from app import board
    from app import policy as policy_module
    from app.board import Filters
    from app.risks import risk_policy

    db.expire_all()
    found = board.board(
        db,
        policy_module.get_autonomy(),
        Filters(),
        agent_mode="enforce",
        risk_rules=risk_policy(),
    )
    return {column: [card.key for card in cards] for column, cards in found.columns.items()}


def _demo_user(db: Session, key: str):
    from app.models import User
    from app.seed import demo_users

    email = next(u["email"] for u in demo_users() if u["key"] == key)
    return db.scalar(select(User).where(User.email == email))


def _drift(db: Session) -> None:
    """Move the demo on: work the orchestrator and people might have done since the seed."""
    from app.models import ChangeRequest, InboxItem, OrchestratorAction, ProjectRequest

    db.execute(delete(ChangeRequest))
    queues = db.scalar(
        select(ProjectRequest).where(ProjectRequest.seed_key == "request-larchwick-queues")
    )
    queues.status = "withdrawn"
    db.scalar(select(Project).where(Project.seed_key == "project-quillmere-yard")).stage = "closed"
    action = OrchestratorAction(
        step="draft_plan",
        subject_type="project",
        subject_id="4",
        mode="agent",
        agent="planner",
        who="agent",
        reason="the plan is incomplete",
        due_at=SEEDED_AT,
        policy_digest="0" * 64,
        open_key="draft_plan:project:4",
    )
    db.add(action)
    db.flush()
    db.add(
        InboxItem(
            step="pass_gate",
            subject_type="request",
            subject_id="2",
            who="sponsor",
            reason="the request is waiting at the approval gate",
            policy_digest="0" * 64,
            open_key="pass_gate:request:2",
            action_id=action.id,
        )
    )
    db.commit()


# The orchestrator's next steps for each seeded item, as (subject type, step).
EXPECTED_STEPS = {
    # A draft request with no value case: the intake agent drafts one.
    "request-brackenfold-referrals": {("request", "draft_value_case")},
    # A submitted request ready for its approval gate.
    "request-larchwick-queues": {("request", "pass_gate")},
    # In planning with an empty plan: the planner agent drafts it.
    "project-pragmattie-sync": {("project", "draft_plan")},
    # A complete plan and no proposal: the proposal agent drafts one.
    "project-brackenfold-intake": {("project", "draft_proposal")},
    # A sent proposal: the simulated client answers it.
    "project-brackenfold-rota": {("proposal", "client_answer")},
    # Ready for its go-ahead gate.
    "project-larchwick-onboarding": {("project", "pass_gate")},
    # In delivery with a pending change request, decided by a project manager.
    "project-quillmere-routing": {("change_request", "decide_change_pm")},
}


def test_after_a_reset_every_board_column_has_a_card(db):
    from app.board import COLUMNS

    seed(db)
    db.commit()
    _drift(db)
    seed(db, reset=True)
    db.commit()
    columns = _board_columns(db)
    assert list(columns) == list(COLUMNS)
    assert all(columns.values()), columns


def test_after_a_reset_the_orchestrators_next_steps_match_the_seeded_states(db):
    seed(db)
    db.commit()
    _drift(db)
    seed(db, reset=True)
    db.commit()
    found = _steps_by_seed_key(db)
    for seed_key, expected in EXPECTED_STEPS.items():
        assert expected <= found.get(seed_key, set()), (seed_key, found.get(seed_key))
    # The projects in closure and closed wait on nobody.
    assert "project-quillmere-yard" not in found
    assert "project-larchwick-statements" not in found
    # Nothing waits on anything but the seeded items.
    assert set(found) <= set(EXPECTED_STEPS)


def test_reset_restores_the_same_board_and_next_steps(db):
    seed(db)
    db.commit()
    columns, steps = _board_columns(db), _steps_by_seed_key(db)
    _drift(db)
    assert _steps_by_seed_key(db) != steps
    seed(db, reset=True)
    db.commit()
    # Ids move on (they are never reused), so compare each card's column by count.
    assert {c: len(keys) for c, keys in _board_columns(db).items()} == {
        c: len(keys) for c, keys in columns.items()
    }
    assert _steps_by_seed_key(db) == steps


def test_reset_clears_orchestrator_actions_and_inbox_items_and_keeps_every_audit_row(db):
    from app.models import InboxItem, OrchestratorAction

    seed(db)
    db.commit()
    _drift(db)
    before = list(db.scalars(select(AuditDecision.id).order_by(AuditDecision.id)))
    result = seed(db, reset=True)
    db.commit()
    assert (result.cleared.orchestrator_actions, result.cleared.inbox_items) == (1, 1)
    assert _count(db, OrchestratorAction) == 0 and _count(db, InboxItem) == 0
    after = list(db.scalars(select(AuditDecision.id).order_by(AuditDecision.id)))
    assert after[: len(before)] == before  # nothing deleted or reordered
    row = db.scalar(
        select(AuditDecision)
        .where(AuditDecision.subject_type == "demo_portfolio")
        .order_by(AuditDecision.id.desc())
    )
    assert row.action_taken["action"] == "reset_portfolio"
    assert row.action_taken["cleared"]["orchestrator_actions"] == 1
    assert row.action_taken["cleared"]["inbox_items"] == 1
    assert row.action_taken["cleared"]["projects"] == len(PROJECTS)
    assert row.actor_user_id is None


def test_seeded_clients_are_simulated(db):
    seed(db)
    db.commit()
    assert [c.simulated for c in db.scalars(select(Client))] == [True] * len(CLIENTS)


def test_two_project_managers_exist_and_each_seeded_request_names_a_demo_user(db):
    from app.models import ProjectRequest, User

    seed(db, reset=True)
    db.commit()
    managers = db.scalars(select(User).where(User.role == "project_manager")).all()
    assert len(managers) == 2
    assert all(m.email.endswith(".example") and m.is_active for m in managers)
    ids = {m.id for m in managers}
    assert {p.pm_user_id for p in db.scalars(select(Project))} == ids
    assert {r.requested_by for r in db.scalars(select(ProjectRequest))} <= ids


def test_the_seeded_change_is_decided_by_the_other_project_manager(db, client):
    from app.models import ChangeRequest
    from app.security import CSRF_COOKIE, SESSION_COOKIE, issue_token

    seed(db, reset=True)
    db.commit()
    change = db.scalar(select(ChangeRequest))
    project = db.get(Project, change.project_id)
    raiser, own_pm = _demo_user(db, "project_manager_2"), _demo_user(db, "project_manager")
    assert change.status == "submitted" and change.decider == "project_manager"
    assert change.raised_by == raiser.id and project.pm_user_id == own_pm.id

    def decide_as(user):
        client.cookies.set(SESSION_COOKIE, issue_token(user.id, user.role).token)
        client.cookies.set(CSRF_COOKIE, "test-csrf-value")
        client.headers["X-CSRF-Token"] = "test-csrf-value"
        return client.post(
            f"/api/v1/projects/{project.id}/changes/{change.id}/decide",
            json={
                "version": change.version,
                "outcome": "approve",
                "reason": "The stock count week can't be moved; three days is fine.",
            },
        )

    # The project manager who raised it can't decide it; the project's own one can.
    assert decide_as(raiser).status_code == 403
    response = decide_as(own_pm)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
