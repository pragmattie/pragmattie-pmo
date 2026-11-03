"""API shapes of project requests.

Every input refuses unknown fields, so a client that sends `requested_by`, `status`,
`submitted_at` or anything else the server owns gets a 422 rather than having it silently dropped.
`RequestFields` holds the rules a whole request must meet; a PATCH is checked by merging it into
the stored request and validating the result as `RequestFields`.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.intake import LONG_TEXT_MAX, TITLE_MAX
from app.schemas.common import MoneyStr

RequestStatus = Literal["draft", "submitted", "approved", "declined", "withdrawn"]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX)]
LongText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LONG_TEXT_MAX)
]
Budget = Annotated[MoneyStr, Field(ge=0)]


class RequestFields(BaseModel):
    """Everything a project manager supplies about a request."""

    model_config = ConfigDict(extra="forbid")

    title: Title
    client_id: int | None = None
    asked_for: LongText
    problem: LongText
    outcome: LongText
    budget_low: Budget | None = None
    budget_high: Budget | None = None
    wanted_by: date | None = None
    uses_ai: bool = False
    known_risks: LongText | None = None

    @model_validator(mode="after")
    def _budget_in_order(self) -> Self:
        low, high = self.budget_low, self.budget_high
        if low is not None and high is not None and low > high:
            raise ValueError("budget_low must not be above budget_high")
        return self


class RequestCreate(RequestFields):
    pass


class RequestUpdate(BaseModel):
    """A partial change to a draft. `version` is the one the client read; a stale one is a 409."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    title: Title | None = None
    client_id: int | None = None
    asked_for: LongText | None = None
    problem: LongText | None = None
    outcome: LongText | None = None
    budget_low: Budget | None = None
    budget_high: Budget | None = None
    wanted_by: date | None = None
    uses_ai: bool | None = None
    known_risks: LongText | None = None


class RequestTransition(BaseModel):
    """Submitting or withdrawing. `version` is the one the client read; a stale one is a 409."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    client_id: int | None
    client_name: str | None = None
    asked_for: str
    problem: str
    outcome: str
    budget_low: MoneyStr | None
    budget_high: MoneyStr | None
    wanted_by: date | None
    uses_ai: bool
    known_risks: str | None
    requested_by: int
    requested_by_name: str | None = None
    status: str
    submitted_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class RequestHistoryEntry(BaseModel):
    """One change to a request, from its audit row: the values before (none on a create) and
    after, and who made it (their display name, never their email)."""

    id: int
    version: int
    action: str
    actor_user_id: int | None
    actor_name: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime
