"""API shapes of risks.

Every input refuses unknown fields, so a client that sends `score`, `band`, `source`, `status` on
create, or anything else the server owns, gets a 422 rather than having it silently dropped.
`RiskFields` holds the rules a whole risk must meet; a PATCH is checked by merging it into the
stored risk and validating the result as `RiskFields`.
"""

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.risk import RESPONSES_BY_KIND

Kind = Literal["threat", "opportunity"]
Category = Literal["ai", "project"]
Objective = Literal["schedule", "cost", "quality"]
Response = Literal["avoid", "reduce", "share", "accept", "pursue", "increase"]
Level = Annotated[int, Field(ge=1, le=5, strict=True)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
# On these routes a person may only open or close a risk; proposing, accepting and rejecting
# arrive with item 3.4.
EditableStatus = Literal["open", "closed"]


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
    status: EditableStatus | None = None


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
    source: str
    version: int
    created_at: datetime
    updated_at: datetime


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
    band: str
    count: int


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
