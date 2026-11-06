"""A project's baselines: frozen copies of its plan (item 5.4).

Passing the go-ahead gate stores baseline 1, the plan as it stood when the sponsor decided; change
requests (5.5) add 2, 3 and so on. `number` counts per project. A baseline is never changed or
deleted: no route writes one after it is made. `plan` holds the scope, deliverables, milestones,
estimate lines, contingency and total, with money as 2-decimal strings; `total` is the same total
as a decimal, so it can be compared without reading the JSON. `gate_decision_id` is the decision
that made it.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.migrations_filter import table_name
from app.models.base import Base, Money, utcnow

BASELINES_TABLE = table_name("project_baselines")
PROJECTS_TABLE = table_name("projects")
GATE_DECISIONS_TABLE = table_name("gate_decisions")


class ProjectBaseline(Base):
    __tablename__ = BASELINES_TABLE
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_project_baselines_number"),
        # Audit rows name a baseline by id, so an id is never reused.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_project_baselines_project")
    )
    number: Mapped[int]
    gate_decision_id: Mapped[int] = mapped_column(
        ForeignKey(f"{GATE_DECISIONS_TABLE}.id", name="fk_project_baselines_gate_decision"),
        index=True,
    )
    plan: Mapped[Any] = mapped_column(JSON)
    total: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
