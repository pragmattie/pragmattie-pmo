"""API shape of the autonomy policy (item 6.1), read-only."""

from typing import Literal

from pydantic import BaseModel

Mode = Literal["agent", "window", "person"]


class StepOut(BaseModel):
    id: str
    mode: Mode  # as the policy file sets it
    effective_mode: Mode  # after the agents' off switches
    agent: str | None
    who: str
    window_minutes: int | None
    person_only: bool  # a step a person always decides, whatever the file says


class AgentSwitchOut(BaseModel):
    name: str
    enabled: bool


class AutonomyOut(BaseModel):
    digest: str  # SHA-256 of the policy file as loaded
    steps: list[StepOut]
    agents: list[AgentSwitchOut]
