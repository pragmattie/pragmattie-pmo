"""API shapes of stage gates and their decisions (items 4.4 and 5.4).

A request's `pass` names the project manager of the project it creates (item 4.6). A project's
gate (the go-ahead gate) takes the same decision without one, and shows the project's latest
baseline once passing the gate has made one.

A decision carries the person criteria's ticks and notes with it: nothing is saved before the
decision is sent, so a `GET` shows them unticked (`met` is null). Inputs refuse unknown fields.
"""

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.gate import MIN_REASON, NOTE_MAX
from app.models.intake import LONG_TEXT_MAX
from app.schemas.common import MoneyStr

Outcome = Literal["pass", "return", "decline"]
Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=MIN_REASON, max_length=LONG_TEXT_MAX),
]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NOTE_MAX)]
CriterionId = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class PersonTick(BaseModel):
    """A person's answer to one person criterion. A tick needs a note saying why."""

    model_config = ConfigDict(extra="forbid")

    id: CriterionId
    met: bool
    note: Note | None = None

    @model_validator(mode="after")
    def _tick_has_a_note(self) -> Self:
        if self.met and self.note is None:
            raise ValueError("a ticked criterion needs a note")
        return self


class ProjectGateDecisionIn(BaseModel):
    """A decision at a project's gate. `version` is the subject's version the client read; a
    stale one is a 409."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    outcome: Outcome
    reason: Reason
    criteria: list[PersonTick] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _ids_unique(self) -> Self:
        ids = [tick.id for tick in self.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("each criterion may be answered once")
        return self


class GateDecisionIn(ProjectGateDecisionIn):
    """A decision at a request's gate (the approval gate)."""

    # The project manager of the project a `pass` creates (item 4.6): needed for `pass`, refused
    # for `return` and `decline`, which create nothing.
    project_manager_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _project_manager_only_on_pass(self) -> Self:
        if self.outcome == "pass" and self.project_manager_id is None:
            raise ValueError("a pass names the project manager of the new project")
        if self.outcome != "pass" and self.project_manager_id is not None:
            raise ValueError("only a pass names a project manager")
        return self


class CriterionOut(BaseModel):
    """One criterion and its result: an automatic check's result now, or a person's tick (null
    until a decision records one)."""

    id: str
    text: str
    kind: Literal["check", "person"]
    check: str | None
    met: bool | None
    detail: str | None = None
    note: str | None = None


class GateInfo(BaseModel):
    id: str
    name: str
    from_stage: str
    to_stage: str
    applies_to: str
    deciders: list[str]


class GateDecisionOut(BaseModel):
    id: int
    gate_id: str
    outcome: str
    decided_by: int
    decided_by_name: str | None
    reason: str
    criteria: list[CriterionOut]
    policy_version: str
    created_at: datetime


class GateOutBase(BaseModel):
    gate: GateInfo
    criteria: list[CriterionOut]
    checks_met: bool  # every automatic check is met now
    policy_version: str
    latest_decision: GateDecisionOut | None


class GateOut(GateOutBase):
    request_id: int
    request_version: int
    request_status: str
    requested_by: int
    project_id: int | None  # the project a pass made


class BaselineOut(BaseModel):
    """A frozen copy of the plan; `plan` keeps money as 2-decimal strings."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    number: int
    gate_decision_id: int | None
    change_request_id: int | None
    plan: dict[str, Any]
    total: MoneyStr
    created_at: datetime


class ProjectGateOut(GateOutBase):
    project_id: int
    project_version: int
    project_stage: str
    project_status: str
    pm_user_id: int | None
    baseline: BaselineOut | None  # the latest baseline, once there is one
