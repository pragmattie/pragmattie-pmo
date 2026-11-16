"""API shapes for the decision inbox (item 6.3)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class FactOut(BaseModel):
    key: str
    label: str
    value: str
    met: bool | None


class InboxEntryOut(BaseModel):
    # `item`: an open inbox item. `window`: a pending window action, which happens at `due_at`
    # unless the step's `who` holds it (`action_id` is what the hold route takes).
    kind: Literal["item", "window"]
    id: int
    step: str
    subject_type: str
    subject_id: int
    subject_title: str
    project_id: int | None
    project_name: str | None
    # The role the step waits on, and the person, when the project names one for that role.
    who: str
    named_user_id: int | None
    named_user_name: str | None
    # The step's plain reason, and why it went to a person (a hand-off), when it did.
    reason: str
    handoff: str | None
    created_at: datetime
    action_id: int | None
    due_at: datetime | None
    # The gate a `pass_gate` item asks for: approval, go_ahead or acceptance.
    gate_id: str | None = None
    # The agent's case: the facts, and a recommendation code wrote from them.
    facts: list[FactOut]
    recommendation: str


class InboxCountOut(BaseModel):
    count: int
