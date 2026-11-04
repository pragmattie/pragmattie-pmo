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
from app.seed import CLIENTS, PROJECTS, RISKS, clear_seeded, main, seed

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1]
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+[\w-]+")


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


def _snapshot(db: Session) -> list[tuple]:
    clients = db.execute(select(Client).order_by(Client.id)).scalars()
    projects = db.execute(select(Project).order_by(Project.id)).scalars()
    risks = db.execute(select(Risk).order_by(Risk.id)).scalars()
    return [
        tuple(getattr(row, col) for col in row.__table__.columns.keys())
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
    assert _count(db, Project) == 4
    sync = db.scalar(select(Project).where(Project.name == "PragMattie Sync"))
    assert sync is not None and sync.client_id is None
    client_projects = db.scalars(select(Project).where(Project.client_id.is_not(None))).all()
    assert len({p.client_id for p in client_projects}) == 3


def test_seeded_projects_use_known_stages_and_statuses():
    for project in PROJECTS:
        assert project["stage"] in STAGES, project["seed_key"]
        assert project["status"] in STATUSES, project["seed_key"]


def test_every_email_and_website_is_on_example(db):
    seed(db)
    db.commit()
    # Check the data itself and what was stored, including any address hidden in a text field.
    texts = [str(v) for row in (*CLIENTS, *PROJECTS) for v in row.values() if v]
    texts += [str(v) for row in _snapshot(db) for v in row if isinstance(v, str)]
    emails = [m.group(0) for t in texts for m in EMAIL.finditer(t)]
    assert emails, "seed should include contact emails"
    assert all(email.endswith(".example") for email in emails), emails
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


def test_every_seeded_project_has_ai_and_ordinary_risks(db):
    seed(db)
    db.commit()
    for project in db.scalars(select(Project)):
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

    # --reset keeps the project that the person's risk is on, and seeding restores it.
    assert clear_seeded(db) == (3, 3)  # the internal project has no client
    db.commit()
    assert db.scalars(select(Risk.title)).all() == ["A person's own risk"]
    seed(db)
    db.commit()
    assert _count(db, Project) == 4 and _count(db, Risk) == len(RISKS) + 1


def test_reset_clears_only_seeded_rows(db):
    seed(db)
    own_client = Client(name="A Person's Client Ltd", website="https://own.example")
    db.add(own_client)
    db.flush()
    db.add(_own_project(own_client.id))
    db.commit()

    assert clear_seeded(db) == (3, 4)
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


def test_every_seeded_project_has_exactly_one_empty_plan(db):
    """The seed gives each project its plan through `new_plan`, as for any new project (5.1)."""
    from app.models import ProjectPlan

    seed(db)
    db.commit()
    counts = _plans_per_project(db)
    assert len(counts) == len(PROJECTS)
    assert set(counts.values()) == {1}
    for plan in db.scalars(select(ProjectPlan)):
        # No seeded project has a brief, so the scope starts empty.
        assert (plan.scope_in, plan.contingency_pct, plan.report_period) == (None, 0, None)

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
    project = db.scalars(select(Project).order_by(Project.id)).first()
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


def test_reset_keeps_a_seeded_client_that_a_persons_project_uses(db):
    seed(db)
    db.commit()
    quillmere = db.scalar(select(Client).where(Client.seed_key == "client-quillmere"))
    db.add(_own_project(quillmere.id))
    db.commit()

    assert clear_seeded(db) == (2, 4)
    db.commit()
    assert db.scalars(select(Client.seed_key)).all() == ["client-quillmere"]
    # Seeding again reuses that client rather than duplicating it.
    seed(db, reset=True)
    db.commit()
    assert _count(db, Client) == 3 and _count(db, Project) == 5


def test_reset_then_seed_gives_the_same_data(db):
    seed(db)
    db.commit()
    names = sorted(db.scalars(select(Project.name)))
    result = seed(db, reset=True)
    db.commit()
    assert (result.cleared_clients, result.cleared_projects) == (3, 4)
    assert sorted(db.scalars(select(Project.name))) == names
    assert _count(db, Client) == 3


def test_main_seeds_and_resets(db, capsys):
    assert main([]) == 0
    assert main([]) == 0
    assert main(["--reset"]) == 0
    out = capsys.readouterr().out
    assert "Cleared 3 clients and 4 projects." in out
    assert f"Seeded 3 clients, 4 projects and {len(RISKS)} risks." in out
    assert _count(db, Client) == 3 and _count(db, Project) == 4
    assert _count(db, Risk) == len(RISKS)


def test_main_reports_a_policy_failure_without_writing(db, capsys, monkeypatch):
    import app.seed as seed_module
    from app.policy import PolicyError

    def broken():
        raise PolicyError("risk.yaml: not valid YAML")

    monkeypatch.setattr(seed_module, "risk_policy", broken)
    assert main([]) == 1
    assert "the risk policy could not be loaded" in capsys.readouterr().err
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
    for offset in (0, 2):
        page = client.get("/api/v1/projects", params={"limit": 2, "offset": offset}).json()
        assert page["total"] == 4
        seen += [p["id"] for p in page["items"]]
    assert len(set(seen)) == 4
    sync = next(p for p in client.get("/api/v1/projects").json()["items"] if p["client_id"] is None)
    assert sync["name"] == "PragMattie Sync"


@pytest.mark.parametrize("path", ["/api/v1/clients", "/api/v1/projects"])
def test_api_lists_are_read_only_and_validate_paging(db, client, sign_in, path):
    sign_in(client, db, "auditor")
    assert client.get(path, params={"limit": 101}).status_code == 422
    assert client.get(path, params={"offset": -1}).status_code == 422
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)(path).status_code == 405
