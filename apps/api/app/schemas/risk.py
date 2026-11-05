"""API shapes of risks.

Every input refuses unknown fields, so a client that sends `score`, `band`, `source`, `status` on
create, or anything else the server owns, gets a 422 rather than having it silently dropped.
`RiskFields` holds the rules a whole risk must meet; a PATCH is checked by merging it into the
stored risk and validating the result as `RiskFields`.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.risk import RESPONSES_BY_KIND
from app.schemas.audit import CostStr
from app.schemas.common import MoneyStr

Kind = Literal["threat", "opportunity"]
Category = Literal["ai", "project"]
Objective = Literal["schedule", "cost", "quality"]
Response = Literal["avoid", "reduce", "share", "accept", "pursue", "increase"]
Level = Annotated[int, Field(ge=1, le=5, strict=True)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
# A PATCH may only move a risk between open and closed (or close an accepted one). Opening a
# proposed risk, rejecting it and accepting a risk have their own routes.
EditableStatus = Literal["open", "closed"]
# Accepting a risk needs a written reason of at least this many characters.
MIN_ACCEPT_REASON = 20
AcceptReason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=MIN_ACCEPT_REASON, max_length=4000)
]


class RiskFields(BaseModel):
    """Everything a person supplies about a risk, with the rules that join the fields up."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    title: Title
    cause: LongText
    effect: LongText
    category: Category
    objectives: list[Objective] = Field(min_length=1, max_length=3)
    probability: Level | None = None
    impact: Level | None = None
    response: Response
    response_plan: LongText
    owner_user_id: int | None = None
    needs_judgement: bool = False
    show_in_proposal: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.objectives)) != len(self.objectives):
            raise ValueError("objectives must not repeat")
        if self.response not in RESPONSES_BY_KIND[self.kind]:
            allowed = ", ".join(RESPONSES_BY_KIND[self.kind])
            raise ValueError(f"a {self.kind}'s response must be one of: {allowed}")
        if (self.probability is None) != (self.impact is None):
            raise ValueError("set probability and impact together")
        # Only a risk that needs a person's judgement may wait for a score.
        if self.probability is None and not self.needs_judgement:
            raise ValueError("probability and impact are required unless needs_judgement is set")
        return self


class RiskCreate(RiskFields):
    pass


class RiskUpdate(BaseModel):
    """A partial change. `version` is the one the client read; a stale one is a 409."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    kind: Kind | None = None
    title: Title | None = None
    cause: LongText | None = None
    effect: LongText | None = None
    category: Category | None = None
    objectives: list[Objective] | None = None
    probability: Level | None = None
    impact: Level | None = None
    response: Response | None = None
    response_plan: LongText | None = None
    owner_user_id: int | None = None
    needs_judgement: bool | None = None
    show_in_proposal: bool | None = None
    status: EditableStatus | None = None


class RiskDecision(BaseModel):
    """Opening a proposed risk. `version` is the one the client read; a stale one is a 409."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class RiskReject(RiskDecision):
    """Rejecting a proposed risk, with the reason why."""

    reason: LongText


class RiskAccept(RiskDecision):
    """Accepting a risk: why, and when it will next be looked at (today or later)."""

    reason: AcceptReason
    review_date: date

    @model_validator(mode="after")
    def _not_past(self) -> Self:
        if self.review_date < date.today():
            raise ValueError("review_date must be today or later")
        return self


class RiskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    kind: str
    title: str
    cause: str
    effect: str
    category: str
    objectives: list[str]
    probability: int | None
    impact: int | None
    score: int | None
    band: str | None
    response: str
    response_plan: str
    owner_user_id: int | None
    status: str
    needs_judgement: bool
    show_in_proposal: bool
    source: str
    escalated_to: str | None
    escalated_band: str | None
    review_date: date | None
    version: int
    created_at: datetime
    updated_at: datetime


class RiskEscalationOut(RiskOut):
    """An escalated risk in the caller's list, with its project's name."""

    project_name: str = ""


TemplateId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
# More than the library holds, and small enough to keep one request quick.
MAX_TEMPLATES_PER_REQUEST = 50


class RiskTemplateOut(BaseModel):
    """A starter risk from the library: no probability or impact, which depend on the project."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    category: str
    title: str
    cause: str
    effect: str
    objectives: list[str]
    response: str
    response_plan: str


class FromLibrary(BaseModel):
    """The library templates to add to a project, each at most once."""

    model_config = ConfigDict(extra="forbid")

    template_ids: list[TemplateId] = Field(min_length=1, max_length=MAX_TEMPLATES_PER_REQUEST)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.template_ids)) != len(self.template_ids):
            raise ValueError("template_ids must not repeat")
        return self


class HeatCell(BaseModel):
    """One probability x impact cell of the heat map: how many open risks sit in it."""

    probability: int
    impact: int
    band: str
    count: int


class BandTotal(BaseModel):
    """Open risks in one band, and the role the band escalates to (who may accept it)."""

    band: str
    count: int
    owner_role: str


class RiskHeatmap(BaseModel):
    """Open risks by probability and impact, every cell present (25), plus totals per band in the
    policy's order (lowest first). `unscored` counts open risks still waiting for a person's
    judgement, which have no cell."""

    kind: str | None
    cells: list[HeatCell]
    bands: list[BandTotal]
    unscored: int


class RiskHistoryEntry(BaseModel):
    """One change to a risk, from its audit row: the values before (none on a create) and after."""

    id: int
    version: int
    action: str
    actor_user_id: int | None
    actor_name: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: datetime


class RiskProposalOut(BaseModel):
    """One risk the risk agent suggests. An ethical or reputational one has no probability or
    impact and `needs_judgement` set: a person scores it."""

    model_config = ConfigDict(from_attributes=True)

    kind: str
    category: str
    title: str
    cause: str
    effect: str
    objectives: list[str]
    probability: int | None
    impact: int | None
    response: str
    judgement: str
    needs_judgement: bool
    reason: str


class DroppedProposal(BaseModel):
    """A suggestion code dropped: its place in the model's list, its title if any, and why."""

    index: int
    title: str | None
    reason: str


class AdjustedProposal(BaseModel):
    """A change code made to a kept suggestion, such as a clamped probability."""

    index: int
    change: str


class RiskSuggestion(BaseModel):
    """The answer of `POST .../risks/suggest`.

    A dry run has `request` (the exact request that would be sent) and `cost_ceiling_usd` (the
    most it can cost, rounded up to the cent), and nothing else is called or saved. A real run has
    the proposals, what code dropped or adjusted, its tokens and cost, and its audit row; `saved`
    holds the proposed risks when the mode is `enforce` and is empty in `shadow`."""

    mode: Literal["shadow", "enforce"]
    dry_run: bool
    model: str
    prompt_version: str
    request: dict[str, Any] | None = None
    cost_ceiling_usd: MoneyStr | None = None
    proposals: list[RiskProposalOut] = []
    dropped: list[DroppedProposal] = []
    adjusted: list[AdjustedProposal] = []
    saved: list[RiskOut] = []
    audit_id: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: CostStr | None = None
