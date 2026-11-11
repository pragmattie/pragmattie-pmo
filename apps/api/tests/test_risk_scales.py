"""The risk policy's labelled scoring scales (item 7.1)."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.api.risk_policy as risk_policy_api
from app.db import engine
from app.main import app
from app.models import Base
from app.policy import PolicyError

RISK_YAML = Path(__file__).resolve().parents[3] / "policies" / "risk.yaml"
URL = "/api/v1/risk-policy/scales"


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


def test_the_scales_come_from_the_risk_policy_file(web, db, sign_in):
    sign_in(web, db, "project_manager")
    response = web.get(URL)
    assert response.status_code == 200
    body = response.json()
    raw = yaml.safe_load(RISK_YAML.read_text())

    assert [p["level"] for p in body["probability"]] == [1, 2, 3, 4, 5]
    for item in body["probability"]:
        entry = raw["probability"][item["level"]]
        assert item == {
            "level": item["level"],
            "label": entry["label"],
            "definition": entry["definition"],
        }

    assert [i["level"] for i in body["impact"]] == [1, 2, 3, 4, 5]
    for item in body["impact"]:
        entry = raw["impact"][item["level"]]
        assert item["label"] == entry["label"]
        # An impact level's definition carries its schedule, cost and quality effects.
        for objective in ("schedule", "cost", "quality"):
            assert entry[objective] in item["definition"]


@pytest.mark.parametrize("role", ["auditor", "sponsor", "ethics_reviewer"])
def test_any_reader_of_projects_may_read_the_scales(web, db, sign_in, role):
    sign_in(web, db, role)
    assert web.get(URL).status_code == 200


def test_the_scales_need_a_signed_in_person(web, db):
    assert web.get(URL).status_code == 401


def test_a_policy_failure_is_a_503(web, db, sign_in, monkeypatch):
    sign_in(web, db, "project_manager")

    def broken():
        raise PolicyError("risk.yaml: not valid YAML")

    monkeypatch.setattr(risk_policy_api, "risk_policy", broken)
    response = web.get(URL)
    assert response.status_code == 503
    assert "yaml" not in response.json()["detail"]
