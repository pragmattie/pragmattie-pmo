"""API shapes of a client project's proposal (item 5.2).

Every input refuses unknown fields, so a client can never send a price, a milestone date, a
deliverable or a risk: code copies them from the plan. Each write carries the proposal's
`version`; a stale one is a 409. Money is a JSON string with 2 decimal places. A risk appears as
its title and response only, never its score or band.

Each text section has a state (item 5.3): who wrote it (`agent`, `person`, or null for one prefilled
from the plan or empty) and, for an agent's draft, who reviewed it and when. `unreviewed` is true
for an agent's draft no person has edited or confirmed; while any is, submit is refused.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.intake import LONG_TEXT_MAX
from app.models.proposal import SHORT_TEXT_MAX
from app.schemas.audit import CostStr
from app.schemas.common import MoneyStr

# A decision on a proposal needs a written reason of at least this many characters.
MIN_REASON = 20

LongText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LONG_TEXT_MAX)
]
ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SHORT_TEXT_MAX)
]
Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=MIN_REASON, max_length=LONG_TEXT_MAX)
]
Version = Annotated[int, Field(ge=1)]
Status = Literal["draft", "submitted", "approved", "sent", "accepted", "declined", "withdrawn"]
Section = Literal["summary", "scope_in", "scope_out", "assumptions", "terms"]


def _not_future(day: date, name: str) -> None:
    if day > date.today():
        raise ValueError(f"{name} can't be in the future")


class ProposalWrite(BaseModel):
    """A write that carries only the version the client read (refresh, submit)."""

    model_config = ConfigDict(extra="forbid")

    version: Version


class ProposalUpdate(ProposalWrite):
    """The text sections the project manager writes. `null` clears one."""

    summary: LongText | None = None
    scope_in: LongText | None = None
    scope_out: LongText | None = None
    assumptions: LongText | None = None
    terms: LongText | None = None


class ProposalDecision(ProposalWrite):
    """The sponsor's approval or return, or a withdrawal, with the reason why."""

    reason: Reason


class ProposalSent(ProposalWrite):
    """What a person did outside the product: to whom, on what date and by what means."""

    sent_to: ShortText
    sent_on: date
    means: ShortText

    @model_validator(mode="after")
    def _past(self) -> Self:
        _not_future(self.sent_on, "sent_on")
        return self


class ProposalAnswer(ProposalWrite):
    """The client's answer, the day it came and an optional note."""

    answer: Literal["accepted", "declined"]
    answered_on: date
    note: LongText | None = None

    @model_validator(mode="after")
    def _past(self) -> Self:
        _not_future(self.answered_on, "answered_on")
        return self


class FlagOut(BaseModel):
    rule: str
    section: str
    text: str


class EstimateLineCopy(BaseModel):
    description: str
    category: str
    amount: MoneyStr


class MilestoneCopy(BaseModel):
    name: str
    target_date: date
    description: str | None = None


class DeliverableCopy(BaseModel):
    name: str
    description: str | None = None
    milestone: str | None = None


class RiskCopy(BaseModel):
    """A risk the client should know about: its title and response, never its score or band."""

    model_config = ConfigDict(extra="ignore")

    risk_id: int
    title: str
    response: str
    response_plan: str


class SectionState(BaseModel):
    source: Literal["agent", "person"] | None = None
    reviewed_by: int | None = None
    reviewed_at: datetime | None = None
    unreviewed: bool = False


class ProposalOut(BaseModel):
    id: int
    project_id: int
    project_name: str
    client_name: str
    status: Status
    summary: str | None
    scope_in: str | None
    scope_out: str | None
    assumptions: str | None
    terms: str | None
    price: MoneyStr
    estimate_lines: list[EstimateLineCopy]
    milestones: list[MilestoneCopy]
    deliverables: list[DeliverableCopy]
    risks: list[RiskCopy]
    plan_version_seen: int
    plan_changed: bool
    flags: list[FlagOut]
    sections: dict[Section, SectionState]
    unreviewed_sections: list[Section]
    # The number of the latest frozen copy, who submitted it, and the number of the copy the
    # sponsor last approved. `approval_current` is false once the proposal changed after it.
    latest_version: int | None
    submitted_by: int | None
    submitted_by_name: str | None
    approved_version: int | None
    approval_current: bool
    version: int
    updated_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    actor_user_id: int
    actor_name: str | None = None
    reason: str | None
    sent_to: str | None
    sent_on: date | None
    means: str | None
    answer: str | None
    answered_on: date | None
    note: str | None
    created_at: datetime


class VersionOut(BaseModel):
    id: int
    number: int
    content: dict[str, Any]
    submitted_by: int
    submitted_by_name: str | None
    submitted_at: datetime
    events: list[EventOut]


class DraftRemoved(BaseModel):
    """Something code removed from the agent's text: a money figure, a date or an unknown token."""

    section: Section
    kind: Literal["money", "date", "token"]
    text: str


class DraftFilled(BaseModel):
    """A token code replaced with the plan's value."""

    section: Section
    token: str
    value: str


class DraftDropped(BaseModel):
    part: str
    reason: str


class DraftKept(BaseModel):
    section: Section
    reason: str


class ProposalDraftResult(BaseModel):
    """The answer of `POST /projects/{id}/proposal/draft`.

    A dry run has `request` (the exact request that would be sent) and `cost_ceiling_usd` (the
    most it can cost, rounded up to the cent), and nothing is called or saved. A real run has the
    drafted sections with the plan's values filled in, what code removed, filled in or dropped,
    its tokens and cost and its audit row, and the checker's flags on the proposal as it is (in
    `enforce`) or as it would be (in `shadow`). `replaced` names the sections saved as unreviewed
    agent drafts (empty in `shadow`); `kept` the ones a person edited or confirmed, which the
    agent leaves alone."""

    mode: Literal["shadow", "enforce"]
    dry_run: bool
    model: str
    prompt_version: str
    request: dict[str, Any] | None = None
    cost_ceiling_usd: MoneyStr | None = None
    sections: dict[Section, str] = {}
    removed: list[DraftRemoved] = []
    filled: list[DraftFilled] = []
    dropped: list[DraftDropped] = []
    replaced: list[Section] = []
    kept: list[DraftKept] = []
    flags: list[FlagOut] = []
    proposal_version: int | None = None
    audit_id: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: CostStr | None = None
