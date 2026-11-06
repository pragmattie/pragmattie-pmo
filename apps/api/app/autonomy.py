"""The autonomy policy (item 6.1): for every step in a project's life, whether an agent does it
alone (`agent`), after an objection window a person may hold (`window`), or a person does it
(`person`), so how much the agents do is a written, reviewable choice (R3, R4).

The steps, the agents, which agent may do which step and the floors are fixed here, never in the
file. Five steps are always decided by a person (R1): a file that sets one of them lower is
refused. Each agent has an off switch in the file (R2); a switched-off agent's steps go to a
person. `score_risk` never applies to a risk marked `needs_judgement` (R9), whatever the file
says (`effective_mode`).

The file is `policies/autonomy.yaml`, read with the same strict helpers as the other policies
(`app.policy`): a missing or invalid file raises `PolicyError`, and nothing is filled in from
code. Running the steps is the orchestrator's job (6.2), not this module's.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from app.config import get_settings
from app.policy import PolicyError, _choice, _keys, _mapping, _read_with_digest
from app.roles import Role

AUTONOMY_FILE = "autonomy.yaml"

AGENT = "agent"
WINDOW = "window"
PERSON = "person"
MODES = (AGENT, WINDOW, PERSON)

# The agents, named as their audit rows name them. Some are not built yet (the planner and the
# simulated client): the orchestrator sends a step whose agent isn't built to a person.
AGENTS = ("intake_agent", "risk_agent", "proposal_agent", "planner_agent", "simulated_client")

# Every step, and the agents that may do it. A step with no agents can only be `person`.
STEP_AGENTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "draft_value_case": ("intake_agent",),
        "draft_readiness": ("intake_agent",),
        "draft_plan": ("planner_agent",),
        "propose_risks": ("risk_agent",),
        "score_risk": ("risk_agent",),
        "open_low_risk": ("risk_agent",),
        "draft_proposal": ("proposal_agent",),
        "submit_proposal": ("proposal_agent",),
        "approve_proposal": (),
        "send_proposal": (),
        "client_answer": ("simulated_client",),
        "accept_risk": (),
        "pass_gate": (),
        "decide_change_pm": (),
        "decide_change_sponsor": (),
    }
)
STEPS = tuple(STEP_AGENTS)

# Steps a person always decides (R1), whatever the file says.
FLOOR_STEPS = frozenset(
    {"approve_proposal", "send_proposal", "accept_risk", "pass_gate", "decide_change_sponsor"}
)

SCORE_RISK = "score_risk"
MIN_WINDOW = 1
MAX_WINDOW = 24 * 60


@dataclass(frozen=True)
class Step:
    id: str
    mode: str
    who: str  # decides a person step, may hold a window step, gets an agent step when it's off
    agent: str | None  # for agent and window steps
    window_minutes: int | None  # for window steps


@dataclass(frozen=True)
class AutonomyPolicy:
    """Every step in `STEPS` order, each agent's off switch, and the SHA-256 of the file as
    loaded, so a step that runs can record exactly which rules applied."""

    steps: Mapping[str, Step]
    enabled: Mapping[str, bool]
    digest: str = ""


class JudgedRisk(Protocol):
    needs_judgement: bool


def _window(value: Any, where: str) -> int:
    # bool is a subclass of int; `true` is not a number of minutes.
    if type(value) is not int or not MIN_WINDOW <= value <= MAX_WINDOW:
        raise PolicyError(f"{where}: expected a whole number from {MIN_WINDOW} to {MAX_WINDOW}")
    return value


def _parse_step(step_id: str, entry: Any, where: str) -> Step:
    at = f"{where} steps.{step_id}"
    entry = _mapping(entry, at)
    _keys(entry, at, ("mode", "who"), ("agent", "window_minutes"))
    mode = _choice(entry["mode"], f"{at}.mode", MODES, "mode")
    if step_id in FLOOR_STEPS and mode != PERSON:
        raise PolicyError(
            f"{at}.mode: {step_id} must be decided by a person (mode person), not {mode}"
        )
    who = _choice(entry["who"], f"{at}.who", tuple(role.value for role in Role), "role")

    agent = None
    if mode == PERSON:
        if "agent" in entry:
            raise PolicyError(f"{at}.agent: only an agent or window step names an agent")
    else:
        if "agent" not in entry:
            raise PolicyError(f"{at}.agent: an {mode} step must name its agent")
        agent = _choice(entry["agent"], f"{at}.agent", AGENTS, "agent")
        allowed = STEP_AGENTS[step_id]
        if agent not in allowed:
            may = ", ".join(allowed) if allowed else "none: a person does it"
            raise PolicyError(f"{at}.agent: {agent} can't do {step_id}; agents that may: {may}")

    window = None
    if mode == WINDOW:
        if "window_minutes" not in entry:
            raise PolicyError(f"{at}.window_minutes: a window step needs its minutes")
        window = _window(entry["window_minutes"], f"{at}.window_minutes")
    elif "window_minutes" in entry:
        raise PolicyError(f"{at}.window_minutes: only a window step has minutes")
    return Step(id=step_id, mode=mode, who=who, agent=agent, window_minutes=window)


def parse_autonomy(data: dict, where: str = AUTONOMY_FILE, digest: str = "") -> AutonomyPolicy:
    _keys(data, where, ("version", "steps", "agents"))

    raw = _mapping(data["steps"], f"{where} steps")
    unknown = [str(step) for step in raw if step not in STEP_AGENTS]
    if unknown:
        raise PolicyError(
            f"{where} steps: unknown steps: {', '.join(unknown)}; steps are {', '.join(STEPS)}"
        )
    missing = [step for step in STEPS if step not in raw]
    if missing:
        raise PolicyError(f"{where} steps: missing steps: {', '.join(missing)}")
    steps = {step: _parse_step(step, raw[step], where) for step in STEPS}

    raw = _mapping(data["agents"], f"{where} agents")
    unknown = [str(name) for name in raw if name not in AGENTS]
    if unknown:
        raise PolicyError(
            f"{where} agents: unknown agents: {', '.join(unknown)}; agents are {', '.join(AGENTS)}"
        )
    missing = [name for name in AGENTS if name not in raw]
    if missing:
        raise PolicyError(f"{where} agents: missing agents: {', '.join(missing)}")
    enabled = {}
    for name in AGENTS:
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


def effective_mode(policy: AutonomyPolicy, step: str, risk: JudgedRisk | None = None) -> str:
    """How `step` runs now: its mode in the file, except that it goes to a person when its agent
    is switched off (R2), and `score_risk` goes to a person for a risk marked `needs_judgement`
    (R9), whatever the file says."""
    rule = policy.steps[step]
    if rule.agent is not None and not policy.enabled[rule.agent]:
        return PERSON
    if step == SCORE_RISK and risk is not None and risk.needs_judgement:
        return PERSON
    return rule.mode
