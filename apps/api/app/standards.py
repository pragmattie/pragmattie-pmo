"""Standards traceability map: read `docs/standards-map.yaml`, validate it, return a frozen model.

The map says which of our own rules (`policies/project-rules.md`) each feature follows. Entries
stay short, and a rule reference must be one of `RULE_IDS`. A missing or invalid file raises
`StandardsError`. The map loads on first use and is cached for the life of the process.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.config import get_settings

SCHEMA_VERSION = 1
MAX_PROSE_CHARS = 200
PLAN_MILESTONES = ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9")

# The rules in `policies/project-rules.md`; a test keeps this in step with that file.
RULE_IDS = tuple(f"R{number}" for number in range(1, 13))

ID_PATTERN = re.compile(r"^(P[1-9])-[a-z0-9]+(?:-[a-z0-9]+)*$")


class StandardsError(ValueError):
    """The standards map is missing, unreadable or breaks a validation rule."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Feature(_Strict):
    id: str
    title: str = Field(min_length=1)
    rules: tuple[str, ...]
    notes: str = ""

    @field_validator("id")
    @classmethod
    def _id_names_a_milestone(cls, value: str) -> str:
        if not ID_PATTERN.match(value):
            raise ValueError("must be P<1-9>-<lower-case-slug>, e.g. P1-audit-trail")
        return value

    @field_validator("rules")
    @classmethod
    def _known_rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [rule for rule in value if rule not in RULE_IDS]
        if unknown:
            raise ValueError(f"unknown rules: {', '.join(unknown)}")
        if len(set(value)) != len(value):
            raise ValueError("a rule is listed twice")
        return value

    @property
    def milestone(self) -> str:
        return self.id.split("-", 1)[0]

    @model_validator(mode="after")
    def _prose_is_short(self) -> "Feature":
        length = len(self.title) + len(self.notes)
        if length > MAX_PROSE_CHARS:
            raise ValueError(
                f"prose (title, notes) is {length} characters; the limit is {MAX_PROSE_CHARS}"
            )
        return self


class StandardsMap(_Strict):
    version: Literal[1]
    features: tuple[Feature, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "StandardsMap":
        seen: set[str] = set()
        for feature in self.features:
            if feature.id in seen:
                raise ValueError(f"duplicate feature id {feature.id!r}")
            seen.add(feature.id)
        return self


def parse_standards(data: object, where: str = "standards-map.yaml") -> StandardsMap:
    try:
        return StandardsMap.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, err['loc'])) or '(root)'}: {err['msg']}" for err in exc.errors()
        )
        raise StandardsError(f"{where}: {problems}") from None


def load_standards(path: Path | str) -> StandardsMap:
    path = Path(path)
    where = path.name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise StandardsError(f"{where}: standards map not found at {path}") from None
    except UnicodeDecodeError:
        raise StandardsError(f"{where}: not valid UTF-8 text") from None
    except OSError as exc:
        raise StandardsError(f"{where}: cannot read {path}: {exc.strerror}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StandardsError(f"{where}: not valid YAML: {exc}") from None
    return parse_standards(data, where)


@lru_cache
def get_standards() -> StandardsMap:
    return load_standards(get_settings().standards_map_path)
