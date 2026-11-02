"""Governance policies: read the YAML files in `policies/`, validate them, return frozen objects.

This module is the only code that reads policy files. A missing or invalid file raises
`PolicyError` naming the file and the problem: nothing is filled in from code. Policies load on
first use (`get_policies`), not at startup, so the API starts before the real files are applied.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from app.config import get_settings

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


@dataclass(frozen=True)
class GatesPolicy:
    """Placeholder until stage gates arrive in milestone P4."""

    gates: tuple[str, ...]


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
    path = directory / name
    try:
        text = path.read_text(encoding="utf-8")
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
    return data


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


def parse_gates(data: dict, where: str = "gates.yaml") -> GatesPolicy:
    _keys(data, where, ("version", "gates"))
    if data["gates"] != []:
        raise PolicyError(f"{where} gates: must be an empty list until stage gates arrive in P5")
    return GatesPolicy(gates=())


def load_policies(directory: Path | str) -> Policies:
    """Read and validate every policy file in `directory`; raise `PolicyError` on any problem."""
    directory = Path(directory)
    approvers = parse_approvers(_read(directory, "approvers.yaml"))
    return Policies(
        tiers=parse_tiers(_read(directory, "tiers.yaml")),
        risk=parse_risk(_read(directory, "risk.yaml"), approvers.roles),
        gates=parse_gates(_read(directory, "gates.yaml")),
        approvers=approvers,
    )


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
