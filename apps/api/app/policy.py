"""Governance policies: read the YAML files in `policies/`, validate them, return frozen objects.

This module is the only code that reads policy files. A missing or invalid file raises
`PolicyError` naming the file and the problem: nothing is filled in from code. Policies load on
first use (`get_policies`), not at startup, so the API starts before the real files are applied.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from app import autonomy
from app.autonomy import AutonomyPolicy, Step
from app.config import get_settings
from app.models.risk import CATEGORIES as RISK_CATEGORIES
from app.models.risk import KINDS as RISK_KINDS
from app.models.risk import RESPONSES_BY_KIND
from app.roles import Role

SCHEMA_VERSION = 1
TIERS = ("T0", "T1", "T2", "T3")
# High tiers always need a person: they can never be listed as auto-approved.
HIGH_TIERS = frozenset({"T2", "T3"})
SCALE_LEVELS = (1, 2, 3, 4, 5)
OBJECTIVES = ("schedule", "cost", "quality")
# Objectives whose threshold must state an allowed variation as a percentage.
PERCENT_OBJECTIVES = frozenset({"schedule", "cost"})
MAX_SCORE = SCALE_LEVELS[-1] * SCALE_LEVELS[-1]


class PolicyError(ValueError):
    """A policy file is missing, unreadable or breaks a validation rule."""


@dataclass(frozen=True)
class Tier:
    name: str
    description: str
    approvals_required: int


@dataclass(frozen=True)
class TierPolicy:
    tiers: Mapping[str, Tier]  # every tier, T0 to T3 in order
    auto_approved: frozenset[str]
    fallback_tier: str  # used when a risk assessment fails


@dataclass(frozen=True)
class ProbabilityLevel:
    level: int
    label: str
    definition: str


@dataclass(frozen=True)
class ImpactLevel:
    """One impact level; each definition is a size of effect, so it fits threats and
    opportunities alike."""

    level: int
    label: str
    schedule: str
    cost: str
    quality: str


@dataclass(frozen=True)
class ObjectiveThreshold:
    objective: str
    definition: str
    allowed_variation_pct: Decimal | None  # plus or minus; None where it is not a percentage


@dataclass(frozen=True)
class PriorityBand:
    name: str
    min_score: int  # probability x impact, inclusive
    max_score: int
    escalation_owner: str  # a role in approvers.yaml


@dataclass(frozen=True)
class RiskPolicy:
    probability: Mapping[int, ProbabilityLevel]
    impact: Mapping[int, ImpactLevel]
    thresholds: Mapping[str, ObjectiveThreshold]
    priority_bands: tuple[PriorityBand, ...]  # ordered, non-overlapping, covering 1 to 25
    minimum_band_by_impact: Mapping[int, str]  # e.g. impact 5 is at least "Medium"


# Stage gates (item 4.4).
STAGES = ("intake", "planning", "delivery", "closure", "closed")
GATE_SUBJECTS = ("request", "project")
# The automatic checks the code knows, and the subjects each one can read; `app.gates.CHECKS`
# implements each one for each of its subjects.
CHECK_SUBJECTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "value_case_confirmed": ("request",),
        "readiness_confirmed": ("request",),
        "readiness_not_red": ("request", "project"),
        "yellow_actions_owned": ("request",),
        # The go-ahead gate (item 5.4).
        "plan_complete": ("project",),
        "estimate_within_budget": ("project",),
        "proposal_accepted": ("project",),
        "no_unreviewed_risks": ("project",),
        "escalations_settled": ("project",),
    }
)
KNOWN_CHECKS = tuple(CHECK_SUBJECTS)
GATE_ID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
MAX_GATE_ID = 64
MAX_CRITERION_TEXT = 500


@dataclass(frozen=True)
class Criterion:
    """One thing that must be true to pass a gate: an automatic `check`, or a `person` tick."""

    id: str
    text: str
    check: str | None  # a name in KNOWN_CHECKS; None for a person criterion

    @property
    def person(self) -> bool:
        return self.check is None


@dataclass(frozen=True)
class Gate:
    id: str
    name: str
    from_stage: str
    to_stage: str
    applies_to: str
    deciders: tuple[str, ...]  # roles that may decide it
    criteria: tuple[Criterion, ...]


@dataclass(frozen=True)
class GatesPolicy:
    """The stage gates, by id in file order. `digest` is the SHA-256 of the file as loaded, so a
    decision records exactly which rules applied. An empty `gates` is the placeholder."""

    gates: Mapping[str, Gate]
    digest: str = ""


@dataclass(frozen=True)
class Approver:
    role: str
    title: str
    contact: str


@dataclass(frozen=True)
class ApproversPolicy:
    roles: Mapping[str, Approver]


@dataclass(frozen=True)
class Policies:
    tiers: TierPolicy
    risk: RiskPolicy
    gates: GatesPolicy
    approvers: ApproversPolicy


# --- Small checkers. `where` names the file and key, so every message points at the problem.


def _mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise PolicyError(f"{where}: expected a mapping")
    return value


def _keys(data: dict, where: str, required: tuple, optional: tuple = ()) -> None:
    unknown = set(data) - set(required) - set(optional)
    if unknown:
        raise PolicyError(f"{where}: unknown keys: {', '.join(sorted(map(str, unknown)))}")
    missing = [key for key in required if key not in data]
    if missing:
        raise PolicyError(f"{where}: missing keys: {', '.join(map(str, missing))}")


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{where}: expected non-empty text")
    return value.strip()


def _int(value: Any, where: str, minimum: int = 0) -> int:
    # bool is a subclass of int; `true` is not a count.
    if type(value) is not int or value < minimum:
        raise PolicyError(f"{where}: expected a whole number of at least {minimum}")
    return value


def _tier_name(value: Any, where: str) -> str:
    if value not in TIERS:
        raise PolicyError(f"{where}: unknown tier {value!r}; tiers are {', '.join(TIERS)}")
    return value


def _choice(value: Any, where: str, allowed: tuple[str, ...], what: str) -> str:
    if value not in allowed:
        raise PolicyError(f"{where}: unknown {what} {value!r}; {what}s are {', '.join(allowed)}")
    return value


def _levels(data: Any, where: str) -> dict:
    data = _mapping(data, where)
    for level in data:
        if type(level) is not int or level not in SCALE_LEVELS:
            raise PolicyError(f"{where}: unknown level {level!r}; levels are 1 to 5")
    missing = [str(level) for level in SCALE_LEVELS if level not in data]
    if missing:
        raise PolicyError(f"{where}: missing levels: {', '.join(missing)}")
    return data


def _read(directory: Path, name: str) -> dict:
    return _read_with_digest(directory, name)[0]


def _read_with_digest(directory: Path, name: str) -> tuple[dict, str]:
    """The file's data and the SHA-256 of its bytes."""
    path = directory / name
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except FileNotFoundError:
        raise PolicyError(
            f"{name}: policy file not found at {path}. The real files are applied by a person "
            "(drafts are in ci/proposed/policies/)."
        ) from None
    except UnicodeDecodeError:
        raise PolicyError(f"{name}: not valid UTF-8 text") from None
    except OSError as exc:
        raise PolicyError(f"{name}: cannot read {path}: {exc.strerror}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"{name}: not valid YAML: {exc}") from None
    data = _mapping(data, name)
    if data.get("version") != SCHEMA_VERSION:
        raise PolicyError(f"{name}: version must be {SCHEMA_VERSION}")
    return data, hashlib.sha256(raw).hexdigest()


# --- One parser per file.


def parse_tiers(data: dict, where: str = "tiers.yaml") -> TierPolicy:
    _keys(data, where, ("version", "fallback_tier", "auto_approved", "tiers"))
    raw = _mapping(data["tiers"], f"{where} tiers")
    for name in raw:
        _tier_name(name, f"{where} tiers")
    missing = [name for name in TIERS if name not in raw]
    if missing:
        raise PolicyError(f"{where} tiers: missing tiers: {', '.join(missing)}")

    tiers: dict[str, Tier] = {}
    for name in TIERS:
        at = f"{where} tiers.{name}"
        entry = _mapping(raw[name], at)
        _keys(entry, at, ("description", "approvals_required"))
        tiers[name] = Tier(
            name=name,
            description=_text(entry["description"], f"{at}.description"),
            approvals_required=_int(entry["approvals_required"], f"{at}.approvals_required"),
        )
    for lower, higher in zip(TIERS, TIERS[1:], strict=False):
        if tiers[higher].approvals_required < tiers[lower].approvals_required:
            raise PolicyError(
                f"{where} tiers: {higher} asks for fewer approvals than {lower}; "
                "a higher tier never asks for less than a lower one"
            )

    if not isinstance(data["auto_approved"], list):
        raise PolicyError(f"{where} auto_approved: expected a list of tiers")
    auto = [_tier_name(name, f"{where} auto_approved") for name in data["auto_approved"]]
    high = sorted(HIGH_TIERS & set(auto))
    if high:
        raise PolicyError(f"{where} auto_approved: {', '.join(high)} can never be auto-approved")
    for name, tier in tiers.items():
        if (name in auto) != (tier.approvals_required == 0):
            raise PolicyError(
                f"{where} tiers.{name}: a tier is auto-approved exactly when it needs 0 approvals"
            )

    fallback = _tier_name(data["fallback_tier"], f"{where} fallback_tier")
    return TierPolicy(
        tiers=MappingProxyType(tiers), auto_approved=frozenset(auto), fallback_tier=fallback
    )


def parse_approvers(data: dict, where: str = "approvers.yaml") -> ApproversPolicy:
    _keys(data, where, ("version", "roles"))
    raw = _mapping(data["roles"], f"{where} roles")
    if not raw:
        raise PolicyError(f"{where} roles: at least one role is required")
    roles: dict[str, Approver] = {}
    for role, entry in raw.items():
        at = f"{where} roles.{role}"
        role = _text(role, f"{where} roles")
        entry = _mapping(entry, at)
        _keys(entry, at, ("title", "contact"))
        contact = _text(entry["contact"], f"{at}.contact")
        # Demonstration data only: contacts use the reserved .example domain.
        if "@" not in contact or not contact.lower().endswith(".example"):
            raise PolicyError(f"{at}.contact: must be an address in the .example domain")
        roles[role] = Approver(
            role=role, title=_text(entry["title"], f"{at}.title"), contact=contact
        )
    return ApproversPolicy(roles=MappingProxyType(roles))


def _parse_bands(raw: Any, where: str, roles: Mapping[str, Approver]) -> tuple[PriorityBand, ...]:
    if not isinstance(raw, list) or not raw:
        raise PolicyError(f"{where}: expected a non-empty list of bands")
    bands = []
    for index, entry in enumerate(raw):
        at = f"{where}[{index}]"
        entry = _mapping(entry, at)
        _keys(entry, at, ("name", "min_score", "max_score", "escalation_owner"))
        band = PriorityBand(
            name=_text(entry["name"], f"{at}.name"),
            min_score=_int(entry["min_score"], f"{at}.min_score", minimum=1),
            max_score=_int(entry["max_score"], f"{at}.max_score", minimum=1),
            escalation_owner=_text(entry["escalation_owner"], f"{at}.escalation_owner"),
        )
        if band.min_score > band.max_score:
            raise PolicyError(f"{at}: min_score is above max_score")
        if band.escalation_owner not in roles:
            raise PolicyError(
                f"{at}.escalation_owner: {band.escalation_owner!r} is not a role in approvers.yaml"
            )
        bands.append(band)

    names = [band.name for band in bands]
    if len(set(names)) != len(names):
        raise PolicyError(f"{where}: band names must be unique")
    for previous, band in zip(bands, bands[1:], strict=False):
        if band.min_score < previous.min_score:
            raise PolicyError(
                f"{where}: bands are out of order ({band.name} before "
                f"{previous.name}); list them from lowest score to highest"
            )
        if band.min_score <= previous.max_score:
            raise PolicyError(f"{where}: bands {previous.name} and {band.name} overlap")
        if band.min_score > previous.max_score + 1:
            raise PolicyError(f"{where}: gap between bands {previous.name} and {band.name}")
    if bands[0].min_score != 1 or bands[-1].max_score != MAX_SCORE:
        raise PolicyError(f"{where}: bands must cover every score from 1 to {MAX_SCORE}")
    return tuple(bands)


def parse_risk(data: dict, roles: Mapping[str, Approver], where: str = "risk.yaml") -> RiskPolicy:
    _keys(
        data,
        where,
        (
            "version",
            "probability",
            "impact",
            "thresholds",
            "priority_bands",
            "minimum_band_by_impact",
        ),
    )

    probability = {}
    raw = _levels(data["probability"], f"{where} probability")
    for level in SCALE_LEVELS:
        at = f"{where} probability.{level}"
        entry = _mapping(raw[level], at)
        _keys(entry, at, ("label", "definition"))
        probability[level] = ProbabilityLevel(
            level=level,
            label=_text(entry["label"], f"{at}.label"),
            definition=_text(entry["definition"], f"{at}.definition"),
        )

    impact = {}
    raw = _levels(data["impact"], f"{where} impact")
    for level in SCALE_LEVELS:
        at = f"{where} impact.{level}"
        entry = _mapping(raw[level], at)
        _keys(entry, at, ("label", *OBJECTIVES))
        impact[level] = ImpactLevel(
            level=level,
            label=_text(entry["label"], f"{at}.label"),
            **{name: _text(entry[name], f"{at}.{name}") for name in OBJECTIVES},
        )

    thresholds = {}
    raw = _mapping(data["thresholds"], f"{where} thresholds")
    _keys(raw, f"{where} thresholds", OBJECTIVES)
    for objective in OBJECTIVES:
        at = f"{where} thresholds.{objective}"
        entry = _mapping(raw[objective], at)
        if objective in PERCENT_OBJECTIVES:
            _keys(entry, at, ("definition", "allowed_variation_pct"))
            pct = entry["allowed_variation_pct"]
            if type(pct) not in (int, float) or not 0 <= pct <= 100:
                raise PolicyError(f"{at}.allowed_variation_pct: expected a number from 0 to 100")
            pct = Decimal(str(pct))
        else:
            _keys(entry, at, ("definition",))
            pct = None
        thresholds[objective] = ObjectiveThreshold(
            objective=objective,
            definition=_text(entry["definition"], f"{at}.definition"),
            allowed_variation_pct=pct,
        )

    bands = _parse_bands(data["priority_bands"], f"{where} priority_bands", roles)
    rank = {band.name: index for index, band in enumerate(bands)}

    minimums = {}
    at = f"{where} minimum_band_by_impact"
    for level, band in _mapping(data["minimum_band_by_impact"], at).items():
        if type(level) is not int or level not in SCALE_LEVELS:
            raise PolicyError(f"{at}: unknown impact level {level!r}; levels are 1 to 5")
        band = _text(band, f"{at}.{level}")
        if band not in rank:
            raise PolicyError(f"{at}.{level}: {band!r} is not a priority band")
        minimums[level] = band
    ordered = sorted(minimums.items())
    for (lower, lower_band), (higher, higher_band) in zip(ordered, ordered[1:], strict=False):
        if rank[higher_band] < rank[lower_band]:
            raise PolicyError(f"{at}: impact {higher} has a lower minimum band than impact {lower}")

    return RiskPolicy(
        probability=MappingProxyType(probability),
        impact=MappingProxyType(impact),
        thresholds=MappingProxyType(thresholds),
        priority_bands=bands,
        minimum_band_by_impact=MappingProxyType(minimums),
    )


def _gate_id(value: Any, where: str) -> str:
    value = _text(value, where)
    if len(value) > MAX_GATE_ID or not GATE_ID.match(value):
        raise PolicyError(
            f"{where}: {value!r} must be lower-case words joined by underscores, "
            f"at most {MAX_GATE_ID} characters"
        )
    return value


def _parse_criterion(entry: Any, at: str) -> Criterion:
    entry = _mapping(entry, at)
    _keys(entry, at, ("id", "text"), ("check", "person"))
    criterion_id = _gate_id(entry["id"], f"{at}.id")
    at = f"{at} ({criterion_id})"
    if ("check" in entry) == ("person" in entry):
        raise PolicyError(f"{at}: give exactly one of `check` or `person`")
    text = _text(entry["text"], f"{at}.text")
    if len(text) > MAX_CRITERION_TEXT:
        raise PolicyError(f"{at}.text: at most {MAX_CRITERION_TEXT} characters")
    check = None
    if "check" in entry:
        check = _choice(entry["check"], f"{at}.check", KNOWN_CHECKS, "check")
    elif entry["person"] is not True:
        raise PolicyError(f"{at}.person: must be true (a person ticks it with a note)")
    return Criterion(id=criterion_id, text=text, check=check)


def parse_gates(data: dict, where: str = "gates.yaml", digest: str = "") -> GatesPolicy:
    """The gates; an empty list is the placeholder (no gate can be decided yet)."""
    roles = tuple(role.value for role in Role)
    _keys(data, where, ("version", "gates"))
    raw = data["gates"]
    if not isinstance(raw, list):
        raise PolicyError(f"{where} gates: expected a list of gates")
    gates: dict[str, Gate] = {}
    for index, entry in enumerate(raw):
        at = f"{where} gates[{index}]"
        entry = _mapping(entry, at)
        _keys(
            entry,
            at,
            ("id", "name", "from_stage", "to_stage", "applies_to", "deciders", "criteria"),
        )
        gate_id = _gate_id(entry["id"], f"{at}.id")
        if gate_id in gates:
            raise PolicyError(f"{at}.id: duplicate gate id {gate_id!r}")
        at = f"{where} gates.{gate_id}"
        from_stage = _choice(entry["from_stage"], f"{at}.from_stage", STAGES, "stage")
        to_stage = _choice(entry["to_stage"], f"{at}.to_stage", STAGES, "stage")
        if from_stage == to_stage:
            raise PolicyError(f"{at}: a gate must lead to a different stage")
        applies_to = _choice(entry["applies_to"], f"{at}.applies_to", GATE_SUBJECTS, "subject")

        deciders = entry["deciders"]
        if not isinstance(deciders, list) or not deciders:
            raise PolicyError(f"{at}.deciders: expected a non-empty list of roles")
        for role in deciders:
            _choice(role, f"{at}.deciders", roles, "role")
        if len(set(deciders)) != len(deciders):
            raise PolicyError(f"{at}.deciders: roles must not repeat")

        raw_criteria = entry["criteria"]
        if not isinstance(raw_criteria, list) or not raw_criteria:
            raise PolicyError(f"{at}.criteria: expected a non-empty list of criteria")
        criteria: list[Criterion] = []
        for position, item in enumerate(raw_criteria):
            criterion = _parse_criterion(item, f"{at}.criteria[{position}]")
            if criterion.check is not None and applies_to not in CHECK_SUBJECTS[criterion.check]:
                raise PolicyError(
                    f"{at}.criteria[{position}].check: {criterion.check!r} can't check a "
                    f"{applies_to}"
                )
            if any(c.id == criterion.id for c in criteria):
                raise PolicyError(
                    f"{at}.criteria[{position}].id: duplicate criterion id {criterion.id!r}"
                )
            criteria.append(criterion)

        gates[gate_id] = Gate(
            id=gate_id,
            name=_text(entry["name"], f"{at}.name"),
            from_stage=from_stage,
            to_stage=to_stage,
            applies_to=applies_to,
            deciders=tuple(deciders),
            criteria=tuple(criteria),
        )
    return GatesPolicy(gates=MappingProxyType(gates), digest=digest)


def load_policies(directory: Path | str) -> Policies:
    """Read and validate every policy file in `directory`; raise `PolicyError` on any problem."""
    directory = Path(directory)
    approvers = parse_approvers(_read(directory, "approvers.yaml"))
    gates, digest = _read_with_digest(directory, "gates.yaml")
    return Policies(
        tiers=parse_tiers(_read(directory, "tiers.yaml")),
        risk=parse_risk(_read(directory, "risk.yaml"), approvers.roles),
        gates=parse_gates(gates, digest=digest),
        approvers=approvers,
    )


@dataclass(frozen=True)
class RiskTemplate:
    """One starter risk. No probability or impact: those depend on the project."""

    id: str
    kind: str
    category: str
    title: str
    cause: str
    effect: str
    objectives: tuple[str, ...]
    response: str
    response_plan: str


@dataclass(frozen=True)
class RiskLibrary:
    templates: Mapping[str, RiskTemplate]  # by id, in file order


RISK_LIBRARY_FILE = "risk-library.yaml"
TEMPLATE_KEYS = (
    "id",
    "kind",
    "category",
    "title",
    "cause",
    "effect",
    "objectives",
    "response",
    "response_plan",
)
TEMPLATE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_TEMPLATE_ID = 64
MAX_TEMPLATE_TITLE = 200


def parse_risk_library(data: dict, where: str = RISK_LIBRARY_FILE) -> RiskLibrary:
    _keys(data, where, ("version", "templates"))
    raw = data["templates"]
    if not isinstance(raw, list) or not raw:
        raise PolicyError(f"{where} templates: expected a non-empty list of templates")
    templates: dict[str, RiskTemplate] = {}
    for index, entry in enumerate(raw):
        at = f"{where} templates[{index}]"
        entry = _mapping(entry, at)
        _keys(entry, at, TEMPLATE_KEYS)
        template_id = _text(entry["id"], f"{at}.id")
        if len(template_id) > MAX_TEMPLATE_ID or not TEMPLATE_ID.match(template_id):
            raise PolicyError(
                f"{at}.id: {template_id!r} must be lower-case words joined by hyphens, "
                f"at most {MAX_TEMPLATE_ID} characters"
            )
        if template_id in templates:
            raise PolicyError(f"{at}.id: duplicate id {template_id!r}")
        at = f"{where} templates.{template_id}"
        kind = _choice(entry["kind"], f"{at}.kind", RISK_KINDS, "kind")
        objectives = entry["objectives"]
        if not isinstance(objectives, list) or not objectives:
            raise PolicyError(f"{at}.objectives: expected a non-empty list of objectives")
        for objective in objectives:
            _choice(objective, f"{at}.objectives", OBJECTIVES, "objective")
        if len(set(objectives)) != len(objectives):
            raise PolicyError(f"{at}.objectives: objectives must not repeat")
        title = _text(entry["title"], f"{at}.title")
        if len(title) > MAX_TEMPLATE_TITLE:
            raise PolicyError(f"{at}.title: at most {MAX_TEMPLATE_TITLE} characters")
        templates[template_id] = RiskTemplate(
            id=template_id,
            kind=kind,
            category=_choice(entry["category"], f"{at}.category", RISK_CATEGORIES, "category"),
            title=title,
            cause=_text(entry["cause"], f"{at}.cause"),
            effect=_text(entry["effect"], f"{at}.effect"),
            objectives=tuple(objectives),
            response=_choice(
                entry["response"], f"{at}.response", RESPONSES_BY_KIND[kind], f"{kind} response"
            ),
            response_plan=_text(entry["response_plan"], f"{at}.response_plan"),
        )
    return RiskLibrary(templates=MappingProxyType(templates))


def load_risk_library(directory: Path | str) -> RiskLibrary:
    """Read and validate `risk-library.yaml` in `directory`; raise `PolicyError` on any problem.

    Loaded apart from `load_policies`, so a missing library only affects the library's routes."""
    return parse_risk_library(_read(Path(directory), RISK_LIBRARY_FILE))


@lru_cache
def get_risk_library() -> RiskLibrary:
    """The risk library in the configured policies directory, loaded on first use. A failure is
    not cached, so the next call tries again once the file is fixed."""
    return load_risk_library(get_settings().policies_dir)


@lru_cache
def get_policies() -> Policies:
    """The policies in the configured directory, loaded on first use. A failure is not cached,
    so the next call tries again once the files are fixed."""
    return load_policies(get_settings().policies_dir)


def score_risk(
    policy: RiskPolicy, probability: int | None, impact: int | None
) -> tuple[int | None, str | None]:
    """A risk's score (probability x impact) and priority band under `policy`.

    The band is the one whose range holds the score, raised to `minimum_band_by_impact` for the
    risk's impact where that is higher. With no probability or impact yet (a risk that needs a
    person's judgement and has not had one) there is no score and no band: `(None, None)`.
    """
    if probability is None or impact is None:
        return None, None
    if probability not in SCALE_LEVELS or impact not in SCALE_LEVELS:
        raise ValueError("probability and impact must each be 1 to 5")
    score = probability * impact
    bands = policy.priority_bands
    index = next(i for i, band in enumerate(bands) if band.min_score <= score <= band.max_score)
    floor = policy.minimum_band_by_impact.get(impact)
    if floor is not None:
        index = max(index, next(i for i, band in enumerate(bands) if band.name == floor))
    return score, bands[index].name


# Change requests (item 5.5).


@dataclass(frozen=True)
class ChangesPolicy:
    """Who decides a change request. The project manager decides a change that adds at most
    `max_cost_increase_pct` to the baseline total, moves no milestone later by more than
    `max_date_move_working_days`, and changes no scope; `escalate_to` decides the rest."""

    max_cost_increase_pct: Decimal
    max_date_move_working_days: int
    escalate_to: str


CHANGES_FILE = "changes.yaml"
# The roles a change can go to: the code knows how each one decides.
CHANGE_ESCALATIONS = (Role.SPONSOR.value,)
MAX_CHANGE_PCT = Decimal(100)


def _percent(value: Any, where: str) -> Decimal:
    """A percentage from 0 to 100 with at most 2 decimal places."""
    if type(value) not in (int, float):
        raise PolicyError(f"{where}: expected a number from 0 to {MAX_CHANGE_PCT}")
    number = Decimal(str(value))
    if not number.is_finite() or not 0 <= number <= MAX_CHANGE_PCT:
        raise PolicyError(f"{where}: expected a number from 0 to {MAX_CHANGE_PCT}")
    if number.as_tuple().exponent < -2:  # type: ignore[operator]
        raise PolicyError(f"{where}: at most 2 decimal places")
    return number


def parse_changes(data: dict, where: str = CHANGES_FILE) -> ChangesPolicy:
    _keys(data, where, ("version", "project_manager", "escalate_to"))
    limits = _mapping(data["project_manager"], f"{where} project_manager")
    at = f"{where} project_manager"
    _keys(limits, at, ("max_cost_increase_pct", "max_date_move_working_days"))
    return ChangesPolicy(
        max_cost_increase_pct=_percent(
            limits["max_cost_increase_pct"], f"{at}.max_cost_increase_pct"
        ),
        max_date_move_working_days=_int(
            limits["max_date_move_working_days"], f"{at}.max_date_move_working_days"
        ),
        escalate_to=_choice(
            data["escalate_to"], f"{where} escalate_to", CHANGE_ESCALATIONS, "role"
        ),
    )


def load_changes(directory: Path | str) -> ChangesPolicy:
    """Read and validate `changes.yaml` in `directory`; raise `PolicyError` on any problem.

    Loaded apart from `load_policies`, so a missing file only affects the change routes."""
    return parse_changes(_read(Path(directory), CHANGES_FILE))


@lru_cache
def get_changes() -> ChangesPolicy:
    """The changes policy in the configured directory, loaded on first use. A failure is not
    cached, so the next call tries again once the file is fixed."""
    return load_changes(get_settings().policies_dir)


# The autonomy policy (item 6.1). The steps, agents and floors are fixed in `app.autonomy`.

AUTONOMY_FILE = "autonomy.yaml"


def _window_minutes(value: Any, where: str) -> int:
    # bool is a subclass of int; `true` is not a number of minutes.
    if type(value) is not int or not autonomy.MIN_WINDOW <= value <= autonomy.MAX_WINDOW:
        raise PolicyError(
            f"{where}: expected a whole number from {autonomy.MIN_WINDOW} to {autonomy.MAX_WINDOW}"
        )
    return value


def _parse_autonomy_step(step_id: str, entry: Any, where: str) -> Step:
    at = f"{where} steps.{step_id}"
    entry = _mapping(entry, at)
    _keys(entry, at, ("mode", "who"), ("agent", "window_minutes"))
    mode = _choice(entry["mode"], f"{at}.mode", autonomy.MODES, "mode")
    if step_id in autonomy.FLOOR_STEPS and mode != autonomy.PERSON:
        raise PolicyError(
            f"{at}.mode: {step_id} must be decided by a person (mode person), not {mode}"
        )
    who = _choice(entry["who"], f"{at}.who", tuple(role.value for role in Role), "role")
    # Imported here: app.authz imports this module, so a module-level import would be circular.
    from app.authz import Action, can

    permission = autonomy.who_permission(step_id)
    if not can(who, Action(permission)):
        raise PolicyError(f"{at}.who: {who} can't take {step_id}: it needs {permission}")

    agent = None
    if mode == autonomy.PERSON:
        if "agent" in entry:
            raise PolicyError(f"{at}.agent: only an agent or window step names an agent")
    else:
        if "agent" not in entry:
            raise PolicyError(f"{at}.agent: an {mode} step must name its agent")
        agent = _choice(entry["agent"], f"{at}.agent", autonomy.AGENTS, "agent")
        allowed = autonomy.STEP_AGENTS[step_id]
        if agent not in allowed:
            may = ", ".join(allowed) if allowed else "none: a person does it"
            raise PolicyError(f"{at}.agent: {agent} can't do {step_id}; agents that may: {may}")

    window = None
    if mode == autonomy.WINDOW:
        if "window_minutes" not in entry:
            raise PolicyError(f"{at}.window_minutes: a window step needs its minutes")
        window = _window_minutes(entry["window_minutes"], f"{at}.window_minutes")
    elif "window_minutes" in entry:
        raise PolicyError(f"{at}.window_minutes: only a window step has minutes")
    return Step(id=step_id, mode=mode, who=who, agent=agent, window_minutes=window)


def parse_autonomy(data: dict, where: str = AUTONOMY_FILE, digest: str = "") -> AutonomyPolicy:
    _keys(data, where, ("version", "steps", "agents"))

    raw = _mapping(data["steps"], f"{where} steps")
    unknown = [str(step) for step in raw if step not in autonomy.STEP_AGENTS]
    if unknown:
        raise PolicyError(
            f"{where} steps: unknown steps: {', '.join(unknown)}; "
            f"steps are {', '.join(autonomy.STEPS)}"
        )
    missing = [step for step in autonomy.STEPS if step not in raw]
    if missing:
        raise PolicyError(f"{where} steps: missing steps: {', '.join(missing)}")
    steps = {step: _parse_autonomy_step(step, raw[step], where) for step in autonomy.STEPS}

    raw = _mapping(data["agents"], f"{where} agents")
    unknown = [str(name) for name in raw if name not in autonomy.AGENTS]
    if unknown:
        raise PolicyError(
            f"{where} agents: unknown agents: {', '.join(unknown)}; "
            f"agents are {', '.join(autonomy.AGENTS)}"
        )
    missing = [name for name in autonomy.AGENTS if name not in raw]
    if missing:
        raise PolicyError(f"{where} agents: missing agents: {', '.join(missing)}")
    enabled = {}
    for name in autonomy.AGENTS:
        at = f"{where} agents.{name}"
        entry = _mapping(raw[name], at)
        _keys(entry, at, ("enabled",))
        if not isinstance(entry["enabled"], bool):
            raise PolicyError(f"{at}.enabled: expected true or false")
        enabled[name] = entry["enabled"]

    return AutonomyPolicy(
        steps=MappingProxyType(steps), enabled=MappingProxyType(enabled), digest=digest
    )


def load_autonomy(directory: Path | str) -> AutonomyPolicy:
    """Read and validate `autonomy.yaml` in `directory`; raise `PolicyError` on any problem.

    Loaded apart from `app.policy.load_policies`, so a missing file only affects autonomy."""
    data, digest = _read_with_digest(Path(directory), AUTONOMY_FILE)
    return parse_autonomy(data, digest=digest)


@lru_cache
def get_autonomy() -> AutonomyPolicy:
    """The autonomy policy in the configured directory, loaded on first use. A failure is not
    cached, so the next call tries again once the file is fixed."""
    return load_autonomy(get_settings().policies_dir)
