"""API shapes of project requests.

Every input refuses unknown fields, so a client that sends `requested_by`, `status`,
`submitted_at` or anything else the server owns gets a 422 rather than having it silently dropped.
`RequestFields` holds the rules a whole request must meet; a PATCH is checked by merging it into
the stored request and validating the result as `RequestFields`.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.intake import (
    ACTION_MAX,
    ACTIONS_MAX,
    BENEFITS_MAX,
    LONG_TEXT_MAX,
    MEASURE_MAX,
    TARGET_MAX,
    TITLE_MAX,
)
from app.schemas.audit import CostStr
from app.schemas.common import MoneyStr

RequestStatus = Literal["draft", "submitted", "approved", "declined", "withdrawn"]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX)]
LongText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LONG_TEXT_MAX)
]
Budget = Annotated[MoneyStr, Field(ge=0)]
Measure = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MEASURE_MAX)
]
ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TARGET_MAX)
]
ActionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=ACTION_MAX)
]
Rating = Literal["green", "yellow", "red"]


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


class BenefitIn(BaseModel):
    """One benefit. Only the description is needed to save it; a request can't be submitted until
    at least one benefit says how it is measured and what the target is."""

    model_config = ConfigDict(extra="forbid")

    description: LongText
    measure: Measure | None = None
    current_value: ShortText | None = None
    target: ShortText | None = None
    target_date: date | None = None


class BenefitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    description: str
    measure: str | None
    current_value: str | None
    target: str | None
    target_date: date | None


class ValueCaseIn(BaseModel):
    """The whole value case, replacing what is stored. `version` is the request's version the
    client read; a stale one is a 409. `source` and confirmation are the server's to set."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    benefits: list[BenefitIn] = Field(min_length=1, max_length=BENEFITS_MAX)
    cost_low: Budget | None = None
    cost_high: Budget | None = None
    cost_method: LongText | None = None
    strategic_fit: LongText
    cost_of_not_doing: LongText | None = None

    @model_validator(mode="after")
    def _cost_in_order(self) -> Self:
        low, high = self.cost_low, self.cost_high
        if low is not None and high is not None and low > high:
            raise ValueError("cost_low must not be above cost_high")
        return self


class ValueCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: int
    # The request's version after this value case was last saved; send it with the next change.
    request_version: int
    benefits: list[BenefitOut]
    cost_low: MoneyStr | None
    cost_high: MoneyStr | None
    cost_method: str | None
    strategic_fit: str
    cost_of_not_doing: str | None
    source: Literal["person", "agent"]
    confirmed_by: int | None
    confirmed_by_name: str | None = None
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ReadinessActionIn(BaseModel):
    """One thing to do before an area is ready. The owner must be an active user (else 422); a
    due date in the past is allowed, so an older action never blocks a later save."""

    model_config = ConfigDict(extra="forbid")

    description: ActionText
    owner_id: int
    due_date: date


class ReadinessAreaIn(BaseModel):
    """One area's rating and note. Any area may have actions; a yellow one needs at least one."""

    model_config = ConfigDict(extra="forbid")

    rating: Rating
    note: LongText
    actions: list[ReadinessActionIn] = Field(max_length=ACTIONS_MAX)

    @model_validator(mode="after")
    def _yellow_has_an_action(self) -> Self:
        if self.rating == "yellow" and not self.actions:
            raise ValueError("a yellow area needs at least one action with an owner and a date")
        return self


class ReadinessIn(BaseModel):
    """The whole readiness check, replacing what is stored. Every area and field is required.
    `version` is the request's version the client read; a stale one is a 409. `source` and
    confirmation are the server's to set."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    problem: ReadinessAreaIn
    approach: ReadinessAreaIn
    data: ReadinessAreaIn


class ReadinessActionOut(BaseModel):
    """An action. Owner and date are empty only on an agent's draft, until a person adds them."""

    description: str
    owner_id: int | None
    owner_name: str | None
    due_date: date | None


class ReadinessAreaOut(BaseModel):
    # Empty only on an agent's draft that left the area unrated.
    rating: Rating | None
    note: str | None
    actions: list[ReadinessActionOut]


class ReadinessOut(BaseModel):
    request_id: int
    # The request's version after this check was last saved; send it with the next change.
    request_version: int
    problem: ReadinessAreaOut
    approach: ReadinessAreaOut
    data: ReadinessAreaOut
    # The worst area; `unrated` until a person has confirmed it and every area is rated.
    overall: Literal["green", "yellow", "red", "unrated"]
    source: Literal["person", "agent"]
    confirmed_by: int | None
    confirmed_by_name: str | None = None
    confirmed_at: datetime | None
    created_at: datetime
    updated_at: datetime


Concern = Literal["none", "ethical", "legal", "reputational"]


class DraftValueCase(BaseModel):
    """The value case the intake agent proposed, after code checked it."""

    benefits: list[BenefitOut]
    cost_low: MoneyStr | None
    cost_high: MoneyStr | None
    cost_method: str | None
    strategic_fit: str
    cost_of_not_doing: str | None
    concern: Concern


class DraftArea(BaseModel):
    # Empty when code dropped the area. Never green: a person rates an area green.
    rating: Literal["yellow", "red"] | None
    note: str | None
    concern: Concern
    # What to do; a person adds each action's owner and date.
    actions: list[str]


class DraftReadiness(BaseModel):
    problem: DraftArea
    approach: DraftArea
    data: DraftArea


class DraftChange(BaseModel):
    part: str
    reason: str


class DraftAdjustment(BaseModel):
    part: str
    change: str


class DraftSkipped(BaseModel):
    section: Literal["value_case", "readiness"]
    reason: str


class DraftResult(BaseModel):
    """The answer of `POST /requests/{id}/draft`.

    A dry run has `request` (the exact request that would be sent) and `cost_ceiling_usd` (the
    most it can cost, rounded up to the cent), and nothing is called or saved. A real run has the
    proposal, what code dropped or changed, its tokens and cost and its audit row. In `enforce`,
    `saved` names the sections saved as unconfirmed agent drafts and `skipped` the ones a person
    has confirmed, which the agent leaves alone; in `shadow` both are empty."""

    mode: Literal["shadow", "enforce"]
    dry_run: bool
    model: str
    prompt_version: str
    request: dict[str, Any] | None = None
    cost_ceiling_usd: MoneyStr | None = None
    value_case: DraftValueCase | None = None
    readiness: DraftReadiness | None = None
    dropped: list[DraftChange] = []
    adjusted: list[DraftAdjustment] = []
    saved: list[Literal["value_case", "readiness"]] = []
    skipped: list[DraftSkipped] = []
    request_version: int | None = None
    audit_id: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: CostStr | None = None
