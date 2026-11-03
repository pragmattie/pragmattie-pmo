"""The risk register: threats and opportunities, one project each.

`score` and `band` are written only by the server, from the risk policy (`app.policy.score_risk`);
they are stored so the list can filter by band and sort by score in SQL. After the policy file
changes, `python -m app.risks rescore` brings every stored score and band up to date.

`status` moves only along agreed paths: a proposed risk is opened or rejected, an open one is
accepted or closed, a closed one may reopen, and an accepted one may close or is reopened when a
re-score moves it into a higher band (`app.api.risks`).

`version` guards concurrent edits: a client sends the version it read, and SQLAlchemy also adds
it to every UPDATE (`version_id_col`), so a write that lost a race fails instead of overwriting.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    column,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, utcnow

RISKS_TABLE = table_name("risks")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

KINDS = ("threat", "opportunity")
CATEGORIES = ("ai", "project")
THREAT_RESPONSES = ("avoid", "reduce", "share", "accept")
OPPORTUNITY_RESPONSES = ("pursue", "increase", "share", "accept")
RESPONSES_BY_KIND = {"threat": THREAT_RESPONSES, "opportunity": OPPORTUNITY_RESPONSES}
RESPONSES = tuple(dict.fromkeys(THREAT_RESPONSES + OPPORTUNITY_RESPONSES))
STATUSES = ("proposed", "open", "accepted", "closed", "rejected")
SOURCES = ("person", "agent")


def _check(name: str, sql: Any) -> CheckConstraint:
    return CheckConstraint(sql, name=f"ck_risks_{name}")


class Risk(Base):
    __tablename__ = RISKS_TABLE
    __table_args__ = (
        _check("kind", column("kind").in_(KINDS)),
        _check("category", column("category").in_(CATEGORIES)),
        _check("response", column("response").in_(RESPONSES)),
        _check("status", column("status").in_(STATUSES)),
        _check("source", column("source").in_(SOURCES)),
        _check("probability", column("probability").between(1, 5)),
        _check("impact", column("impact").between(1, 5)),
        _check("score", column("score").between(1, 25)),
        Index("ix_risks_project_status", "project_id", "status"),
        Index("ix_risks_band", "band"),
        Index("ix_risks_score", "score"),
        Index("ix_risks_escalated_to", "escalated_to"),
        UniqueConstraint("seed_key", name="uq_risks_seed_key"),
        # Audit rows name a risk by id, so an id is never reused, even after a delete (the demo
        # seed's --reset). MySQL never reuses one; SQLite needs AUTOINCREMENT for that.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_risks_project")
    )
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    cause: Mapped[str] = mapped_column(Text)
    effect: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(16))
    objectives: Mapped[list[str]] = mapped_column(JSON)
    probability: Mapped[int | None]
    impact: Mapped[int | None]
    score: Mapped[int | None]
    band: Mapped[str | None] = mapped_column(String(32))
    response: Mapped[str] = mapped_column(String(16))
    response_plan: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_risks_owner_user"), index=True
    )
    status: Mapped[str] = mapped_column(String(16))
    needs_judgement: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(16))
    # The role an open risk is escalated to, and the band that sent it there; empty once that
    # role acts (accepts it) or the risk leaves open. See `app.risks.settle_escalation`.
    escalated_to: Mapped[str | None] = mapped_column(String(32))
    escalated_band: Mapped[str | None] = mapped_column(String(32))
    # When an accepted risk is next looked at; set by the accept route.
    review_date: Mapped[date | None] = mapped_column(Date)
    version: Mapped[int] = mapped_column(default=1)
    # Set only on demo risks made by `python -m app.seed`; risks people add leave it empty.
    seed_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}
