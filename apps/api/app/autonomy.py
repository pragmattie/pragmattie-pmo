"""The autonomy policy (item 6.1): for every step in a project's life, whether an agent does it
alone (`agent`), after an objection window a person may hold (`window`), or a person does it
(`person`), so how much the agents do is a written, reviewable choice (R3, R4).

The steps, the agents, which agent may do which step and the floors are fixed here, never in the
file. Five steps are always decided by a person (R1): a file that sets one of them lower is
refused. Each agent has an off switch in the file (R2); a switched-off agent's steps go to a
person. `score_risk` never applies to a risk marked `needs_judgement` (R9), whatever the file
says (`effective_mode`).

The file is the autonomy policy in `policies/`, read and validated by `app.policy.get_autonomy`
(the only code that reads policy files): a missing or invalid file raises `PolicyError`, and
nothing is filled in from code. Running the steps is the orchestrator's job (6.2).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

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
        # Not a floor step, but no agent may decide a change until a later item adds one, so for
        # now it can only be `person`.
        "decide_change_pm": (),
        "decide_change_sponsor": (),
    }
)
STEPS = tuple(STEP_AGENTS)

# The permission (an `app.authz.Action` value) that a step's `who` must hold to decide it, hold
# its window or take it over: the loader refuses a `who` role without it. Read-only roles hold
# none of these, so they can never be `who`.
DECIDE_PERMISSION: Mapping[str, str] = MappingProxyType(
    {
        "pass_gate": "gate:decide",
        "decide_change_sponsor": "gate:decide",
        "approve_proposal": "proposal:approve",
        "accept_risk": "risk:accept",
    }
)
DEFAULT_PERMISSION = "project:write"


def who_permission(step: str) -> str:
    """The permission `step`'s `who` must hold."""
    return DECIDE_PERMISSION.get(step, DEFAULT_PERMISSION)


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
