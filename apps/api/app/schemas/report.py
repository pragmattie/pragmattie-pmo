"""API shapes of status reports (item 7.3).

Every input refuses unknown fields. Figures and ratings are worked out by code and never taken
from a client; a client writes only the text sections. Every write on a report carries the
version the client read; a stale one is a 409.
"""

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.models.report import REASON_MAX, SECTION_MAX
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
    flags: list[FlagOut]
    figures_at: datetime
    replaces_id: int | None
    replace_reason: str | None
    replaced_by_id: int | None = None
    created_by: int
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
