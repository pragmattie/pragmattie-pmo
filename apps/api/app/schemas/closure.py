"""API shapes of closing a project (item 7.7): the closure report, benefits measured at close and
lessons.

Every input refuses unknown fields. Figures are worked out by code and never taken from a client.
Every write carries the version the client read (0 for a benefit not measured yet); a stale one
is a 409.
"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.closure import LESSON_TEXT_MAX, SECTION_MAX
from app.models.intake import TARGET_MAX
from app.schemas.audit import CostStr
from app.schemas.common import MoneyStr
from app.schemas.plan import Version
from app.schemas.report import FigureOut, FlagOut, SectionState

SectionText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=SECTION_MAX)]
LessonText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LESSON_TEXT_MAX)
]
ValueText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TARGET_MAX)
]
Category = Literal["scope", "schedule", "cost", "risk", "people", "ai"]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClosureSections(_Input):
    delivered: SectionText | None = None
    value_case: SectionText | None = None
    remaining: SectionText | None = None


class ClosureTextIn(_Input):
    """Edit the text: only the sections sent change."""

    version: Version
    sections: ClosureSections


class ClosureVersionIn(_Input):
    version: Version


class BenefitIn(_Input):
    """A benefit as measured at close, entered by a person: the value now with the day it was
    measured, or the owner and date to measure it later (or both). `version` is 0 the first
    time."""

    version: Annotated[int, Field(ge=0)]
    value_now: ValueText | None = None
    value_now_on: date | None = None
    measure_owner_id: int | None = None
    measure_by: date | None = None


class LessonIn(_Input):
    category: Category
    what_happened: LessonText
    do_again: LessonText | None = None
    do_differently: LessonText | None = None
    suggest_as_risk: bool = False


class LessonEditIn(LessonIn):
    version: Version


class LessonVersionIn(_Input):
    version: Version


class LessonRecordIn(LessonVersionIn):
    # Whether the recorded suggestion is offered to future projects as a suggested risk.
    suggest_as_risk: bool = False


class BenefitOut(BaseModel):
    benefit_id: int
    position: int
    description: str
    measure: str | None
    value_before: str | None
    target: str | None
    target_date: date | None
    value_now: str | None
    value_now_on: date | None
    measure_owner_id: int | None
    measure_owner_name: str | None = None
    measure_by: date | None
    # 0 until the benefit is first measured or given an owner.
    version: int


class LessonOut(BaseModel):
    id: int
    project_id: int
    category: Category
    what_happened: str
    do_again: str | None
    do_differently: str | None
    suggest_as_risk: bool
    source: Literal["person", "agent"]
    status: Literal["suggested", "recorded", "discarded"]
    reviewed_by: int | None
    reviewed_at: datetime | None
    version: int
    created_at: datetime


class AcceptedRiskOut(BaseModel):
    id: int
    title: str
    band: str | None


class ClosureReportOut(BaseModel):
    id: int
    project_id: int
    figures: list[FigureOut]
    figures_at: datetime
    sections: dict[str, str | None]
    section_states: dict[str, SectionState]
    flags: list[FlagOut]
    frozen_at: datetime | None
    version: int
    created_at: datetime


class ClosureOut(BaseModel):
    """A project's closure: its report (none until one is made), its benefits, its lessons and
    the accepted risks still standing."""

    project_id: int
    project_name: str
    project_stage: str
    project_status: str
    report: ClosureReportOut | None
    benefits: list[BenefitOut]
    lessons: list[LessonOut]
    accepted_risks: list[AcceptedRiskOut]


class ClosureDraftResult(BaseModel):
    """What the closure agent drafted, or with `dry_run` the request it would send."""

    mode: Literal["shadow", "enforce"]
    dry_run: bool
    model: str
    prompt_version: str
    request: dict | None = None
    cost_ceiling_usd: MoneyStr | None = None
    sections: dict[str, str] | None = None
    lessons: list[dict] | None = None
    lesson_ids: list[int] | None = None
    figure_keys: list[str] | None = None
    replaced: list[str] | None = None
    kept: list[dict[str, str]] | None = None
    report_version: int | None = None
    audit_id: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: CostStr | None = None
