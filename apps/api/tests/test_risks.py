"""The risk register's model and API (items 3.1, 3.4 and 3.6)."""

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.api.risks as risks_api
from app import risks
from app.authz import may_accept_band
from app.db import engine
from app.main import app
from app.models import AuditDecision, Base, Project, Risk, User
from app.policy import PolicyError, load_policies, score_risk

REPO_ROOT = Path(__file__).resolve().parents[3]
RISK_YAML = REPO_ROOT / "policies" / "risk.yaml"

BODY = {
    "kind": "threat",
    "title": "Supplier misses the data handover",
    "cause": "The supplier has one engineer on the export",
    "effect": "Testing starts late",
    "category": "project",
    "objectives": ["schedule", "cost"],
    "probability": 3,
    "impact": 4,
    "response": "reduce",
    "response_plan": "Agree a dated handover plan with a named backup",
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
def web(db) -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def pm(web, db, sign_in) -> User:
    return sign_in(web, db, "project_manager")


def _project(db: Session, name: str = "Harbour Analytics") -> Project:
    project = Project(name=name, stage="delivery", sponsor="S", pm="P", status="on_track")
    db.add(project)
    db.commit()
    return project


@pytest.fixture
def project(db) -> Project:
    return _project(db)


def _url(project_id: int, risk_id: int | None = None) -> str:
    return f"/api/v1/projects/{project_id}/risks" + (f"/{risk_id}" if risk_id else "")


def _create(web: TestClient, project_id: int, **changes):
    return web.post(_url(project_id), json={**BODY, **changes})


def _audit(db: Session, *, escalations: bool = False) -> list[AuditDecision]:
    """The risk audit rows, oldest first; escalation rows only when asked for."""
    db.expire_all()
    stmt = select(AuditDecision).where(AuditDecision.subject_type == "risk")
    rows = list(db.scalars(stmt.order_by(AuditDecision.id)))
    if escalations:
        return rows
    return [row for row in rows if row.action_taken.get("action") != "escalate"]


# --- Scoring follows the policy file


def _expected_from_yaml(probability: int, impact: int) -> tuple[int, str]:
    """Worked straight from the YAML, independently of the loader."""
    data = yaml.safe_load(RISK_YAML.read_text(encoding="utf-8"))
    score = probability * impact
    names = [band["name"] for band in data["priority_bands"]]
    band = next(
        b["name"] for b in data["priority_bands"] if b["min_score"] <= score <= b["max_score"]
    )
    floor = data["minimum_band_by_impact"].get(impact)
    if floor is not None and names.index(floor) > names.index(band):
        band = floor
    return score, band


def test_the_impact_5_floor_raises_a_low_score():
    policy = load_policies(REPO_ROOT / "policies").risk
    assert score_risk(policy, 1, 5) == (5, "Medium")
    assert score_risk(policy, 5, 1) == (5, "Low")
    assert score_risk(policy, None, 3) == (None, None)


@pytest.mark.parametrize("impact", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("probability", [1, 2, 3, 4, 5])
def test_every_score_and_band_match_the_policy(web, db, pm, project, probability, impact):
    response = _create(web, project.id, probability=probability, impact=impact)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["score"], body["band"]) == _expected_from_yaml(probability, impact)
    assert web.get(_url(project.id, body["id"])).json()["band"] == body["band"]


@pytest.mark.parametrize("field", ["score", "band", "source", "status", "version", "id"])
def test_the_client_cannot_set_server_fields_on_create(web, db, pm, project, field):
    response = _create(web, project.id, **{field: "High" if field == "band" else 25})
    assert response.status_code == 422
    assert db.scalar(select(Risk.id)) is None


@pytest.mark.parametrize("field", ["score", "band", "source"])
def test_the_client_cannot_set_server_fields_on_update(web, db, pm, project, field):
    risk = _create(web, project.id).json()
    response = web.patch(_url(project.id, risk["id"]), json={"version": 1, field: 1})
    assert response.status_code == 422


def test_a_new_risk_is_open_from_a_person_at_version_1(web, db, pm, project):
    body = _create(web, project.id).json()
    assert (body["status"], body["source"], body["version"]) == ("open", "person", 1)
    assert body["owner_user_id"] is None
    assert body["needs_judgement"] is False


def test_a_policy_failure_refuses_the_write(web, db, pm, project, monkeypatch):
    def broken():
        raise PolicyError("risk.yaml: not valid YAML")

    monkeypatch.setattr(risks_api, "risk_policy", broken)
    response = _create(web, project.id)
    assert response.status_code == 503
    assert "risk policy" in response.json()["detail"]
    assert "yaml" not in response.json()["detail"]
    assert db.scalar(select(Risk.id)) is None
    assert _audit(db) == []


def test_a_policy_failure_refuses_an_update(web, db, pm, project, monkeypatch):
    risk = _create(web, project.id).json()
    monkeypatch.setattr(risks_api, "risk_policy", lambda: (_ for _ in ()).throw(PolicyError("x")))
    response = web.patch(_url(project.id, risk["id"]), json={"version": 1, "impact": 5})
    assert response.status_code == 503
    assert len(_audit(db)) == 1


# --- Risks that need a person's judgement


def test_a_needs_judgement_risk_waits_for_a_score(web, db, pm, project):
    created = _create(
        web, project.id, needs_judgement=True, probability=None, impact=None, category="ai"
    )
    assert created.status_code == 201, created.text
    risk = created.json()
    assert (risk["score"], risk["band"]) == (None, None)

    response = web.patch(
        _url(project.id, risk["id"]), json={"version": 1, "probability": 2, "impact": 5}
    )
    assert response.status_code == 200, response.text
    assert (response.json()["score"], response.json()["band"]) == (10, "Medium")


def test_only_a_needs_judgement_risk_may_go_unscored(web, db, pm, project):
    assert _create(web, project.id, probability=None, impact=None).status_code == 422


def test_probability_and_impact_go_together(web, db, pm, project):
    response = _create(web, project.id, needs_judgement=True, impact=None)
    assert response.status_code == 422


@pytest.mark.parametrize("value", [0, 6, "3", 2.5, True])
def test_levels_are_whole_numbers_1_to_5(web, db, pm, project, value):
    assert _create(web, project.id, probability=value).status_code == 422


# --- Field rules


@pytest.mark.parametrize(
    ("kind", "response", "ok"),
    [
        ("threat", "avoid", True),
        ("threat", "share", True),
        ("threat", "pursue", False),
        ("threat", "increase", False),
        ("opportunity", "pursue", True),
        ("opportunity", "accept", True),
        ("opportunity", "avoid", False),
        ("opportunity", "reduce", False),
    ],
)
def test_the_response_must_fit_the_kind(web, db, pm, project, kind, response, ok):
    assert (_create(web, project.id, kind=kind, response=response).status_code == 201) is ok


def test_changing_the_kind_alone_rechecks_the_response(web, db, pm, project):
    risk = _create(web, project.id).json()
    response = web.patch(_url(project.id, risk["id"]), json={"version": 1, "kind": "opportunity"})
    assert response.status_code == 422
    assert len(_audit(db)) == 1


@pytest.mark.parametrize(
    "objectives", [[], ["schedule", "schedule"], ["scope"], ["cost", "quality", "time"]]
)
def test_objectives_are_one_or_more_distinct_known_ones(web, db, pm, project, objectives):
    assert _create(web, project.id, objectives=objectives).status_code == 422


def test_the_category_is_ai_or_project(web, db, pm, project):
    assert _create(web, project.id, category="ai").status_code == 201
    assert _create(web, project.id, category="legal").status_code == 422


def test_the_owner_must_be_an_existing_active_user(web, db, pm, project):
    assert _create(web, project.id, owner_user_id=pm.id).json()["owner_user_id"] == pm.id
    assert _create(web, project.id, owner_user_id=9999).status_code == 422
    gone = User(email="gone@pmo.example", display_name="Gone", password_hash="x", role="sponsor")
    gone.is_active = False
    db.add(gone)
    db.commit()
    assert _create(web, project.id, owner_user_id=gone.id).status_code == 422
    risk = _create(web, project.id).json()
    response = web.patch(
        _url(project.id, risk["id"]), json={"version": 1, "owner_user_id": gone.id}
    )
    assert response.status_code == 422


def test_the_response_names_the_owner_even_once_deactivated(web, db, pm, project):
    owner = User(
        email="former@pmo.example",
        display_name="Former Lead",
        password_hash="x",
        role="project_manager",
    )
    db.add(owner)
    db.commit()
    risk = _create(web, project.id, owner_user_id=owner.id).json()
    assert risk["owner_name"] == "Former Lead"
    assert _create(web, project.id).json()["owner_name"] is None
    owner.is_active = False
    db.commit()
    assert web.get(_url(project.id, risk["id"])).json()["owner_name"] == "Former Lead"
    listed = web.get(_url(project.id)).json()["items"]
    assert {r["id"]: r["owner_name"] for r in listed}[risk["id"]] == "Former Lead"


@pytest.mark.parametrize("value", ["accepted", "proposed", "rejected", None])
def test_a_patch_may_only_open_or_close(web, db, pm, project, value):
    risk = _create(web, project.id).json()
    response = web.patch(_url(project.id, risk["id"]), json={"version": 1, "status": value})
    assert response.status_code == 422
    assert len(_audit(db)) == 1


def test_a_patch_without_a_version_is_422(web, db, pm, project):
    risk = _create(web, project.id).json()
    assert web.patch(_url(project.id, risk["id"]), json={"title": "New"}).status_code == 422


# --- Versions and audit


def test_a_stale_version_is_409(web, db, pm, project):
    risk = _create(web, project.id).json()
    url = _url(project.id, risk["id"])
    assert web.patch(url, json={"version": 1, "title": "First"}).json()["version"] == 2
    response = web.patch(url, json={"version": 1, "title": "Second"})
    assert response.status_code == 409
    assert web.get(url).json()["title"] == "First"
    assert len(_audit(db)) == 2


def test_a_write_that_loses_a_race_is_409(web, db, pm, project, monkeypatch):
    # Another request changes the risk after this one has read it: the version in the UPDATE no
    # longer matches, so SQLAlchemy raises StaleDataError, which becomes a 409.
    risk_id = _create(web, project.id).json()["id"]
    real = risks_api.apply_score

    def changed_meanwhile(risk, policy):
        with Session(engine) as other:
            other.get(Risk, risk_id).title = "Changed meanwhile"
            other.commit()
        return real(risk, policy)

    monkeypatch.setattr(risks_api, "apply_score", changed_meanwhile)
    response = web.patch(_url(project.id, risk_id), json={"version": 1, "impact": 5})
    assert response.status_code == 409
    db.expire_all()
    stored = db.get(Risk, risk_id)
    assert (stored.title, stored.impact, stored.version) == ("Changed meanwhile", 4, 2)
    assert len(_audit(db)) == 1


def test_create_change_and_close_are_one_audit_row_each(web, db, pm, project):
    # Low throughout, so the project manager owns its escalation and may close it.
    risk = _create(web, project.id, probability=1, impact=3).json()
    url = _url(project.id, risk["id"])
    web.patch(url, json={"version": 1, "impact": 4})
    web.patch(url, json={"version": 2, "status": "closed"})

    created, changed, closed = _audit(db)
    for row, version in ((created, 1), (changed, 2), (closed, 3)):
        assert (row.agent, row.trigger, row.subject_id, row.status) == (
            "risk_register",
            "human",
            str(risk["id"]),
            "ok",
        )
        assert row.actor_user_id == pm.id
        assert row.version == version == row.output["version"]
    assert [r.action_taken["action"] for r in (created, changed, closed)] == [
        "create",
        "update",
        "close",
    ]
    assert created.signals is None
    assert created.output["score"] == 3
    assert (changed.signals["impact"], changed.signals["score"], changed.signals["band"]) == (
        3,
        3,
        "Low",
    )
    assert (changed.output["impact"], changed.output["score"], changed.output["band"]) == (
        4,
        4,
        "Low",
    )
    assert (closed.signals["status"], closed.output["status"]) == ("open", "closed")


def test_a_closed_risk_can_be_reopened(web, db, pm, project):
    risk = _create(web, project.id, **LOW).json()
    url = _url(project.id, risk["id"])
    web.patch(url, json={"version": 1, "status": "closed"})
    response = web.patch(url, json={"version": 2, "status": "open"})
    assert response.json()["status"] == "open"
    assert _audit(db)[-1].action_taken == {"action": "update"}


def test_a_patch_that_changes_nothing_writes_no_row(web, db, pm, project):
    risk = _create(web, project.id).json()
    response = web.patch(_url(project.id, risk["id"]), json={"version": 1, "title": BODY["title"]})
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert len(_audit(db)) == 1


def test_there_is_no_delete(web, db, pm, project):
    risk = _create(web, project.id).json()
    assert web.delete(_url(project.id, risk["id"])).status_code == 405


# --- Projects and lookups


def test_a_risk_under_another_projects_id_is_404(web, db, pm, project):
    other = _project(db, "Other")
    risk = _create(web, project.id).json()
    assert web.get(_url(other.id, risk["id"])).status_code == 404
    patch = web.patch(_url(other.id, risk["id"]), json={"version": 1, "title": "X"})
    assert patch.status_code == 404
    assert len(_audit(db)) == 1


def test_an_unknown_project_is_404(web, db, pm):
    assert web.get(_url(9999)).status_code == 404
    assert _create(web, 9999).status_code == 404


# --- The list


def _seed_list(web, project_id: int) -> dict[str, int]:
    made = {
        "low": _create(web, project_id, probability=1, impact=2),
        "high": _create(web, project_id, probability=5, impact=4, category="ai"),
        "medium": _create(web, project_id, probability=2, impact=4),
        "medium_too": _create(
            web, project_id, probability=4, impact=2, kind="opportunity", response="pursue"
        ),
        "unscored": _create(web, project_id, needs_judgement=True, probability=None, impact=None),
    }
    return {name: response.json()["id"] for name, response in made.items()}


def _ids(response) -> list[int]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def test_the_list_sorts_by_score_with_unscored_last_and_ids_breaking_ties(web, db, pm, project):
    ids = _seed_list(web, project.id)
    expected = [ids["high"], ids["medium"], ids["medium_too"], ids["low"], ids["unscored"]]
    assert _ids(web.get(_url(project.id))) == expected
    ascending = [ids["low"], ids["medium"], ids["medium_too"], ids["high"], ids["unscored"]]
    assert _ids(web.get(_url(project.id), params={"sort": "score"})) == ascending
    assert web.get(_url(project.id), params={"sort": "title"}).status_code == 422


def test_the_list_pages_stably(web, db, pm, project):
    ids = _seed_list(web, project.id)
    pages = [
        web.get(_url(project.id), params={"limit": 2, "offset": offset}).json()
        for offset in (0, 2, 4)
    ]
    assert [p["total"] for p in pages] == [5, 5, 5]
    assert sum(([i["id"] for i in p["items"]] for p in pages), []) == _ids(
        web.get(_url(project.id))
    )
    assert len(ids) == 5


def test_the_list_filters(web, db, pm, project):
    ids = _seed_list(web, project.id)
    web.patch(_url(project.id, ids["low"]), json={"version": 1, "status": "closed"})

    def listed(**params):
        return set(_ids(web.get(_url(project.id), params=params)))

    assert listed(status="closed") == {ids["low"]}
    assert listed(band="Medium") == {ids["medium"], ids["medium_too"]}
    assert listed(category="ai") == {ids["high"]}
    assert listed(kind="opportunity") == {ids["medium_too"]}
    assert listed(band="Medium", kind="threat") == {ids["medium"]}
    assert web.get(_url(project.id), params={"status": "deleted"}).status_code == 422
    assert web.get(_url(project.id), params={"kind": "issue"}).status_code == 422
    assert listed(probability=2, impact=4) == {ids["medium"]}
    assert listed(probability=4) == {ids["medium_too"]}
    assert web.get(_url(project.id), params={"impact": 6}).status_code == 422


def test_the_list_holds_only_this_projects_risks(web, db, pm, project):
    other = _project(db, "Other")
    _create(web, other.id)
    mine = _create(web, project.id).json()["id"]
    assert _ids(web.get(_url(project.id))) == [mine]


def test_a_reader_cannot_create_or_change(web, db, sign_in, project):
    pm_client = TestClient(app, base_url="http://localhost")
    sign_in(pm_client, db, "project_manager", "writer@pmo.example")
    risk = _create(pm_client, project.id).json()
    sign_in(web, db, "auditor")
    assert web.get(_url(project.id, risk["id"])).status_code == 200
    assert _create(web, project.id).status_code == 403
    assert web.patch(_url(project.id, risk["id"]), json={"version": 1}).status_code == 403


# --- The heat map and history (item 3.3)


def _heatmap(web, project_id: int, **params) -> dict:
    response = web.get(_url(project_id) + "/heatmap", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _cell(heatmap: dict, probability: int, impact: int) -> dict:
    return next(
        c for c in heatmap["cells"] if (c["probability"], c["impact"]) == (probability, impact)
    )


def test_the_heatmap_counts_open_risks_by_cell_and_band(web, db, pm, project):
    ids = _seed_list(web, project.id)
    _create(web, project.id, probability=2, impact=4)
    web.patch(_url(project.id, ids["low"]), json={"version": 1, "status": "closed"})
    _create(web, _project(db, "Elsewhere").id, probability=2, impact=4)

    heatmap = _heatmap(web, project.id)
    assert len(heatmap["cells"]) == 25
    assert _cell(heatmap, 2, 4)["count"] == 2
    assert _cell(heatmap, 5, 4)["count"] == 1
    assert _cell(heatmap, 1, 2)["count"] == 0  # closed
    assert heatmap["bands"] == [
        {"band": "Low", "count": 0, "owner_role": "project_manager"},
        {"band": "Medium", "count": 3, "owner_role": "sponsor"},
        {"band": "High", "count": 1, "owner_role": "sponsor"},
    ]
    assert heatmap["unscored"] == 1
    assert sum(c["count"] for c in heatmap["cells"]) == 4


def test_each_heatmap_cell_has_its_policy_band(web, db, pm, project):
    heatmap = _heatmap(web, project.id)
    for cell in heatmap["cells"]:
        assert cell["band"] == _expected_from_yaml(cell["probability"], cell["impact"])[1]


def test_the_heatmap_shows_threats_and_opportunities_separately(web, db, pm, project):
    _seed_list(web, project.id)
    threats = _heatmap(web, project.id, kind="threat")
    opportunities = _heatmap(web, project.id, kind="opportunity")
    assert (threats["kind"], opportunities["kind"]) == ("threat", "opportunity")
    assert _cell(opportunities, 4, 2)["count"] == 1
    assert sum(c["count"] for c in opportunities["cells"]) == 1
    assert _cell(threats, 4, 2)["count"] == 0
    assert web.get(_url(project.id) + "/heatmap", params={"kind": "issue"}).status_code == 422


def test_the_heatmap_of_an_unknown_project_is_404(web, db, pm):
    assert web.get(_url(9999) + "/heatmap").status_code == 404


def test_a_policy_failure_refuses_the_heatmap(web, db, pm, project, monkeypatch):
    def broken():
        raise PolicyError("broken")

    monkeypatch.setattr(risks_api, "risk_policy", broken)
    assert web.get(_url(project.id) + "/heatmap").status_code == 503


def test_history_lists_the_risks_audit_rows_newest_first_with_names(web, db, pm, project):
    risk = _create(web, project.id, **LOW).json()
    _create(web, project.id)  # another risk's rows stay out
    web.patch(_url(project.id, risk["id"]), json={"version": 1, "title": "Renamed"})
    web.patch(_url(project.id, risk["id"]), json={"version": 2, "status": "closed"})

    sign_in_reader = TestClient(app, base_url="http://localhost")
    response = sign_in_reader.get(_url(project.id, risk["id"]) + "/history")
    assert response.status_code == 401

    response = web.get(_url(project.id, risk["id"]) + "/history")
    assert response.status_code == 200, response.text
    page = response.json()
    # A new Low risk is escalated to the project manager as it is created.
    assert page["total"] == 4
    assert [e["action"] for e in page["items"]] == ["close", "update", "escalate", "create"]
    assert [e["version"] for e in page["items"]] == [3, 2, 1, 1]
    assert {e["actor_name"] for e in page["items"]} == {pm.display_name}
    create, update = page["items"][3], page["items"][1]
    assert create["before"] is None and create["after"]["title"] == BODY["title"]
    assert (update["before"]["title"], update["after"]["title"]) == (BODY["title"], "Renamed")


def test_history_pages_and_needs_the_risk_in_the_project(web, db, pm, project):
    risk = _create(web, project.id).json()
    web.patch(_url(project.id, risk["id"]), json={"version": 1, "title": "Renamed"})
    first = web.get(_url(project.id, risk["id"]) + "/history", params={"limit": 1}).json()
    second = web.get(
        _url(project.id, risk["id"]) + "/history", params={"limit": 1, "offset": 1}
    ).json()
    assert [first["items"][0]["action"], second["items"][0]["action"]] == ["update", "escalate"]
    other = _project(db, "Other")
    assert web.get(_url(other.id, risk["id"]) + "/history").status_code == 404


def test_every_reader_sees_history_without_audit_read(web, db, sign_in, project):
    pm_client = TestClient(app, base_url="http://localhost")
    sign_in(pm_client, db, "project_manager", "writer@pmo.example")
    risk = _create(pm_client, project.id).json()
    sign_in(web, db, "sponsor")
    assert web.get("/api/v1/audit").status_code == 403
    # The create and its escalation.
    assert web.get(_url(project.id, risk["id"]) + "/history").json()["total"] == 2


def test_people_lists_active_users_by_name_without_emails(web, db, sign_in):
    sign_in(web, db, "auditor", "zed@pmo.example")
    other = TestClient(app, base_url="http://localhost")
    gone = sign_in(other, db, "sponsor", "gone@pmo.example")
    gone.is_active = False
    db.commit()
    response = web.get("/api/v1/users")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert gone.id not in {p["id"] for p in items}
    assert all(set(p) == {"id", "display_name"} for p in items)


# --- Re-scoring after a policy change


def test_rescore_updates_changed_risks_with_one_audit_row_each(web, db, pm, project, tmp_path):
    first = _create(web, project.id, probability=2, impact=3).json()  # 6, Low
    second = _create(web, project.id, probability=5, impact=5).json()  # 25, High
    _create(web, project.id, needs_judgement=True, probability=None, impact=None)

    # A stricter policy: Low now stops at 5.
    for name in ("approvers.yaml", "tiers.yaml", "gates.yaml", "risk.yaml"):
        (tmp_path / name).write_text((REPO_ROOT / "policies" / name).read_text())
    data = yaml.safe_load(RISK_YAML.read_text())
    data["priority_bands"][0]["max_score"] = 5
    data["priority_bands"][1]["min_score"] = 6
    (tmp_path / "risk.yaml").write_text(yaml.safe_dump(data))
    policy = load_policies(tmp_path).risk

    with Session(engine) as session:
        assert risks.rescore(session, policy) == 1
    db.expire_all()
    assert db.get(Risk, first["id"]).band == "Medium"
    assert db.get(Risk, first["id"]).version == 2
    assert db.get(Risk, second["id"]).version == 1
    row = _audit(db)[-1]
    assert row.action_taken == {"action": "rescore"}
    assert row.actor_user_id is None
    assert (row.signals["band"], row.output["band"]) == ("Low", "Medium")
    assert len(_audit(db)) == 4

    with Session(engine) as session:
        assert risks.rescore(session, policy) == 0


def test_the_rescore_command_reports_a_policy_failure(monkeypatch, capsys):
    def broken():
        raise PolicyError("risk.yaml: missing")

    monkeypatch.setattr(risks, "risk_policy", broken)
    assert risks.main(["rescore"]) == 1
    assert "could not be loaded" in capsys.readouterr().err


# --- Escalation and acceptance (item 3.4)

LOW = {"probability": 1, "impact": 2}  # score 2
MEDIUM = {"probability": 3, "impact": 3}  # score 9
HIGH = {"probability": 4, "impact": 4}  # score 16
REASON = "Small, well understood and cheaper to watch than to treat"


def _client(db: Session, sign_in, role: str, email: str) -> TestClient:
    client = TestClient(app, base_url="http://localhost")
    sign_in(client, db, role, email)
    return client


@pytest.fixture
def sponsor(db, sign_in) -> TestClient:
    return _client(db, sign_in, "sponsor", "sponsor.one@pmo.example")


def _accept(client: TestClient, project_id: int, risk: dict, **changes):
    body = {
        "version": risk["version"],
        "reason": REASON,
        "review_date": (date.today() + timedelta(days=30)).isoformat(),
        **changes,
    }
    return client.post(_url(project_id, risk["id"]) + "/accept", json=body)


def _proposed(web: TestClient, project_id: int) -> dict:
    response = web.post(
        _url(project_id) + "/from-library", json={"template_ids": ["ai-model-drift"]}
    )
    assert response.status_code == 201, response.text
    return response.json()[0]


def _actions(db: Session) -> list[str]:
    return [row.action_taken["action"] for row in _audit(db, escalations=True)]


def test_the_band_owner_rank_decides_who_may_accept():
    policy = load_policies(REPO_ROOT / "policies").risk
    assert may_accept_band("project_manager", "Low", policy)
    assert not may_accept_band("project_manager", "Medium", policy)
    assert not may_accept_band("project_manager", "High", policy)
    assert all(may_accept_band("sponsor", band, policy) for band in ("Low", "Medium", "High"))
    for role in ("admin", "auditor", "ethics_reviewer", "nobody"):
        assert not may_accept_band(role, "Low", policy)
    assert not may_accept_band("sponsor", None, policy)
    assert not may_accept_band("sponsor", "Unknown", policy)


def test_a_pm_accepting_a_medium_risk_is_403_and_audited(web, db, pm, project, sponsor):
    risk = _create(web, project.id, **MEDIUM).json()
    response = _accept(web, project.id, risk)
    assert response.status_code == 403
    assert "sponsor" in response.json()["detail"]
    refused = _audit(db)[-1]
    assert (refused.status, refused.actor_user_id, refused.trigger) == ("rejected", pm.id, "human")
    assert refused.action_taken == {
        "action": "accept",
        "band": "Medium",
        "owner_role": "sponsor",
        "actor_role": "project_manager",
    }
    assert web.get(_url(project.id, risk["id"])).json()["status"] == "open"

    response = _accept(sponsor, project.id, risk)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["response"]) == ("accepted", "accept")
    assert body["review_date"] == (date.today() + timedelta(days=30)).isoformat()
    assert (body["escalated_to"], body["escalated_band"]) == (None, None)
    accepted = _audit(db)[-1]
    assert (accepted.status, accepted.version) == ("ok", 2)
    assert accepted.action_taken["action"] == "accept"
    assert accepted.action_taken["reason"] == REASON
    assert accepted.actor_user_id != pm.id
    assert (accepted.signals["status"], accepted.output["status"]) == ("open", "accepted")


def test_a_refused_acceptance_twice_is_two_rows(web, db, pm, project):
    risk = _create(web, project.id, **HIGH).json()
    assert _accept(web, project.id, risk).status_code == 403
    assert _accept(web, project.id, risk).status_code == 403
    refused = [row for row in _audit(db) if row.status == "rejected"]
    assert [row.attempt for row in refused] == [3, 4]  # after the create and its escalation


def test_a_pm_may_accept_a_low_risk(web, db, pm, project):
    risk = _create(web, project.id, **LOW).json()
    response = _accept(web, project.id, risk)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"


@pytest.mark.parametrize("role", ["admin", "auditor", "ethics_reviewer"])
def test_roles_without_risk_accept_are_403(web, db, project, sign_in, role):
    writer = _client(db, sign_in, "project_manager", "writer@pmo.example")
    risk = _create(writer, project.id, **LOW).json()
    sign_in(web, db, role)
    assert _accept(web, project.id, risk).status_code == 403


@pytest.mark.parametrize(
    "changes",
    [
        {"reason": "Too short"},
        {"reason": "    padded but short    "},
        {"review_date": (date.today() - timedelta(days=1)).isoformat()},
        {"review_date": "soon"},
        {"extra": 1},
        {"reason": None},
        {"review_date": None},
    ],
)
def test_accepting_needs_a_reason_and_a_review_date(web, db, pm, project, changes):
    risk = _create(web, project.id, **LOW).json()
    payload = {"version": 1, "reason": REASON, "review_date": date.today().isoformat(), **changes}
    payload = {key: value for key, value in payload.items() if value is not None}
    response = web.post(_url(project.id, risk["id"]) + "/accept", json=payload)
    assert response.status_code == 422
    assert web.get(_url(project.id, risk["id"])).json()["status"] == "open"


def test_a_review_date_of_today_is_allowed(web, db, pm, project):
    risk = _create(web, project.id, **LOW).json()
    response = _accept(web, project.id, risk, review_date=date.today().isoformat())
    assert response.status_code == 200, response.text


def test_a_needs_judgement_risk_is_accepted_only_once_scored(web, db, pm, project, sponsor):
    risk = _create(web, project.id, needs_judgement=True, probability=None, impact=None).json()
    assert risk["escalated_to"] is None
    response = _accept(sponsor, project.id, risk)
    assert response.status_code == 409
    assert "score" in response.json()["detail"]

    scored = web.patch(_url(project.id, risk["id"]), json={"version": 1, **MEDIUM}).json()
    assert scored["escalated_to"] == "sponsor"
    assert _accept(sponsor, project.id, scored).status_code == 200


def test_only_an_open_risk_can_be_accepted(web, db, pm, project, sponsor):
    proposed = _proposed(web, project.id)
    assert _accept(sponsor, project.id, proposed).status_code == 409
    risk = _create(web, project.id, **LOW).json()
    closed = web.patch(_url(project.id, risk["id"]), json={"version": 1, "status": "closed"})
    assert _accept(sponsor, project.id, closed.json()).status_code == 409
    assert _accept(sponsor, project.id, {**risk, "version": 9}).status_code == 409


def test_rescoring_into_a_higher_band_reopens_and_escalates(web, db, pm, project):
    risk = _create(web, project.id, **LOW).json()
    accepted = _accept(web, project.id, risk).json()
    assert accepted["status"] == "accepted"

    response = web.patch(_url(project.id, risk["id"]), json={"version": 2, **MEDIUM})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["band"]) == ("open", "Medium")
    assert (body["escalated_to"], body["escalated_band"]) == ("sponsor", "Medium")
    assert body["review_date"] is None
    rows = _audit(db, escalations=True)
    update, escalation = rows[-2:]
    assert update.action_taken == {"action": "update", "reopened": True}
    assert (update.signals["status"], update.output["status"]) == ("accepted", "open")
    assert escalation.action_taken == {
        "action": "escalate",
        "risk_id": risk["id"],
        "band": "Medium",
        "owner_role": "sponsor",
    }
    assert (escalation.version, escalation.actor_user_id) == (3, pm.id)


def test_rescoring_within_or_below_the_band_keeps_it_accepted(web, db, pm, project, sponsor):
    risk = _create(web, project.id, **HIGH).json()
    _accept(sponsor, project.id, risk)
    same = web.patch(_url(project.id, risk["id"]), json={"version": 2, "probability": 5}).json()
    assert (same["status"], same["band"], same["escalated_to"]) == ("accepted", "High", None)
    lower = web.patch(_url(project.id, risk["id"]), json={"version": 3, **LOW}).json()
    assert (lower["status"], lower["escalated_to"]) == ("accepted", None)


def test_a_new_risk_escalates_to_its_bands_owner(web, db, pm, project):
    low = _create(web, project.id, **LOW).json()
    medium = _create(web, project.id, **MEDIUM).json()
    assert (low["escalated_to"], low["escalated_band"]) == ("project_manager", "Low")
    assert (medium["escalated_to"], medium["escalated_band"]) == ("sponsor", "Medium")
    assert _actions(db) == ["create", "escalate", "create", "escalate"]


def test_an_escalation_follows_the_owner_not_every_edit(web, db, pm, project, sponsor):
    risk = _create(web, project.id, **LOW).json()
    url = _url(project.id, risk["id"])
    # Low to Medium: a new owner, so a new escalation.
    body = web.patch(url, json={"version": 1, **MEDIUM}).json()
    assert (body["escalated_to"], body["escalated_band"]) == ("sponsor", "Medium")
    assert _actions(db)[-2:] == ["update", "escalate"]
    # Medium to High: the same owner, so no new row, but the band it shows moves.
    body = web.patch(url, json={"version": 2, **HIGH}).json()
    assert (body["escalated_to"], body["escalated_band"]) == ("sponsor", "High")
    assert _actions(db)[-1] == "update"
    # A text edit keeps the escalation.
    body = web.patch(url, json={"version": 3, "title": "Renamed"}).json()
    assert body["escalated_to"] == "sponsor"
    # Back to Low is held for the sponsor: refused, nothing changes, one refusal row.
    response = web.patch(url, json={"version": 4, **LOW})
    assert response.status_code == 403
    body = web.get(url).json()
    assert (body["band"], body["escalated_to"], body["version"]) == ("High", "sponsor", 4)
    assert _audit(db)[-1].status == "rejected"
    # The sponsor accepts, which ends the escalation; then a re-score and close are the PM's.
    accepted = _accept(sponsor, project.id, body).json()
    assert (accepted["status"], accepted["escalated_to"]) == ("accepted", None)
    body = web.patch(url, json={"version": 5, **LOW}).json()
    assert (body["status"], body["band"], body["escalated_to"]) == ("accepted", "Low", None)
    body = web.patch(url, json={"version": 6, "status": "closed"}).json()
    assert body["status"] == "closed"
    # Reopening escalates afresh, to the owner of its band now.
    body = web.patch(url, json={"version": 7, "status": "open"}).json()
    assert (body["escalated_to"], body["escalated_band"]) == ("project_manager", "Low")


def test_losing_the_score_of_an_escalated_risk_is_held(web, db, pm, project):
    risk = _create(web, project.id, **MEDIUM).json()
    url = _url(project.id, risk["id"])
    rows = len(_audit(db, escalations=True))
    unscore = {"needs_judgement": True, "probability": None, "impact": None}
    response = web.patch(url, json={"version": 1, **unscore})
    assert response.status_code == 403
    body = web.get(url).json()
    assert (body["band"], body["escalated_to"], body["version"]) == ("Medium", "sponsor", 1)
    assert body["needs_judgement"] is False
    refused = _audit(db, escalations=True)[rows:]
    assert len(refused) == 1
    assert refused[0].status == "rejected"
    assert refused[0].action_taken["action"] == "rescore"


def test_losing_the_score_clears_the_escalation(web, db, pm, project):
    # The project manager owns a Low risk's escalation, so may take its score away.
    risk = _create(web, project.id, **LOW).json()
    body = web.patch(
        _url(project.id, risk["id"]),
        json={"version": 1, "needs_judgement": True, "probability": None, "impact": None},
    ).json()
    assert (body["band"], body["escalated_to"]) == (None, None)


@pytest.mark.parametrize("target", ["open", "closed"])
def test_a_patch_never_moves_a_proposed_risk(web, db, pm, project, target):
    risk = _proposed(web, project.id)
    response = web.patch(_url(project.id, risk["id"]), json={"version": 1, "status": target})
    assert response.status_code == 409
    assert web.get(_url(project.id, risk["id"])).json()["status"] == "proposed"


def test_a_patch_may_still_edit_and_score_a_proposed_risk(web, db, pm, project):
    risk = _proposed(web, project.id)
    response = web.patch(_url(project.id, risk["id"]), json={"version": 1, **MEDIUM})
    assert response.status_code == 200, response.text
    body = response.json()
    # Not open yet, so not escalated.
    assert (body["status"], body["band"], body["escalated_to"]) == ("proposed", "Medium", None)


def _with_suggestion(db: Session, risk_id: int) -> int:
    """The risk agent's suggestion, as `risk_agent.score` saves it; the risk's new version."""
    row = db.get(Risk, risk_id)
    row.suggested_probability, row.suggested_impact = 1, 2
    row.suggested_probability_reason = "It is unlikely."
    row.suggested_impact_reason = "It would be small."
    row.suggested_model = "claude-test"
    row.suggested_audit_id = 77
    db.commit()
    return row.version


@pytest.mark.parametrize("field", ["title", "cause", "effect"])
def test_a_text_edit_clears_the_agents_suggestion_and_says_so(web, db, pm, project, field):
    risk = _proposed(web, project.id)
    version = _with_suggestion(db, risk["id"])
    response = web.patch(
        _url(project.id, risk["id"]), json={"version": version, field: "New words", **MEDIUM}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert all(body[f"suggested_{n}"] is None for n in ("probability", "impact", "audit_id"))
    assert body["suggested_probability_reason"] is None and body["suggested_model"] is None
    row = _audit(db)[-1]
    assert row.action_taken == {
        "action": "update",
        "suggestion_cleared": {"probability": 1, "impact": 2, "audit_id": 77},
    }


def test_an_edit_that_keeps_the_text_keeps_the_suggestion(web, db, pm, project):
    risk = _proposed(web, project.id)
    version = _with_suggestion(db, risk["id"])
    response = web.patch(
        _url(project.id, risk["id"]), json={"version": version, "category": "project", **MEDIUM}
    )
    assert response.status_code == 200, response.text
    assert (response.json()["suggested_probability"], response.json()["suggested_impact"]) == (
        1,
        2,
    )
    assert _audit(db)[-1].action_taken == {"action": "update"}


def test_a_patch_never_opens_an_accepted_or_rejected_risk(web, db, pm, project):
    accepted = _accept(web, project.id, _create(web, project.id, **LOW).json()).json()
    response = web.patch(_url(project.id, accepted["id"]), json={"version": 2, "status": "open"})
    assert response.status_code == 409
    proposed = _proposed(web, project.id)
    rejected = web.post(
        _url(project.id, proposed["id"]) + "/reject", json={"version": 1, "reason": "Duplicate"}
    ).json()
    for target in ("open", "closed"):
        response = web.patch(
            _url(project.id, rejected["id"]), json={"version": 2, "status": target}
        )
        assert response.status_code == 409


def test_an_accepted_risk_can_be_closed(web, db, pm, project):
    accepted = _accept(web, project.id, _create(web, project.id, **LOW).json()).json()
    response = web.patch(_url(project.id, accepted["id"]), json={"version": 2, "status": "closed"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"
    assert _audit(db)[-1].action_taken == {"action": "close"}


@pytest.mark.parametrize(
    "changes",
    [
        {"response": "reduce"},
        {"needs_judgement": True},
        {"needs_judgement": True, "probability": None, "impact": None},
        {"probability": None, "impact": None},
        {"status": "closed", "response": "reduce"},
    ],
)
def test_a_patch_keeps_an_accepted_risks_decision(web, db, pm, project, changes):
    accepted = _accept(web, project.id, _create(web, project.id, **LOW).json()).json()
    rows = len(_audit(db, escalations=True))
    response = web.patch(_url(project.id, accepted["id"]), json={"version": 2, **changes})
    assert response.status_code == 409
    assert "accepted risk" in response.json()["detail"]
    body = web.get(_url(project.id, accepted["id"])).json()
    assert (body["status"], body["response"], body["band"]) == ("accepted", "accept", "Low")
    assert body["needs_judgement"] is False
    assert len(_audit(db, escalations=True)) == rows


def test_a_patch_may_still_rescore_an_accepted_risk(web, db, pm, project):
    accepted = _accept(web, project.id, _create(web, project.id, **LOW).json()).json()
    url = _url(project.id, accepted["id"])
    same = web.patch(url, json={"version": 2, "probability": 2, "impact": 1})
    assert same.status_code == 200, same.text
    assert (same.json()["status"], same.json()["response"]) == ("accepted", "accept")
    higher = web.patch(url, json={"version": 3, **MEDIUM, "response": "accept"})
    assert higher.status_code == 200, higher.text
    assert (higher.json()["status"], higher.json()["band"]) == ("open", "Medium")


def test_opening_a_proposed_risk(web, db, pm, project, sponsor):
    risk = _proposed(web, project.id)
    url = _url(project.id, risk["id"]) + "/open"
    assert sponsor.post(url, json={"version": 1}).status_code == 403
    assert web.post(url, json={"version": 2}).status_code == 409
    response = web.post(url, json={"version": 1})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "open"
    # Unscored, so nobody to escalate to yet.
    assert response.json()["escalated_to"] is None
    row = _audit(db)[-1]
    assert (row.action_taken, row.actor_user_id) == ({"action": "open"}, pm.id)
    assert web.post(url, json={"version": 2}).status_code == 409


def test_opening_a_scored_proposed_risk_escalates_it(web, db, pm, project):
    risk = _proposed(web, project.id)
    web.patch(_url(project.id, risk["id"]), json={"version": 1, **HIGH})
    response = web.post(_url(project.id, risk["id"]) + "/open", json={"version": 2})
    assert response.json()["escalated_to"] == "sponsor"
    assert _actions(db)[-2:] == ["open", "escalate"]


@pytest.mark.parametrize("body", [{"version": 1}, {"version": 1, "reason": "   "}])
def test_rejecting_needs_a_reason(web, db, pm, project, body):
    risk = _proposed(web, project.id)
    assert web.post(_url(project.id, risk["id"]) + "/reject", json=body).status_code == 422


def test_rejecting_a_proposed_risk(web, db, pm, project, sponsor):
    risk = _proposed(web, project.id)
    url = _url(project.id, risk["id"]) + "/reject"
    body = {"version": 1, "reason": "Already covered by the supplier risk"}
    assert sponsor.post(url, json=body).status_code == 403
    response = web.post(url, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    row = _audit(db)[-1]
    assert row.action_taken == {"action": "reject", "reason": body["reason"]}
    assert web.post(url, json={**body, "version": 2}).status_code == 409
    open_risk = _create(web, project.id).json()
    response = web.post(_url(project.id, open_risk["id"]) + "/reject", json=body)
    assert response.status_code == 409


ESCALATIONS = "/api/v1/risks/escalations"


def test_the_escalations_list_is_for_the_callers_role(web, db, pm, project, sponsor, sign_in):
    other = _project(db, "Other Project")
    low = _create(web, project.id, **LOW).json()
    medium = _create(web, project.id, **MEDIUM).json()
    high = _create(web, other.id, **HIGH).json()
    _create(web, project.id, needs_judgement=True, probability=None, impact=None)

    mine = web.get(ESCALATIONS).json()
    assert [item["id"] for item in mine["items"]] == [low["id"]]
    theirs = sponsor.get(ESCALATIONS).json()
    assert theirs["total"] == 2
    assert [item["id"] for item in theirs["items"]] == [high["id"], medium["id"]]
    assert theirs["items"][0]["project_name"] == "Other Project"
    paged = sponsor.get(ESCALATIONS, params={"limit": 1, "offset": 1}).json()
    assert [item["id"] for item in paged["items"]] == [medium["id"]]

    # Once the owner acts, it drops off.
    _accept(sponsor, project.id, medium)
    assert [item["id"] for item in sponsor.get(ESCALATIONS).json()["items"]] == [high["id"]]
    _accept(sponsor, other.id, high)
    assert sponsor.get(ESCALATIONS).json()["total"] == 0

    auditor = _client(db, sign_in, "auditor", "auditor.one@pmo.example")
    assert auditor.get(ESCALATIONS).json()["total"] == 0
    assert TestClient(app, base_url="http://localhost").get(ESCALATIONS).status_code == 401


def test_rescore_reopens_an_accepted_risk_that_rises(web, db, pm, project, tmp_path):
    risk = _create(web, project.id, probability=2, impact=3).json()  # 6: Low
    _accept(web, project.id, risk)
    for name in ("approvers.yaml", "tiers.yaml", "gates.yaml", "risk.yaml"):
        (tmp_path / name).write_text((REPO_ROOT / "policies" / name).read_text())
    data = yaml.safe_load(RISK_YAML.read_text())
    data["priority_bands"][0]["max_score"] = 5
    data["priority_bands"][1]["min_score"] = 6
    (tmp_path / "risk.yaml").write_text(yaml.safe_dump(data))
    policy = load_policies(tmp_path).risk

    with Session(engine) as session:
        assert risks.rescore(session, policy) == 1
    db.expire_all()
    stored = db.get(Risk, risk["id"])
    assert (stored.status, stored.band, stored.escalated_to) == ("open", "Medium", "sponsor")
    assert _actions(db)[-2:] == ["rescore", "escalate"]


# --- Escalation hold and final rejections (item 3.6)


def _stored(db: Session, risk_id: int) -> dict:
    db.expire_all()
    return risks.snapshot(db.get(Risk, risk_id))


def test_a_pm_closing_a_risk_escalated_to_the_sponsor_is_403_and_audited(web, db, pm, project):
    risk = _create(web, project.id, **HIGH).json()
    url = _url(project.id, risk["id"])
    before = _stored(db, risk["id"])
    rows = len(_audit(db, escalations=True))
    response = web.patch(url, json={"version": 1, "status": "closed"})
    assert response.status_code == 403
    assert "sponsor" in response.json()["detail"]
    assert _stored(db, risk["id"]) == before
    body = web.get(url).json()
    assert (body["status"], body["escalated_to"], body["escalated_band"]) == (
        "open",
        "sponsor",
        "High",
    )
    new_rows = _audit(db, escalations=True)[rows:]
    assert len(new_rows) == 1
    refused = new_rows[0]
    assert (refused.status, refused.actor_user_id, refused.trigger) == ("rejected", pm.id, "human")
    assert (refused.subject_id, refused.version) == (str(risk["id"]), 1)
    assert refused.action_taken == {
        "action": "close",
        "band": "High",
        "owner_role": "sponsor",
        "actor_role": "project_manager",
        "attempted": {"status": "closed"},
    }
    assert refused.output["status"] == "open"


def test_a_patch_that_closes_and_edits_is_refused_whole(web, db, pm, project):
    risk = _create(web, project.id, **MEDIUM).json()
    before = _stored(db, risk["id"])
    response = web.patch(
        _url(project.id, risk["id"]),
        json={"version": 1, "status": "closed", "title": "Renamed", **HIGH},
    )
    assert response.status_code == 403
    assert _stored(db, risk["id"]) == before
    refused = _audit(db)[-1]
    assert refused.action_taken["attempted"] == {"status": "closed", "title": "Renamed", **HIGH}


@pytest.mark.parametrize(
    ("start", "change"),
    [
        (HIGH, MEDIUM),
        (HIGH, LOW),
        (MEDIUM, LOW),
        (HIGH, {"needs_judgement": True, "probability": None, "impact": None}),
    ],
)
def test_a_pm_rescoring_an_escalated_risk_lower_is_403(web, db, pm, project, start, change):
    risk = _create(web, project.id, **start).json()
    before = _stored(db, risk["id"])
    rows = len(_audit(db, escalations=True))
    response = web.patch(_url(project.id, risk["id"]), json={"version": 1, **change})
    assert response.status_code == 403
    assert "lower band" in response.json()["detail"]
    assert _stored(db, risk["id"]) == before
    new_rows = _audit(db, escalations=True)[rows:]
    assert [(row.status, row.action_taken["action"]) for row in new_rows] == [
        ("rejected", "rescore")
    ]
    assert new_rows[0].action_taken["band"] == before["escalated_band"]
    assert new_rows[0].action_taken["owner_role"] == "sponsor"


@pytest.mark.parametrize(
    ("start", "change", "band"),
    [
        (MEDIUM, {"probability": 4, "impact": 3}, "Medium"),  # 12, same band
        (MEDIUM, HIGH, "High"),
        (HIGH, {"probability": 5, "impact": 5}, "High"),
        (HIGH, {"title": "Renamed", "cause": "A new cause", "response_plan": "A new plan"}, "High"),
    ],
)
def test_a_pm_may_raise_or_edit_an_escalated_risk(web, db, pm, project, start, change, band):
    risk = _create(web, project.id, **start).json()
    response = web.patch(_url(project.id, risk["id"]), json={"version": 1, **change})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["band"], body["escalated_to"], body["version"]) == (band, "sponsor", 2)
    assert all(row.status == "ok" for row in _audit(db, escalations=True))


def test_a_pm_may_close_a_risk_escalated_to_the_pm(web, db, pm, project):
    risk = _create(web, project.id, **LOW).json()
    assert risk["escalated_to"] == "project_manager"
    response = web.patch(_url(project.id, risk["id"]), json={"version": 1, "status": "closed"})
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["escalated_to"]) == ("closed", None)


def test_a_pm_may_close_once_the_sponsor_accepts(web, db, pm, project, sponsor):
    risk = _create(web, project.id, **HIGH).json()
    accepted = _accept(sponsor, project.id, risk).json()
    assert (accepted["status"], accepted["escalated_to"]) == ("accepted", None)
    response = web.patch(_url(project.id, risk["id"]), json={"version": 2, "status": "closed"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"
    assert _audit(db)[-1].action_taken == {"action": "close"}


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Renamed"},
        {"probability": 5, "impact": 5},
        {"status": "closed"},
        {"status": "open"},
        {"title": BODY["title"]},  # no change at all is still refused
    ],
)
def test_any_patch_to_a_rejected_risk_is_409(web, db, pm, project, change):
    proposed = _proposed(web, project.id)
    rejected = web.post(
        _url(project.id, proposed["id"]) + "/reject", json={"version": 1, "reason": "Duplicate"}
    ).json()
    before = _stored(db, rejected["id"])
    rows = len(_audit(db, escalations=True))
    response = web.patch(_url(project.id, rejected["id"]), json={"version": 2, **change})
    assert response.status_code == 409
    assert response.json()["detail"] == "a rejected risk can't be edited"
    assert _stored(db, rejected["id"]) == before
    assert len(_audit(db, escalations=True)) == rows
