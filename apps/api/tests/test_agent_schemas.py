"""Every agent's output schema is one the structured-output API accepts (item 6.11).

The fake-client tests never send a schema to the API, so this walks each schema in code and
fails on any construct the API rejects. No network and no key."""

from typing import Any

import pytest

from app.agents import intake_agent, planner_agent, proposal_agent, risk_agent

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


def test_every_agent_request_sends_a_listed_schema():
    listed = [id(schema) for schema in SCHEMAS.values()]
    for module in (intake_agent, planner_agent, proposal_agent, risk_agent):
        source = open(module.__file__, encoding="utf-8").read()
        for line in source.splitlines():
            if '"output_config"' in line:
                schema_name = line.split('"schema":')[1].strip(" }),")
                assert id(getattr(module, schema_name)) in listed, (module.__name__, schema_name)


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
