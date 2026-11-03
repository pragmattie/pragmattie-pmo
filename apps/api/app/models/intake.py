"""Project requests: the front door for new work (item 4.1).

A request is `draft` while its project manager works on it, `submitted` once it joins the queue,
and `withdrawn` when the project manager takes it back. `approved`, `declined` and a return to
`draft` are set only at the approval gate (item 4.4, `app.api.gates`). `declined` and
`withdrawn` are final.

`requested_by` is the signed-in person who created it. A request with no client is internal work.

`version` guards concurrent edits: a client sends the version it read, and SQLAlchemy also adds
it to every UPDATE (`version_id_col`), so a write that lost a race fails instead of overwriting.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    column,
    or_,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.migrations_filter import table_name
from app.models.base import Base, Money, utcnow

REQUESTS_TABLE = table_name("project_requests")
CLIENTS_TABLE = table_name("clients")
USERS_TABLE = table_name("users")

STATUSES = ("draft", "submitted", "approved", "declined", "withdrawn")
FINAL_STATUSES = ("declined", "withdrawn")

TITLE_MAX = 200
# The longest a free-text field may be, in characters. Four of these sit in one row, so they stay
# well inside MySQL's row size limit for VARCHAR columns in utf8mb4.
LONG_TEXT_MAX = 2000


class ProjectRequest(Base):
    __tablename__ = REQUESTS_TABLE
    __table_args__ = (
        CheckConstraint(column("status").in_(STATUSES), name="ck_project_requests_status"),
        CheckConstraint(
            or_(
                column("budget_low").is_(None),
                column("budget_high").is_(None),
                column("budget_low") <= column("budget_high"),
            ),
            name="ck_project_requests_budget",
        ),
        Index("ix_project_requests_status", "status"),
        Index("ix_project_requests_submitted_at", "submitted_at"),
        Index("ix_project_requests_wanted_by", "wanted_by"),
        # Audit rows name a request by id, so an id is never reused. MySQL never reuses one;
        # SQLite needs AUTOINCREMENT for that.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(TITLE_MAX))
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{CLIENTS_TABLE}.id", name="fk_project_requests_client"), index=True
    )
    asked_for: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    problem: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    outcome: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    budget_low: Mapped[Decimal | None] = mapped_column(Money)
    budget_high: Mapped[Decimal | None] = mapped_column(Money)
    wanted_by: Mapped[date | None] = mapped_column(Date)
    uses_ai: Mapped[bool] = mapped_column(default=False)
    known_risks: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    requested_by: Mapped[int] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_project_requests_requested_by"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


VALUE_CASES_TABLE = table_name("value_cases")
BENEFITS_TABLE = table_name("value_case_benefits")

VALUE_CASE_SOURCES = ("person", "agent")
MEASURE_MAX = 500
TARGET_MAX = 200
# A value case lists a handful of benefits. At 10, a value case with every field at its longest
# still fits in the API's 64 KiB body limit.
BENEFITS_MAX = 10


class ValueCase(Base):
    """Why a request is worth doing, how we will know, and what it will cost (item 4.2).

    One per request, edited with it: only while the request is `draft`, and every change bumps the
    request's `version`. A value case an agent drafted (`source` "agent") counts only once a person
    has confirmed it (`confirmed_by`); saving it as a person confirms it.
    """

    __tablename__ = VALUE_CASES_TABLE
    __table_args__ = (
        CheckConstraint(
            or_(
                column("cost_low").is_(None),
                column("cost_high").is_(None),
                column("cost_low") <= column("cost_high"),
            ),
            name="ck_value_cases_cost",
        ),
        CheckConstraint(column("source").in_(VALUE_CASE_SOURCES), name="ck_value_cases_source"),
        UniqueConstraint("request_id", name="uq_value_cases_request"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey(f"{REQUESTS_TABLE}.id", name="fk_value_cases_request")
    )
    cost_low: Mapped[Decimal | None] = mapped_column(Money)
    cost_high: Mapped[Decimal | None] = mapped_column(Money)
    cost_method: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    strategic_fit: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    cost_of_not_doing: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    source: Mapped[str] = mapped_column(String(16), default="person")
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_value_cases_confirmed_by"), index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    benefits: Mapped[list["ValueCaseBenefit"]] = relationship(
        order_by="ValueCaseBenefit.position", cascade="all, delete-orphan"
    )


class ValueCaseBenefit(Base):
    """One benefit: what it is, how it is measured, where it stands now, the target and when."""

    __tablename__ = BENEFITS_TABLE
    __table_args__ = (
        UniqueConstraint("value_case_id", "position", name="uq_value_case_benefits_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    value_case_id: Mapped[int] = mapped_column(
        ForeignKey(f"{VALUE_CASES_TABLE}.id", name="fk_value_case_benefits_value_case")
    )
    position: Mapped[int]
    description: Mapped[str] = mapped_column(String(LONG_TEXT_MAX))
    measure: Mapped[str | None] = mapped_column(String(MEASURE_MAX))
    current_value: Mapped[str | None] = mapped_column(String(TARGET_MAX))
    target: Mapped[str | None] = mapped_column(String(TARGET_MAX))
    target_date: Mapped[date | None] = mapped_column(Date)


READINESS_TABLE = table_name("readiness_checks")
READINESS_ACTIONS_TABLE = table_name("readiness_actions")

READINESS_AREAS = ("problem", "approach", "data")
RATINGS = ("green", "yellow", "red")
# Worst last: the overall result is the rating furthest along this list.
RATING_ORDER = {rating: rank for rank, rating in enumerate(RATINGS)}
UNRATED = "unrated"
ACTION_MAX = 500
# Actions per area. At 10, a check with every field at its longest fits in the API's body limit.
ACTIONS_MAX = 10


def _rating_check(area: str) -> CheckConstraint:
    rating = column(f"{area}_rating")
    return CheckConstraint(
        or_(rating.is_(None), rating.in_(RATINGS)), name=f"ck_readiness_checks_{area}_rating"
    )


class ReadinessCheck(Base):
    """Whether a request that uses AI is ready (item 4.3, R11).

    Three areas, each rated green, yellow or red with a note: the problem is worth solving, AI is
    the right tool, and the data is good enough. One per request, edited with it like the value
    case. A rating may be empty only on an agent's draft; a person's save rates every area.
    """

    __tablename__ = READINESS_TABLE
    __table_args__ = (
        *(_rating_check(area) for area in READINESS_AREAS),
        CheckConstraint(
            column("source").in_(VALUE_CASE_SOURCES), name="ck_readiness_checks_source"
        ),
        UniqueConstraint("request_id", name="uq_readiness_checks_request"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey(f"{REQUESTS_TABLE}.id", name="fk_readiness_checks_request")
    )
    problem_rating: Mapped[str | None] = mapped_column(String(8))
    problem_note: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    approach_rating: Mapped[str | None] = mapped_column(String(8))
    approach_note: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    data_rating: Mapped[str | None] = mapped_column(String(8))
    data_note: Mapped[str | None] = mapped_column(String(LONG_TEXT_MAX))
    source: Mapped[str] = mapped_column(String(16), default="person")
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_readiness_checks_confirmed_by"), index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    actions: Mapped[list["ReadinessAction"]] = relationship(
        order_by="(ReadinessAction.area, ReadinessAction.position)", cascade="all, delete-orphan"
    )

    def ratings(self) -> dict[str, str | None]:
        return {area: getattr(self, f"{area}_rating") for area in READINESS_AREAS}

    def area_actions(self, area: str) -> list["ReadinessAction"]:
        return sorted((a for a in self.actions if a.area == area), key=lambda a: a.position)

    @property
    def overall(self) -> str:
        """The worst of the three ratings. Never stored. An agent's draft no person has confirmed
        yet, or one with an area left unrated, is `unrated`."""
        ratings = self.ratings().values()
        if self.confirmed_by is None or None in ratings:
            return UNRATED
        return max(ratings, key=RATING_ORDER.__getitem__)


class ReadinessAction(Base):
    """One thing to do before an area is ready: what, who owns it, and by when. Owner and date
    are empty only on an agent's draft; a person's save sets both."""

    __tablename__ = READINESS_ACTIONS_TABLE
    __table_args__ = (
        CheckConstraint(column("area").in_(READINESS_AREAS), name="ck_readiness_actions_area"),
        UniqueConstraint("check_id", "area", "position", name="uq_readiness_actions_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(
        ForeignKey(f"{READINESS_TABLE}.id", name="fk_readiness_actions_check")
    )
    area: Mapped[str] = mapped_column(String(16))
    position: Mapped[int]
    description: Mapped[str] = mapped_column(String(ACTION_MAX))
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_readiness_actions_owner"), index=True
    )
    due_date: Mapped[date | None] = mapped_column(Date)
