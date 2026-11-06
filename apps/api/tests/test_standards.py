import copy
import re
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

import app.standards as standards_module
from app.authz import Role
from app.config import Settings
from app.db import engine
from app.models import Base
from app.standards import (
    MAX_PROSE_CHARS,
    PLAN_MILESTONES,
    RULE_IDS,
    StandardsError,
    get_standards,
    load_standards,
    parse_standards,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MAP_PATH = REPO_ROOT / "docs" / "standards-map.yaml"
RULES_PATH = REPO_ROOT / "policies" / "project-rules.md"


def _valid() -> dict:
    return {
        "version": 1,
        "features": [
            {
                "id": "P1-audit-trail",
                "title": "Append-only audit trail",
                "rules": ["R5"],
                "notes": "One row per decision.",
            }
        ],
    }


def _with(change) -> dict:
    data = copy.deepcopy(_valid())
    change(data["features"][0])
    return data


def _set(key, value):
    def change(feature):
        feature[key] = value

    return change


# --- The real file


def test_repository_map_is_valid():
    standards = load_standards(MAP_PATH)
    assert len(standards.features) >= len(PLAN_MILESTONES)


def test_every_plan_milestone_has_an_entry():
    covered = {feature.milestone for feature in load_standards(MAP_PATH).features}
    assert covered == set(PLAN_MILESTONES)


def test_repository_map_keeps_prose_short():
    for feature in load_standards(MAP_PATH).features:
        assert len(feature.title) + len(feature.notes) <= MAX_PROSE_CHARS, feature.id


def test_rule_ids_match_the_rules_file():
    written = re.findall(r"^- \*\*(R\d+)\.\*\*", RULES_PATH.read_text(encoding="utf-8"), re.M)
    assert tuple(written) == RULE_IDS


def test_standards_map_path_defaults_to_repository_docs():
    assert Settings(_env_file=None, app_env="test").standards_map_path == MAP_PATH


# --- Validator


def test_valid_minimal_map_parses_into_a_frozen_model():
    standards = parse_standards(_valid())
    feature = standards.features[0]
    assert feature.milestone == "P1"
    assert feature.rules == ("R5",)
    with pytest.raises(Exception, match="frozen"):
        feature.title = "changed"


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        (["R13"], "unknown rules: R13"),
        (["r1"], "unknown rules: r1"),
        (["R1", "R1"], "listed twice"),
    ],
)
def test_rejects_bad_rule_references(rules, message):
    with pytest.raises(StandardsError, match=message):
        parse_standards(_with(_set("rules", rules)))


def test_rules_are_required_but_may_be_empty():
    assert parse_standards(_with(_set("rules", []))).features[0].rules == ()

    def drop_rules(feature):
        del feature["rules"]

    with pytest.raises(StandardsError, match="rules"):
        parse_standards(_with(drop_rules))


def test_rejects_entries_over_the_prose_limit():
    # Title and notes together: one character over the limit.
    used = len("Append-only audit trail")
    over = "x" * (MAX_PROSE_CHARS - used + 1)
    with pytest.raises(StandardsError, match="limit is 200"):
        parse_standards(_with(_set("notes", over)))
    exact = "x" * (MAX_PROSE_CHARS - used)
    parse_standards(_with(_set("notes", exact)))


def test_prose_limit_counts_the_title_too():
    with pytest.raises(StandardsError, match="limit is 200"):
        parse_standards(_with(_set("title", "t" * (MAX_PROSE_CHARS + 1))))


@pytest.mark.parametrize("bad_id", ["P10-thing", "P1", "p1-thing", "P1-Thing", "P1_thing", "X1-a"])
def test_rejects_ids_that_do_not_name_a_plan_milestone(bad_id):
    with pytest.raises(StandardsError, match="id"):
        parse_standards(_with(_set("id", bad_id)))


def test_rejects_unknown_and_missing_keys():
    with pytest.raises(StandardsError, match="quote"):
        parse_standards(_with(_set("quote", "pasted text")))

    def drop_title(feature):
        del feature["title"]

    with pytest.raises(StandardsError, match="title"):
        parse_standards(_with(drop_title))


def test_rejects_duplicate_ids_wrong_version_and_empty_list():
    data = _valid()
    data["features"].append(copy.deepcopy(data["features"][0]))
    with pytest.raises(StandardsError, match="duplicate feature id"):
        parse_standards(data)
    with pytest.raises(StandardsError, match="version"):
        parse_standards({**_valid(), "version": 2})
    with pytest.raises(StandardsError, match="features"):
        parse_standards({"version": 1, "features": []})


def test_load_reports_missing_and_malformed_files(tmp_path):
    with pytest.raises(StandardsError, match="not found"):
        load_standards(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("features: [unclosed", encoding="utf-8")
    with pytest.raises(StandardsError, match="not valid YAML"):
        load_standards(bad)
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump(_valid()), encoding="utf-8")
    assert load_standards(good).features[0].id == "P1-audit-trail"


# --- Endpoint


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


def test_endpoint_needs_a_login(db, client):
    assert client.get("/api/v1/standards").status_code == 401


@pytest.mark.parametrize("role", list(Role))
def test_endpoint_serves_the_file_privately_to_every_role(db, client, sign_in, role):
    sign_in(client, db, role)
    response = client.get("/api/v1/standards")
    assert response.status_code == 200
    expected = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))
    assert response.json() == expected
    # Behind a login, so only the browser may keep it; never a shared cache.
    assert response.headers["cache-control"] == "private, max-age=3600"


def test_endpoint_is_cached_and_read_only(db, client, sign_in, monkeypatch):
    sign_in(client, db, "auditor")
    get_standards.cache_clear()
    calls = []
    real = standards_module.load_standards

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(standards_module, "load_standards", counting)
    try:
        assert client.get("/api/v1/standards").status_code == 200
        assert client.get("/api/v1/standards").status_code == 200
        assert len(calls) == 1
    finally:
        get_standards.cache_clear()
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/v1/standards").status_code == 405
