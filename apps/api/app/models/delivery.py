"""Spend to date for a project in delivery (item 7.2), and each deliverable's acceptance rounds
(item 7.6).

`ProjectSpend` is append-only, as the audit table is: the newest entry is the project's current
figure, and a correction is a new entry whose `supersedes_id` names the one it corrects.

`DeliverableAcceptance` is append-only too: one row per offer of a deliverable to the client, and
one per answer to an offer, so a deliverable's history shows every round. An offer records to whom,
on what date and by what means it was offered (nothing is sent from here); an answer records
`accepted` or `not_accepted`, the day it came, what is missing and a note. `source` is `person`, or
`simulated` for the simulated client's answer, the only row with no person. `deliverable_id` is not
a foreign key: an approved change request may remove a deliverable from the plan, and its history
stays. Each round's offer and answer share `round`, and `UNIQUE(deliverable_id, round, kind)` keeps
one of each.

Changing or deleting a loaded row of either table, and a bulk `update()` or `delete()` on one
through any `Session`, raise `AppendOnlyError`. Raw SQL on a plain connection can still bypass
this, as on the audit table.

Milestone and deliverable progress live on the plan rows themselves (`app.models.plan`).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Delete,
    ForeignKey,
    String,
    UniqueConstraint,
    Update,
    event,
)
from sqlalchemy import column as sql_column
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column

from app.migrations_filter import table_name
from app.models.audit import AppendOnlyError
from app.models.base import Base, Money, utcnow

SPEND_TABLE = table_name("project_spend")
ACCEPTANCES_TABLE = table_name("deliverable_acceptances")
PROJECTS_TABLE = table_name("projects")
USERS_TABLE = table_name("users")

NOTE_MAX = 200

# Deliverable acceptance (item 7.6).
OFFER = "offer"
ANSWER = "answer"
ACCEPTANCE_KINDS = (OFFER, ANSWER)
ACCEPTED_ANSWER = "accepted"
NOT_ACCEPTED = "not_accepted"
ACCEPTANCE_ANSWERS = (ACCEPTED_ANSWER, NOT_ACCEPTED)
PERSON = "person"
SIMULATED = "simulated"
ACCEPTANCE_SOURCES = (PERSON, SIMULATED)
NAME_MAX = 200
OFFERED_TO_MAX = 200
MEANS_MAX = 100
ACCEPTANCE_TEXT_MAX = 1000


class ProjectSpend(Base):
    __tablename__ = SPEND_TABLE
    __table_args__ = (
        CheckConstraint(sql_column("amount") >= 0, name="ck_project_spend_amount"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_project_spend_project"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Money)
    as_of: Mapped[date] = mapped_column(Date)
    note: Mapped[str] = mapped_column(String(NOTE_MAX))
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{SPEND_TABLE}.id", name="fk_project_spend_supersedes"), index=True
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_project_spend_created_by"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DeliverableAcceptance(Base):
    __tablename__ = ACCEPTANCES_TABLE
    __table_args__ = (
        CheckConstraint(
            sql_column("kind").in_(ACCEPTANCE_KINDS), name="ck_deliverable_acceptances_kind"
        ),
        CheckConstraint(
            sql_column("answer").in_(ACCEPTANCE_ANSWERS), name="ck_deliverable_acceptances_answer"
        ),
        CheckConstraint(
            sql_column("source").in_(ACCEPTANCE_SOURCES), name="ck_deliverable_acceptances_source"
        ),
        # An offer says to whom, when and how; an answer says what and when.
        CheckConstraint(
            "(kind = 'offer' AND offered_to IS NOT NULL AND offered_on IS NOT NULL "
            "AND means IS NOT NULL AND answer IS NULL) OR "
            "(kind = 'answer' AND answer IS NOT NULL AND answered_on IS NOT NULL)",
            name="ck_deliverable_acceptances_shape",
        ),
        # Only the simulated client's answer has no person.
        CheckConstraint(
            "recorded_by IS NOT NULL OR (source = 'simulated' AND kind = 'answer')",
            name="ck_deliverable_acceptances_person",
        ),
        CheckConstraint(sql_column("round") >= 1, name="ck_deliverable_acceptances_round"),
        UniqueConstraint(
            "deliverable_id", "round", "kind", name="uq_deliverable_acceptances_round"
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey(f"{PROJECTS_TABLE}.id", name="fk_deliverable_acceptances_project"), index=True
    )
    # Not a foreign key: the history outlives a deliverable a change request removes.
    deliverable_id: Mapped[int]
    deliverable_name: Mapped[str] = mapped_column(String(NAME_MAX))
    round: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16))
    offered_to: Mapped[str | None] = mapped_column(String(OFFERED_TO_MAX))
    offered_on: Mapped[date | None] = mapped_column(Date)
    means: Mapped[str | None] = mapped_column(String(MEANS_MAX))
    answer: Mapped[str | None] = mapped_column(String(16))
    answered_on: Mapped[date | None] = mapped_column(Date)
    missing: Mapped[str | None] = mapped_column(String(ACCEPTANCE_TEXT_MAX))
    note: Mapped[str | None] = mapped_column(String(ACCEPTANCE_TEXT_MAX))
    source: Mapped[str] = mapped_column(String(16), default=PERSON)
    recorded_by: Mapped[int | None] = mapped_column(
        ForeignKey(f"{USERS_TABLE}.id", name="fk_deliverable_acceptances_recorded_by"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


APPEND_ONLY_TABLES = frozenset({SPEND_TABLE, ACCEPTANCES_TABLE})


def _refuse_for(table: str):
    def refuse(*_args: Any) -> None:
        raise AppendOnlyError(f"{table} is append-only: record a new entry instead")

    return refuse


for _model in (ProjectSpend, DeliverableAcceptance):
    event.listen(_model, "before_update", _refuse_for(_model.__tablename__))
    event.listen(_model, "before_delete", _refuse_for(_model.__tablename__))


@event.listens_for(Session, "do_orm_execute")
def _refuse_bulk_changes(state: ORMExecuteState) -> None:
    """Bulk `update()` and `delete()` skip the mapper hooks above, so refuse them here."""
    statement = state.statement
    if isinstance(statement, Update | Delete) and statement.table.name in APPEND_ONLY_TABLES:
        _refuse_for(statement.table.name)()
