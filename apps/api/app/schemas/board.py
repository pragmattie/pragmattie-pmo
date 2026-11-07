"""API shapes for the portfolio board (item 6.4)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.common import MoneyStr


class WaitingOut(BaseModel):
    step: str
    subject_type: str
    subject_id: int
    # `agent`: an agent has it (now, or after a window a person may hold). `person`: a person.
    holder: Literal["agent", "person"]
    # The role a person step waits on, or the agent's name.
    who: str
    text: str
    due_at: datetime | None
    simulated: bool


class CardOut(BaseModel):
    # `request-12` or `project-7`: stable across reads, so the page can tell a card that moved.
    key: str
    kind: Literal["request", "project"]
    id: int
    column: str
    name: str
    client_id: int | None
    client_name: str | None
    project_manager: str | None
    health: str | None
    proposal_status: str | None
    top_risk_band: str | None
    open_risks: int
    plan_total: MoneyStr | None
    top_budget: MoneyStr | None
    stopped: bool
    held_by: Literal["agent", "person"] | None
    simulated: bool
    waiting: list[WaitingOut]


class ColumnOut(BaseModel):
    id: str
    title: str
    cards: list[CardOut]


class SummaryOut(BaseModel):
    counts: dict[str, int]
    planning_value: MoneyStr
    delivery_value: MoneyStr
    open_medium_high_risks: int
    decisions_waiting: int
    stopped: int


class ClientChoiceOut(BaseModel):
    id: int
    name: str


class BoardOut(BaseModel):
    columns: list[ColumnOut]
    summary: SummaryOut
    clients: list[ClientChoiceOut]
    project_managers: list[str]
