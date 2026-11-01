"""Standards traceability map: read `docs/standards-map.yaml`, validate it, return a frozen model.

The map tags every feature to general project practice, AI governance practice, and AI project practice. It holds
paraphrase and section numbers only, so prose is capped per entry. A missing or invalid file
raises `StandardsError`. The map loads on first use and is cached for the life of the process.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.config import get_settings

SCHEMA_VERSION = 1
# Our own cut-off: a short paraphrase fits, a pasted passage does not.
MAX_PROSE_CHARS = 200
PLAN_MILESTONES = ("P1", "P2", "P3", "P4", "P5", "P6", "P7")

FocusArea = Literal["initiating", "planning", "executing", "monitoring_controlling", "closing"]
Domain = Literal["governance", "scope", "schedule", "finance", "stakeholders", "resources", "risk"]
AiKind = Literal["principle", "practice_area"]
AiPhase = Literal["I", "II", "III", "IV", "V", "VI", "none"]

FOCUS_AREAS: tuple[FocusArea, ...] = get_args(FocusArea)
DOMAINS: tuple[Domain, ...] = get_args(Domain)
AI_KINDS: tuple[AiKind, ...] = get_args(AiKind)
AI_PHASES: tuple[AiPhase, ...] = get_args(AiPhase)

ID_PATTERN = re.compile(r"^(P[1-7])-[a-z0-9]+(?:-[a-z0-9]+)*$")
SECTION_PATTERN = re.compile(r"^\d+(?:\.\d+)*$")


class StandardsError(ValueError):
    """The standards map is missing, unreadable or breaks a validation rule."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class AiStandardRef(_Strict):
    kind: AiKind
    name: str = Field(min_length=1)
    section: str

    @field_validator("section")
    @classmethod
    def _section_number(cls, value: str) -> str:
        if not SECTION_PATTERN.match(value):
            raise ValueError("must be a section number such as 3.2")
        return value


class Feature(_Strict):
    id: str
    title: str = Field(min_length=1)
    focus_area: FocusArea
    domain: Domain
    ai_standard: AiStandardRef
    ai_phase: AiPhase
    notes: str = ""

    @field_validator("id")
    @classmethod
    def _id_names_a_milestone(cls, value: str) -> str:
        if not ID_PATTERN.match(value):
            raise ValueError("must be P<1-7>-<lower-case-slug>, e.g. P1-audit-trail")
        return value

    @property
    def milestone(self) -> str:
        return self.id.split("-", 1)[0]

    @model_validator(mode="after")
    def _prose_is_short(self) -> "Feature":
        length = len(self.title) + len(self.notes) + len(self.ai_standard.name)
        if length > MAX_PROSE_CHARS:
            raise ValueError(
                f"prose (title, notes, ai_standard.name) is {length} characters; "
                f"the limit is {MAX_PROSE_CHARS}. Paraphrase and cite section numbers instead"
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
