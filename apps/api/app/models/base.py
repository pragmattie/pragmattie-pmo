"""Declarative base and shared column helpers for SQLAlchemy 2 models.

Models use `Mapped[...]` / `mapped_column(...)` and name tables with
`app.migrations_filter.table_name()`.
"""

from datetime import UTC, datetime

from sqlalchemy import Numeric
from sqlalchemy.orm import DeclarativeBase

# Money columns: exact decimal, 12 digits with 2 after the point. Use `mapped_column(Money)`.
Money = Numeric(12, 2, asdecimal=True)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """The current time in UTC, without tzinfo (MySQL DATETIME has no time zone)."""
    return datetime.now(UTC).replace(tzinfo=None)
