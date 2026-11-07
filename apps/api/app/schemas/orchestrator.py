"""API shapes for the orchestrator (item 6.2): one pass's report, and holding a pending action."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.intake import LONG_TEXT_MAX


class OutcomeOut(BaseModel):
    step: str
    subject_type: str
    subject_id: int
    outcome: str
    detail: str


class PassOut(BaseModel):
    mode: str  # ORCHESTRATOR_MODE: off, shadow or enforce
    ran: bool
    busy: bool  # another pass was running, so this one gave up
    steps: int
    agent_calls: int
    outcomes: list[OutcomeOut]


class HoldIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The minimum length is checked by the route, so a short or missing reason is an audited
    # refusal rather than a validation error.
    reason: str = Field(default="", max_length=LONG_TEXT_MAX)


class ActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    step: str
    subject_type: str
    subject_id: str
    mode: str
    agent: str | None
    who: str
    reason: str
    status: str
    due_at: datetime
    attempts: int
    held_by: int | None
    hold_reason: str | None
    held_at: datetime | None


class HoldOut(BaseModel):
    action: ActionOut
    inbox_item_id: int
