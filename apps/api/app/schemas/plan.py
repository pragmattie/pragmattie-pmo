"""API shapes of a project's plan and its milestones, deliverables and estimate lines (item 5.1).

Every input refuses unknown fields, so a client can never send a total: it is computed. Edits
carry the `version` the client read; a stale one is a 409. A PATCH changes only the fields it
sends; sending `null` clears an optional field. Money is a JSON string with 2 decimal places.
"""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

from app.models.intake import LONG_TEXT_MAX
from app.models.plan import CONTINGENCY_MAX, CONTRIBUTORS_MAX, DESCRIPTION_MAX, NAME_MAX
from app.schemas.audit import CostStr
from app.schemas.common import MoneyStr

LongText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LONG_TEXT_MAX)
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX)]
Description = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=DESCRIPTION_MAX)
]
Contributors = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=CONTRIBUTORS_MAX)
]
Amount = Annotated[MoneyStr, Field(gt=0)]
Contingency = Annotated[int, Field(ge=0, le=CONTINGENCY_MAX, strict=True)]
DueDay = Annotated[int, Field(ge=1, le=28, strict=True)]
ReportPeriod = Literal["weekly", "fortnightly", "monthly"]
Category = Literal["people", "software", "services", "other"]
BudgetFit = Literal["within", "below", "above", "no_range"]
Version = Annotated[int, Field(ge=1)]
# Who wrote an item: the planner agent, or a person (who added or edited it).
Source = Literal["agent", "person"]
# A computed sum can pass the 12 digits a stored amount has, so the total is not capped.
Total = Annotated[
    Decimal,
    PlainSerializer(
        lambda value: str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        return_type=str,
        when_used="json",
    ),
]


def _required_stay_set(model: BaseModel, names: tuple[str, ...]) -> None:
    for name in names:
        if name in model.model_fields_set and getattr(model, name) is None:
            raise ValueError(f"{name} can't be cleared")


class PlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Version
    scope_in: LongText | None = None
    scope_out: LongText | None = None
    contingency_pct: Contingency | None = None
    report_period: ReportPeriod | None = None
    report_due_day: DueDay | None = None

    @model_validator(mode="after")
    def _contingency_stays_set(self) -> Self:
        _required_stay_set(self, ("contingency_pct",))
        return self


class PlanOut(BaseModel):
    id: int
    project_id: int
    scope_in: str | None
    scope_out: str | None
    contingency_pct: int
    report_period: str | None
    report_due_day: int | None
    lines_total: Total
    contingency_amount: Total
    total: Total
    budget_low: MoneyStr | None
    budget_high: MoneyStr | None
    budget_fit: BudgetFit
    missing: list[str]
    editable: bool
    version: int
    updated_at: datetime


class PlanWrite(BaseModel):
    """What every write to a plan's lists carries: the version of the item it changes, or of
    the plan for an addition."""

    model_config = ConfigDict(extra="forbid")

    version: Version


class MilestoneCreate(PlanWrite):
    name: Name
    target_date: date
    owner_user_id: int | None = None
    description: Description | None = None
    contributors: Contributors | None = None


class MilestoneUpdate(PlanWrite):
    name: Name | None = None
    target_date: date | None = None
    owner_user_id: int | None = None
    description: Description | None = None
    contributors: Contributors | None = None

    @model_validator(mode="after")
    def _required(self) -> Self:
        _required_stay_set(self, ("name", "target_date"))
        return self


class MilestoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    name: str
    target_date: date
    owner_user_id: int | None
    owner_name: str | None = None
    owner_active: bool = False
    description: str | None
    contributors: str | None
    source: Source
    version: int


class DeliverableCreate(PlanWrite):
    name: Name
    description: Description | None = None
    milestone_id: int | None = None


class DeliverableUpdate(PlanWrite):
    name: Name | None = None
    description: Description | None = None
    milestone_id: int | None = None

    @model_validator(mode="after")
    def _required(self) -> Self:
        _required_stay_set(self, ("name",))
        return self


class DeliverableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    name: str
    description: str | None
    milestone_id: int | None
    milestone_name: str | None = None
    source: Source
    version: int


class EstimateLineCreate(PlanWrite):
    description: Description
    category: Category
    amount: Amount


class EstimateLineUpdate(PlanWrite):
    description: Description | None = None
    category: Category | None = None
    amount: Amount | None = None

    @model_validator(mode="after")
    def _required(self) -> Self:
        _required_stay_set(self, ("description", "category", "amount"))
        return self


class EstimateLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    description: str
    category: str
    amount: MoneyStr
    source: Source
    version: int


# The kinds of plan item a person can confirm, as `POST /projects/{id}/plan/confirm` names them.
ItemKind = Literal["milestone", "deliverable", "estimate_line"]


class PlanConfirm(PlanWrite):
    """Confirm the planner agent's items as a person's, unchanged (item 6.6).

    With `kind` and `id`, one item: `version` is that item's, as for an edit. With neither, every
    agent item in the plan: `version` is the plan's, as for an addition."""

    kind: ItemKind | None = None
    id: int | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> Self:
        if (self.kind is None) != (self.id is None):
            raise ValueError("send kind and id together, or neither to confirm every agent item")
        return self


class PlanDraftDropped(BaseModel):
    part: str
    reason: str


class PlanDraftChanged(BaseModel):
    part: str
    what: str
    # "from" is a keyword, so the field is `before` and serialised as "from".
    before: str = Field(alias="from", serialization_alias="from")
    after: str = Field(alias="to", serialization_alias="to")


class PlanDraftResult(BaseModel):
    """The answer of `POST /projects/{id}/plan/draft` (item 6.5).

    A dry run has `request` (the exact request that would be sent) and `cost_ceiling_usd` (the
    most it can cost, rounded up to the cent), and nothing is called or saved. A real run has the
    draft as code kept it (`draft`), what code dropped or changed, what it saved, removed and
    kept (`saved`; in `shadow` what it would), its tokens and cost and its audit row."""

    mode: Literal["shadow", "enforce"]
    dry_run: bool
    model: str
    prompt_version: str
    request: dict[str, Any] | None = None
    cost_ceiling_usd: MoneyStr | None = None
    draft: dict[str, Any] | None = None
    dropped: list[PlanDraftDropped] = []
    changed: list[PlanDraftChanged] = []
    saved: dict[str, Any] | None = None
    plan_version: int | None = None
    audit_id: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: CostStr | None = None
