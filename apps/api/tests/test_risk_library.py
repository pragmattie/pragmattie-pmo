"""The starter risk library: strict loading, the list route and adding templates (item 3.2)."""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from conftest import risk_library_dir
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.api.risk_library as library_api
from app import risks
from app.db import engine
from app.main import app
from app.models import AuditDecision, Base, Project, Risk, User
from app.policy import RISK_LIBRARY_FILE, PolicyError, load_risk_library

LIBRARY = risk_library_dir() / RISK_LIBRARY_FILE

AI_RISKS = {
    "ai-biased-output",
    "ai-model-drift",
    "ai-made-up-answers",
    "ai-over-reliance",
    "ai-personal-data-exposure",
    "ai-runaway-cost",
    "ai-misuse",
    "ai-hidden-instructions",
}
PROJECT_RISKS = {
    "project-scope-creep",
    "project-key-person-loss",
    "project-supplier-delay",
    "project-wrong-estimate",
    "project-sponsor-change",
    "project-poor-data-quality",
    "project-slow-decisions",
}


def library_dir(tmp_path: Path, change: Callable | None = None) -> Path:
    """A copy of the library, with `change(data)` applied."""
    target = tmp_path / "policies"
    target.mkdir()
    shutil.copy(LIBRARY, target / RISK_LIBRARY_FILE)
    if change:
        path = target / RISK_LIBRARY_FILE
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        change(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def assert_rejected(tmp_path, change, match):
    with pytest.raises(PolicyError, match=match):
        load_risk_library(library_dir(tmp_path, change))


def _set(index: int, key: str, value):
    def change(data):
        data["templates"][index][key] = value

    return change


# --- Loading


def test_the_library_loads_with_the_agreed_risks():
    templates = load_risk_library(LIBRARY.parent).templates
    ai = {t.id for t in templates.values() if t.category == "ai"}
    project = {t.id for t in templates.values() if t.category == "project"}
    assert (ai, project) == (AI_RISKS, PROJECT_RISKS)
    for template in templates.values():
        assert template.title and template.cause and template.effect and template.response_plan
        assert template.objectives


def test_a_template_has_no_probability_or_impact():
    data = yaml.safe_load(LIBRARY.read_text(encoding="utf-8"))
    for entry in data["templates"]:
        assert not {"probability", "impact", "score", "band"} & set(entry), entry["id"]


def test_a_missing_library_names_the_file(tmp_path):
    with pytest.raises(PolicyError, match="risk-library.yaml: policy file not found"):
        load_risk_library(tmp_path)


def test_a_duplicate_id_fails(tmp_path):
    def change(data):
        data["templates"][1]["id"] = data["templates"][0]["id"]

    assert_rejected(tmp_path, change, "duplicate id 'ai-biased-output'")


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("kind", "hazard", "unknown kind 'hazard'"),
        ("category", "legal", "unknown category 'legal'"),
        ("objectives", ["schedule", "scope"], "unknown objective 'scope'"),
        ("response", "transfer", "unknown threat response 'transfer'"),
        # A threat can't take an opportunity's response.
        ("response", "pursue", "unknown threat response 'pursue'"),
    ],
)
def test_an_unknown_value_fails(tmp_path, key, value, match):
    assert_rejected(tmp_path, _set(0, key, value), match)


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("id", "Has Spaces", "lower-case words joined by hyphens"),
        ("id", "a" * 65, "at most 64"),
        ("title", "x" * 201, "at most 200"),
        ("cause", "  ", "expected non-empty text"),
        ("objectives", [], "non-empty list of objectives"),
        ("objectives", ["cost", "cost"], "must not repeat"),
        ("probability", 3, "unknown keys: probability"),
    ],
)
def test_a_bad_field_fails(tmp_path, key, value, match):
    assert_rejected(tmp_path, _set(0, key, value), match)


def test_a_missing_field_fails(tmp_path):
    assert_rejected(tmp_path, lambda data: data["templates"][0].pop("effect"), "missing keys")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda data: data.update(version=2), "version must be 1"),
        (lambda data: data.update(templates=[]), "non-empty list of templates"),
        (lambda data: data.update(extra=1), "unknown keys: extra"),
    ],
)
def test_a_bad_file_fails(tmp_path, change, match):
    assert_rejected(tmp_path, change, match)


# --- The routes


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
def project(db) -> Project:
    project = Project(name="Harbour", stage="delivery", sponsor="S", pm="P", status="on_track")
    db.add(project)
    db.commit()
    return project


def _add(web: TestClient, project_id: int, template_ids: list[str]):
    return web.post(
        f"/api/v1/projects/{project_id}/risks/from-library", json={"template_ids": template_ids}
    )


def test_the_list_is_paged_in_file_order(web, db, pm):
    ids = list(load_risk_library(LIBRARY.parent).templates)
    first = web.get("/api/v1/risk-library", params={"limit": 4}).json()
    second = web.get("/api/v1/risk-library", params={"limit": 4, "offset": 4}).json()
    assert first["total"] == second["total"] == len(ids) == 15
    assert [t["id"] for t in first["items"] + second["items"]] == ids[:8]
    assert set(first["items"][0]) == {
        "id",
        "kind",
        "category",
        "title",
        "cause",
        "effect",
        "objectives",
        "response",
        "response_plan",
    }


def test_a_library_failure_is_a_503(web, db, pm, project, monkeypatch):
    def broken():
        raise PolicyError("risk-library.yaml: not valid YAML")

    monkeypatch.setattr(risks, "risk_library", broken)
    response = web.get("/api/v1/risk-library")
    assert response.status_code == 503
    assert "yaml" not in response.json()["detail"]
    assert _add(web, project.id, ["ai-misuse"]).status_code == 503
    assert db.scalar(select(Risk.id)) is None
    assert library_api.LIBRARY_UNAVAILABLE == response.json()["detail"]


def test_adding_templates_creates_proposed_unscored_risks(web, db, pm, project):
    response = _add(web, project.id, ["ai-hidden-instructions", "project-slow-decisions"])
    assert response.status_code == 201, response.text
    added = response.json()
    templates = load_risk_library(LIBRARY.parent).templates
    assert [r["title"] for r in added] == [
        templates["ai-hidden-instructions"].title,
        templates["project-slow-decisions"].title,
    ]
    chosen = ["ai-hidden-instructions", "project-slow-decisions"]
    for risk, template_id in zip(added, chosen, strict=True):
        template = templates[template_id]
        assert risk["project_id"] == project.id
        assert (risk["status"], risk["source"], risk["version"]) == ("proposed", "person", 1)
        assert (risk["probability"], risk["impact"], risk["score"], risk["band"]) == (None,) * 4
        assert risk["owner_user_id"] is None
        assert (risk["kind"], risk["category"], risk["response"]) == (
            template.kind,
            template.category,
            template.response,
        )
        assert risk["objectives"] == list(template.objectives)
        assert (risk["cause"], risk["effect"], risk["response_plan"]) == (
            template.cause,
            template.effect,
            template.response_plan,
        )


def test_each_added_risk_has_one_audit_row(web, db, pm, project):
    added = _add(web, project.id, ["ai-runaway-cost", "project-supplier-delay"]).json()
    db.expire_all()
    rows = list(
        db.scalars(
            select(AuditDecision)
            .where(AuditDecision.subject_type == "risk")
            .order_by(AuditDecision.id)
        )
    )
    assert [row.subject_id for row in rows] == [str(risk["id"]) for risk in added]
    for row, template_id in zip(rows, ["ai-runaway-cost", "project-supplier-delay"], strict=True):
        assert row.agent == "risk_register"
        assert row.actor_user_id == pm.id
        assert row.signals is None
        assert row.action_taken == {"action": "create", "template_id": template_id}
        assert row.output["status"] == "proposed" and row.output["score"] is None


def test_an_added_risk_can_then_be_scored(web, db, pm, project):
    risk = _add(web, project.id, ["project-wrong-estimate"]).json()[0]
    response = web.patch(
        f"/api/v1/projects/{project.id}/risks/{risk['id']}",
        json={"version": 1, "probability": 3, "impact": 4},
    )
    assert response.status_code == 200, response.text
    assert (response.json()["score"], response.json()["band"]) == (12, "Medium")


@pytest.mark.parametrize(
    "body",
    [
        {"template_ids": []},
        {"template_ids": ["ai-misuse", "ai-misuse"]},
        {"template_ids": ["ai-misuse"], "probability": 3},
        {"template_ids": ["ai-misuse"] * 51},
        {},
    ],
)
def test_a_bad_request_adds_nothing(web, db, pm, project, body):
    response = web.post(f"/api/v1/projects/{project.id}/risks/from-library", json=body)
    assert response.status_code == 422
    assert db.scalar(select(Risk.id)) is None


def test_an_unknown_template_adds_nothing(web, db, pm, project):
    response = _add(web, project.id, ["ai-misuse", "no-such-risk"])
    assert response.status_code == 422
    assert "no-such-risk" in response.json()["detail"]
    assert db.scalar(select(Risk.id)) is None
    assert db.scalar(select(AuditDecision.id)) is None


def test_an_unknown_project_is_a_404(web, db, pm):
    assert _add(web, 9999, ["ai-misuse"]).status_code == 404


def test_reading_roles_cannot_add_templates(web, db, sign_in, project):
    sign_in(web, db, "auditor")
    assert web.get("/api/v1/risk-library").status_code == 200
    assert _add(web, project.id, ["ai-misuse"]).status_code == 403
    assert db.scalar(select(Risk.id)) is None
