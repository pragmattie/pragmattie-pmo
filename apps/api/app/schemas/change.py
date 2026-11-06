"""API shapes of a change request (item 5.5).

Every input refuses unknown fields, so a client can never send the effect or the decider: code
computes both from the baseline. A change set names the baseline's items by id. Money is a JSON
string with 2 decimal places.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.change import TITLE_MAX
from app.models.gate import MIN_REASON
from app.models.intake import LONG_TEXT_MAX
from app.schemas.plan import Amount, Category, Contingency, Description, LongText, Name, Version

MAX_ITEMS = 50

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX)]
Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LONG_TEXT_MAX)
]
DecisionReason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=MIN_REASON, max_length=LONG_TEXT_MAX)
]
Ids = Annotated[list[Annotated[int, Field(ge=1)]], Field(max_length=MAX_ITEMS)]
ChangeStatus = Literal["draft", "submitted", "approved", "rejected", "withdrawn"]
Decider = Literal["project_manager", "sponsor"]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeliverableAdd(_Input):
    name: Name
    description: Description | None = None
    milestone_id: int | None = None


class MilestoneMove(_Input):
    milestone_id: int
    target_date: date


class LineAdd(_Input):
    description: Description
    category: Category
    amount: Amount


class LineChange(_Input):
    line_id: int
    description: Description | None = None
    category: Category | None = None
    amount: Amount | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if self.description is None and self.category is None and self.amount is None:
            raise ValueError("a changed line needs a new description, category or amount")
        return self


def _unique(ids: list[int], what: str) -> None:
    if len(set(ids)) != len(ids):
        raise ValueError(f"{what} must not name the same item twice")


class ChangeSet(_Input):
    """What a change request changes. Anything left out stays as the baseline has it."""

    scope_in: LongText | None = None
    scope_out: LongText | None = None
    add_deliverables: Annotated[list[DeliverableAdd], Field(max_length=MAX_ITEMS)] = []
    remove_deliverables: Ids = []
    move_milestones: Annotated[list[MilestoneMove], Field(max_length=MAX_ITEMS)] = []
    add_lines: Annotated[list[LineAdd], Field(max_length=MAX_ITEMS)] = []
    remove_lines: Ids = []
    change_lines: Annotated[list[LineChange], Field(max_length=MAX_ITEMS)] = []
    contingency_pct: Contingency | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not any(
            (
                self.scope_in is not None,
                self.scope_out is not None,
                self.add_deliverables,
                self.remove_deliverables,
                self.move_milestones,
                self.add_lines,
                self.remove_lines,
                self.change_lines,
                self.contingency_pct is not None,
            )
        ):
            raise ValueError("a change request must change something")
        _unique(self.remove_deliverables, "remove_deliverables")
        _unique([m.milestone_id for m in self.move_milestones], "move_milestones")
        _unique(self.remove_lines, "remove_lines")
        changed = [c.line_id for c in self.change_lines]
        _unique(changed, "change_lines")
        if set(changed) & set(self.remove_lines):
            raise ValueError("a line can't be both removed and changed")
        return self


class ChangeCreate(_Input):
    title: Title
    reason: Reason
    changes: ChangeSet


class ChangeUpdate(_Input):
    version: Version
    title: Title | None = None
    reason: Reason | None = None
    changes: ChangeSet | None = None


class ChangeWrite(_Input):
    version: Version


class ChangeDecisionIn(_Input):
    version: Version
    outcome: Literal["approve", "reject"]
    reason: DecisionReason


class ChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    reason: str
    changes: dict[str, Any]
    effect: dict[str, Any]
    decider: Decider
    measured_baseline_number: int
    status: ChangeStatus
    raised_by: int
    raised_by_name: str | None = None
    submitted_at: datetime | None
    decided_by: int | None
    decided_by_name: str | None = None
    decision_reason: str | None
    decided_at: datetime | None
    baseline_number: int | None = None
    version: int
    created_at: datetime


class ChangesPage(BaseModel):
    """The change log, newest first, with what the viewer needs to raise or decide."""

    items: list[ChangeOut]
    total: int
    limit: int
    offset: int
    project_stage: str
    project_status: str
    pm_user_id: int | None
    # The roles that decide a sponsor-level change: the go-ahead gate's deciders.
    sponsor_deciders: list[str]
    current_baseline_number: int | None
    # A client proposal whose checker flags it against the current plan, e.g. after a change.
    proposal_out_of_date: bool
