"""API shapes of status reports (item 7.3).

Every input refuses unknown fields. Figures and ratings are worked out by code and never taken
from a client; a client writes only the text sections. Every write on a report carries the
version the client read; a stale one is a 409.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.models.report import REASON_MAX, SECTION_MAX
from app.schemas.audit import CostStr
from app.schemas.common import MoneyStr
from app.schemas.plan import Version

ReportStatus = Literal["draft", "published", "withdrawn"]
Rating = Literal["green", "amber", "red"]
SectionText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=SECTION_MAX)]
Reason = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=REASON_MAX)
]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportCreate(_Input):
    """A draft for the period ending on `period_end`. A correction of a published report names
    it in `replaces_id`, with the reason the old one is withdrawn when the correction is
    published."""

    period_end: date
    replaces_id: int | None = None
    reason: Reason | None = None


class Sections(_Input):
    summary: SectionText | None = None
    done: SectionText | None = None
    next: SectionText | None = None
    issues: SectionText | None = None
    decisions: SectionText | None = None


class ReportTextIn(_Input):
    """Edit the text: only the sections sent change."""

    version: Version
    sections: Sections


class ReportVersionIn(_Input):
    version: Version


class ReportWithdrawIn(_Input):
    version: Version
    reason: Reason


class SourceOut(BaseModel):
    table: str
    id: int
    field: str


class FigureOut(BaseModel):
    key: str
    label: str
    # money, percent and date values are strings; counts, days and numbers are whole numbers.
    kind: Literal["money", "percent", "date", "count", "days", "number"]
    value: Any
    note: str
    sources: list[SourceOut]


class RatingOut(BaseModel):
    rating: Rating
    measure: str | None
    rule: str
    text: str


class FlagOut(BaseModel):
    rule: str
    section: str
    text: str


class SectionState(BaseModel):
    """Who wrote a section (item 7.4): the status report agent or a person, and who reviewed an
    agent's draft. `unreviewed` is an agent's draft no person has edited or confirmed yet."""

    source: Literal["agent", "person"] | None = None
    reviewed_by: int | None = None
    reviewed_at: datetime | None = None
    unreviewed: bool = False


class ReportSummaryOut(BaseModel):
    id: int
    status: ReportStatus
    overall: Rating
    replaces_id: int | None
    published_at: datetime | None
    withdrawn_at: datetime | None
    version: int


class PeriodOut(BaseModel):
    start: date
    end: date
    due_on: date
    # The period's reports the caller may see, newest first.
    reports: list[ReportSummaryOut]


class PeriodsPage(BaseModel):
    project_id: int
    report_period: str | None
    report_due_day: int | None
    # Newest period first, paged.
    items: list[PeriodOut]
    total: int
    limit: int
    offset: int


class ReportOut(BaseModel):
    id: int
    project_id: int
    period_start: date
    period_end: date
    due_on: date
    status: ReportStatus
    figures: list[FigureOut]
    ratings: dict[str, RatingOut]
    sections: dict[str, str | None]
    section_states: dict[str, SectionState]
    flags: list[FlagOut]
    figures_at: datetime
    replaces_id: int | None
    replace_reason: str | None
    replaced_by_id: int | None = None
    # None for a draft the orchestrator created when the report fell due (item 7.4).
    created_by: int | None
    created_by_name: str | None = None
    published_by: int | None
    published_by_name: str | None = None
    published_at: datetime | None
    withdrawn_by: int | None
    withdrawn_at: datetime | None
    withdrawn_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class DraftKept(BaseModel):
    section: str
    reason: str


class ReportDraftResult(BaseModel):
    """The answer of `POST /projects/{id}/status-reports/{rid}/draft` (item 7.4).

    A dry run has `request` (the exact request that would be sent) and `cost_ceiling_usd` (the
    most it can cost, rounded up to the cent), and nothing is called or saved. A real run has the
    drafted sections with the figures' values filled in, the figure keys it used, its tokens and
    cost and its audit row. `replaced` names the sections saved as unreviewed agent drafts (empty
    in `shadow`); `kept` the ones the agent leaves alone, and why."""

    mode: Literal["shadow", "enforce"]
    dry_run: bool
    model: str
    prompt_version: str
    request: dict[str, Any] | None = None
    cost_ceiling_usd: MoneyStr | None = None
    sections: dict[str, str] = {}
    figure_keys: list[str] = []
    replaced: list[str] = []
    kept: list[DraftKept] = []
    report_version: int | None = None
    audit_id: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: CostStr | None = None


# --- The executive view (item 7.5)


class ExecutiveMilestoneOut(BaseModel):
    id: int
    name: str
    target: date | None
    plan_date: date | None
    reached_on: date | None
    days_late: int
    due_next: bool


class ExecutiveSpendOut(BaseModel):
    spend_to_date: MoneyStr | None
    spend_as_of: date | None
    baseline_total: MoneyStr | None
    spent_pct: str | None
    time_elapsed_pct: str | None
    spend_ahead_points: str | None


class ExecutiveOut(BaseModel):
    """A published report on one page, from the figures it froze when it was published: the
    overall and four ratings, the summary, the decisions needed, at most three milestones (late
    first, then due next), spend against the baseline and the open High risks."""

    id: int
    project_id: int
    project_name: str
    period_start: date
    period_end: date
    published_at: datetime
    published_by_name: str | None
    figures_at: datetime
    ratings: dict[str, RatingOut]
    summary: str | None
    decisions: str | None
    milestones: list[ExecutiveMilestoneOut]
    # How many milestones the report counts as late and as due next period, so the page can say
    # when it holds no more than it shows.
    milestones_late: int
    milestones_due_next: int
    spend: ExecutiveSpendOut
    high_risks: int
    high_risk_ids: list[int]


class ReportOpenedOut(BaseModel):
    """`recorded` is true only for the sponsor's first opening of a published report."""

    recorded: bool
    opened_at: datetime | None
