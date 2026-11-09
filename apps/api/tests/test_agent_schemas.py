"""Every agent's output schema is one the structured-output API accepts (item 6.11).

The fake-client tests never send a schema to the API, so this walks each schema in code and
fails on any construct the API rejects. No network and no key."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app import risks
from app.agents import intake_agent, planner_agent, proposal_agent, risk_agent
from app.agents.risk_agent import AgentError
from app.config import get_settings
from app.db import engine
from app.models import Base, Project, ProjectProposal, ProjectRequest, Risk, User
from app.policy import load_policies
from app.proposals import PlanFacts

REPO_ROOT = Path(__file__).resolve().parents[3]

# Every schema an agent sends as `output_config.format.schema`.
SCHEMAS = {
    "intake_agent.OUTPUT_SCHEMA": intake_agent.OUTPUT_SCHEMA,
    "planner_agent.OUTPUT_SCHEMA": planner_agent.OUTPUT_SCHEMA,
    "proposal_agent.OUTPUT_SCHEMA": proposal_agent.OUTPUT_SCHEMA,
    "risk_agent.OUTPUT_SCHEMA": risk_agent.OUTPUT_SCHEMA,
    "risk_agent.SCORE_SCHEMA": risk_agent.SCORE_SCHEMA,
}

# The keywords the API accepts in a strict schema. Anything else (numeric and string limits,
# maxItems, oneOf, not, if/then/else, patternProperties, ...) is rejected.
ALLOWED = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "allOf",
        "description",
        "title",
        "default",
        "format",
        "minItems",
        "$ref",
        "$defs",
        "definitions",
    }
)
TYPES = frozenset({"string", "integer", "number", "boolean", "object", "array", "null"})
JSON_TYPE = {str: "string", bool: "boolean", int: "integer", float: "number", type(None): "null"}


def problems(schema: Any, path: str = "$") -> list[str]:
    """Every construct in `schema` that the structured-output API rejects, with its path."""
    if not isinstance(schema, dict):
        return [f"{path}: a schema must be an object"]
    found = [f"{path}: {key!r} is not supported" for key in sorted(set(schema) - ALLOWED)]
    declared = schema.get("type")
    if isinstance(declared, list):
        if "enum" in schema or "const" in schema:
            found.append(f"{path}: a type list with enum; write anyOf instead")
        types = set(declared)
    elif declared is not None:
        types = {declared}
    else:
        types = set()
    found += [f"{path}: unknown type {t!r}" for t in sorted(types - TYPES, key=str)]
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values:
            found.append(f"{path}: enum must be a non-empty list")
        elif types:
            for value in values:
                kind = JSON_TYPE.get(type(value))
                fits = kind in types or (kind == "integer" and "number" in types)
                if not fits:
                    found.append(f"{path}: enum value {value!r} doesn't match its type")
    if schema.get("minItems", 0) not in (0, 1):
        found.append(f"{path}: minItems may only be 0 or 1")
    if "object" in types or "properties" in schema:
        if schema.get("additionalProperties") is not False:
            found.append(f"{path}: an object needs additionalProperties: false")
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in properties:
                found.append(f"{path}: required {name!r} is not a property")
        for name, sub in properties.items():
            found += problems(sub, f"{path}.{name}")
    if "items" in schema:
        found += problems(schema["items"], f"{path}[]")
    for keyword in ("anyOf", "allOf"):
        for n, sub in enumerate(schema.get(keyword, [])):
            found += problems(sub, f"{path}.{keyword}[{n}]")
    for keyword in ("$defs", "definitions"):
        for name, sub in schema.get(keyword, {}).items():
            found += problems(sub, f"{path}.{keyword}.{name}")
    ref = schema.get("$ref")
    if ref is not None and not str(ref).startswith("#/"):
        found.append(f"{path}: only local $ref is supported")
    return found


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_agent_schema_is_one_the_api_accepts(name):
    assert problems(SCHEMAS[name]) == []


class CapturingClaude:
    """Keeps the request it is given and fails the call, so nothing is checked or saved."""

    def __init__(self):
        self.requests: list[dict] = []

    def create(self, request: dict):
        self.requests.append(request)
        raise RuntimeError("no network in this test")


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


def _sent_schemas(db: Session) -> dict[str, dict]:
    """Run every agent task once against a capturing client and return the schema each request
    actually sends as `output_config.format.schema`, by task."""
    settings = get_settings()
    policy = load_policies(REPO_ROOT / "policies").risk
    user = User(
        email="pm@pmo.example", display_name="PM", password_hash="x", role="project_manager"
    )
    project = Project(name="Harbour", stage="delivery", sponsor="S", pm="P", status="on_track")
    db.add_all([user, project])
    db.flush()
    request = ProjectRequest(
        title="Forecasts", asked_for="A", problem="B", outcome="C", requested_by=user.id
    )
    proposal = ProjectProposal(
        project_id=project.id,
        price=Decimal("0"),
        estimate_lines=[],
        milestones=[],
        deliverables=[],
        risks=[],
        plan_version_seen=1,
    )
    risk = Risk(
        project_id=project.id,
        kind="threat",
        title="Data loads fail",
        cause="A fragile export",
        effect="A late report",
        category="project",
        objectives=["schedule"],
        response="reduce",
        response_plan="Agent proposal.",
        status="proposed",
        source="agent",
    )
    db.add_all([request, proposal, risk])
    db.flush()
    facts = PlanFacts(
        version=1,
        total=Decimal("0"),
        estimate_lines=[],
        milestones=[],
        deliverables=[],
        shown_risks=[],
        risk_states={},
    )
    common = {"settings": settings, "actor_user_id": user.id}
    tasks = {
        "intake_agent.run": lambda c: intake_agent.run(db, c, request, save_it=False, **common),
        "planner_agent.run": lambda c: planner_agent.run(db, c, project, save_it=False, **common),
        "proposal_agent.run": lambda c: proposal_agent.run(
            db, c, proposal, facts, save_it=False, **common
        ),
        "risk_agent.run": lambda c: risk_agent.run(
            db, c, project, library=risks.risk_library(), policy=policy, save=False, **common
        ),
        "risk_agent.score": lambda c: risk_agent.score(
            db, c, risk, policy=policy, save=False, **common
        ),
    }
    sent = {}
    for task, call in tasks.items():
        fake = CapturingClaude()
        with pytest.raises(AgentError):
            call(fake)
        assert len(fake.requests) == 1, task
        sent[task] = fake.requests[0]["output_config"]["format"]["schema"]
    return sent


def test_every_agent_request_sends_a_schema_the_api_accepts(db):
    sent = _sent_schemas(db)
    assert set(sent) == {
        "intake_agent.run",
        "planner_agent.run",
        "proposal_agent.run",
        "risk_agent.run",
        "risk_agent.score",
    }
    found = {task: problems(schema) for task, schema in sent.items()}
    assert found == {task: [] for task in sent}


def test_the_old_planner_report_period_fails():
    old = {
        "type": "object",
        "additionalProperties": False,
        "required": ["report_period"],
        "properties": {
            "report_period": {"type": ["string", "null"], "enum": ["weekly", "monthly", None]}
        },
    }
    assert problems(old) == ["$.report_period: a type list with enum; write anyOf instead"]


@pytest.mark.parametrize(
    "schema, expected",
    [
        ({"type": "integer", "minimum": 1}, "'minimum' is not supported"),
        ({"type": "number", "maximum": 5}, "'maximum' is not supported"),
        ({"type": "string", "maxLength": 10}, "'maxLength' is not supported"),
        ({"type": "array", "items": {"type": "string"}, "maxItems": 3}, "'maxItems' is not"),
        ({"type": "array", "items": {"type": "string"}, "minItems": 2}, "minItems may only"),
        ({"type": "string", "enum": ["a", 1]}, "enum value 1 doesn't match"),
        ({"type": "object", "properties": {}}, "additionalProperties: false"),
        ({"oneOf": [{"type": "string"}]}, "'oneOf' is not supported"),
        ({"$ref": "https://schemas.example/x.json"}, "only local $ref"),
    ],
)
def test_the_check_catches_what_the_api_rejects(schema, expected):
    assert any(expected in problem for problem in problems(schema))


def test_a_nullable_enum_written_as_any_of_passes():
    assert problems(planner_agent.OUTPUT_SCHEMA["properties"]["report_period"]) == []
