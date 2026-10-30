from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.models import Money
from app.schemas.common import MAX_LIMIT, Limit, MoneyStr, Offset, Page, paginate


class _Base(DeclarativeBase):
    """Separate metadata, so these test tables never reach Alembic."""


class _Fee(_Base):
    __tablename__ = "fee"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str]
    amount: Mapped[Decimal] = mapped_column(Money)


class FeeOut(BaseModel):
    label: str
    amount: MoneyStr


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}")
    _Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def test_money_column_is_numeric_12_2():
    column = _Fee.__table__.c.amount
    assert (column.type.precision, column.type.scale) == (12, 2)
    assert column.type.asdecimal is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [(Decimal("1250"), "1250.00"), (Decimal("0.5"), "0.50"), (Decimal("99.99"), "99.99")],
)
def test_money_serialises_as_two_decimal_string(value, expected):
    fee = FeeOut(label="x", amount=value)
    assert fee.model_dump(mode="json") == {"label": "x", "amount": expected}
    assert isinstance(fee.model_dump()["amount"], Decimal)


def test_money_rejects_more_than_two_decimals():
    with pytest.raises(ValidationError):
        FeeOut(label="x", amount=Decimal("1.234"))


def test_money_round_trips_through_the_database(session):
    session.add(_Fee(id=1, label="Discovery", amount=Decimal("12500.5")))
    session.commit()
    stored = session.scalar(select(_Fee.amount))
    assert isinstance(stored, Decimal)
    assert FeeOut(label="Discovery", amount=stored).model_dump(mode="json")["amount"] == "12500.50"


def test_page_serialises_items_and_paging():
    page = Page[FeeOut](items=[FeeOut(label="a", amount=Decimal("1"))], total=3, limit=1, offset=0)
    assert page.model_dump(mode="json") == {
        "items": [{"label": "a", "amount": "1.00"}],
        "total": 3,
        "limit": 1,
        "offset": 0,
    }


def test_paginate_is_stable_with_ties(session):
    # Every label ties; the id tie-breaker keeps pages disjoint and complete.
    session.add_all(_Fee(id=i, label="same", amount=Decimal(i)) for i in (5, 3, 1, 4, 2))
    session.commit()
    seen = []
    for offset in (0, 2, 4):
        rows, total = paginate(
            session, select(_Fee), order_by=[_Fee.label, _Fee.id], limit=2, offset=offset
        )
        assert total == 5
        seen += [row.id for row in rows]
    assert seen == [1, 2, 3, 4, 5]


def test_paginate_requires_an_order(session):
    with pytest.raises(ValueError, match="order_by"):
        paginate(session, select(_Fee), order_by=[], limit=10, offset=0)


def test_limit_and_offset_bounds():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/items")
    def items(limit: Limit = 50, offset: Offset = 0) -> dict:
        return {"limit": limit, "offset": offset}

    client = TestClient(app)
    assert client.get("/items").json() == {"limit": 50, "offset": 0}
    assert client.get(f"/items?limit={MAX_LIMIT + 1}").status_code == 422
    assert client.get("/items?limit=0").status_code == 422
    assert client.get("/items?offset=-1").status_code == 422
