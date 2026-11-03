"""The risk register's model and API (item 3.1)."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.api.risks as risks_api
from app import risks
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
    response = web.patch(_url(project.id, risk_id), json={"version": 1, "impact": 1})
    assert response.status_code == 409
    db.expire_all()
    stored = db.get(Risk, risk_id)
    assert (stored.title, stored.impact, stored.version) == ("Changed meanwhile", 4, 2)
    assert len(_audit(db)) == 1


def test_create_change_and_close_are_one_audit_row_each(web, db, pm, project):
    risk = _create(web, project.id).json()
    url = _url(project.id, risk["id"])
    web.patch(url, json={"version": 1, "impact": 5})
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
    assert created.output["score"] == 12
    assert (changed.signals["impact"], changed.signals["score"], changed.signals["band"]) == (
        4,
        12,
        "Medium",
    )
    assert (changed.output["impact"], changed.output["score"], changed.output["band"]) == (
        5,
        15,
        "High",
    )
    assert (closed.signals["status"], closed.output["status"]) == ("open", "closed")


def test_a_closed_risk_can_be_reopened(web, db, pm, project):
    risk = _create(web, project.id).json()
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
        {"band": "Low", "count": 0},
        {"band": "Medium", "count": 3},
        {"band": "High", "count": 1},
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
    risk = _create(web, project.id).json()
    _create(web, project.id)  # another risk's rows stay out
    web.patch(_url(project.id, risk["id"]), json={"version": 1, "title": "Renamed"})
    web.patch(_url(project.id, risk["id"]), json={"version": 2, "status": "closed"})

    sign_in_reader = TestClient(app, base_url="http://localhost")
    response = sign_in_reader.get(_url(project.id, risk["id"]) + "/history")
    assert response.status_code == 401

    response = web.get(_url(project.id, risk["id"]) + "/history")
    assert response.status_code == 200, response.text
    page = response.json()
    # A new Medium risk is escalated to the sponsor as it is created.
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
