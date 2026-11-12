"""API shapes of a project's delivery progress (item 7.2).

Every input refuses unknown fields. A milestone or deliverable write carries the counter the
client read (`reached_version`, `status_version`), sent as `version`; a stale one is a 409. A
spend entry is append-only: a correction names the entry it corrects in `supersedes_id`, which
must be the project's newest entry. Money is a JSON string with 2 decimal places.
"""

from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.delivery import NOTE_MAX
from app.schemas.common import MoneyStr, Page
from app.schemas.plan import Description, Total, Version

# Every status a deliverable can have. A route here accepts the name `accepted` only to refuse it
# (audited): only the acceptance step sets it.
DeliverableStatus = Literal["not_started", "in_progress", "ready_for_acceptance", "accepted"]
SpendNote = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NOTE_MAX)
]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MilestoneReachedIn(_Input):
    """Mark a milestone reached on `reached_on` (with an optional note), or clear it with
    `reached_on: null`."""

    version: Version
    reached_on: date | None
    reached_note: Description | None = None

    @model_validator(mode="after")
    def _note_needs_a_date(self) -> Self:
        if self.reached_on is None and self.reached_note is not None:
            raise ValueError("a note goes with a reached date; clear both together")
        return self


class DeliverableStatusIn(_Input):
    version: Version
    status: DeliverableStatus


class SpendIn(_Input):
    amount: Annotated[MoneyStr, Field(ge=0)]
    as_of: date
    note: SpendNote
    supersedes_id: Annotated[int, Field(ge=1)] | None = None


class MilestoneProgressOut(BaseModel):
    id: int
    name: str
    # The target in the current baseline; None for a milestone the baseline doesn't have.
    target_date: date | None
    # The plan's date now, which may have moved by an approved change.
    plan_date: date
    reached_on: date | None
    reached_note: str | None
    # Working days against the baseline target: above 0 is late, below 0 early, 0 on time.
    # For a milestone not yet reached it counts to today and is never below 0.
    days_late: int | None
    version: int


class DeliverableProgressOut(BaseModel):
    id: int
    name: str
    milestone_id: int | None
    status: DeliverableStatus
    version: int


class SpendOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    amount: MoneyStr
    as_of: date
    note: str
    supersedes_id: int | None
    created_by: int
    created_by_name: str | None = None
    created_at: datetime


class DeliveryOut(BaseModel):
    project_id: int
    project_stage: str
    project_status: str
    baseline_number: int | None
    baseline_total: Total | None
    today: date
    milestones: list[MilestoneProgressOut]
    deliverables: list[DeliverableProgressOut]
    # The newest entry, which is the current figure; None until one is recorded.
    current_spend: SpendOut | None
    # Every entry, newest first and paged.
    spend: Page[SpendOut]
