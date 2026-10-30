"""Shared API schemas: money as a 2-dp string, and paged lists with a stable order."""

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

from fastapi import Query
from pydantic import BaseModel, Field, PlainSerializer
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

MAX_LIMIT = 100
DEFAULT_LIMIT = 50

# Money is a decimal in Python and a JSON string with exactly 2 decimal places.
MoneyStr = Annotated[
    Decimal,
    Field(max_digits=12, decimal_places=2),
    PlainSerializer(
        lambda value: str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        return_type=str,
        when_used="json",
    ),
]

# Query parameters for list routes: `limit: Limit = DEFAULT_LIMIT, offset: Offset = 0`.
Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Offset = Annotated[int, Query(ge=0)]


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


def paginate(
    db: Session, stmt: Select, *, order_by: Sequence[Any], limit: int, offset: int
) -> tuple[list[Any], int]:
    """Run `stmt` one page at a time. `order_by` must end with a unique column (usually the
    primary key) so the order is stable between pages."""
    if not order_by:
        raise ValueError("paginate needs an order_by ending with a unique column")
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(stmt.order_by(*order_by).limit(limit).offset(offset)).all()
    return list(rows), total
