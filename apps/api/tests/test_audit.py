import inspect
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.audit as audit_module
from app.audit import count_decisions, list_decisions, record_decision
from app.db import engine
from app.models import AuditDecision, Base


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


def _fields(**overrides):
    fields = {
        "agent": "triage",
        "trigger": "agent",
        "subject_type": "issue",
        "subject_id": "3",
        "version": 1,
        "attempt": 1,
        "status": "ok",
        "tier": "T2",
    }
    return fields | overrides


def test_record_decision_flushes_and_joins_the_callers_transaction(db):
    row = record_decision(db, **_fields(signals={"size": "small"}))
    # Flushed: it has an id and the same session can read it back.
    assert row.id is not None
    assert db.scalar(select(AuditDecision.signals).where(AuditDecision.id == row.id)) == {
        "size": "small"
    }
    # Not committed: another connection cannot see it, and a rollback removes it.
    with Session(engine) as other:
        assert other.scalar(select(AuditDecision).where(AuditDecision.id == row.id)) is None
    db.rollback()
    assert count_decisions(db) == 0


def test_record_decision_rejects_unknown_fields(db):
    with pytest.raises(TypeError, match="colour"):
        record_decision(db, **_fields(colour="red"))
    for managed in ("id", "created_at"):
        with pytest.raises(TypeError, match=managed):
            record_decision(db, **_fields(**{managed: None}))


def test_duplicate_attempt_is_rejected(db):
    record_decision(db, **_fields())
    db.commit()
    with pytest.raises(IntegrityError):
        record_decision(db, **_fields(status="error"))
    db.rollback()
    # A new attempt for the same subject and version is fine.
    record_decision(db, **_fields(attempt=2))
    assert count_decisions(db) == 2


@pytest.mark.parametrize(("field", "value"), [("trigger", "robot"), ("status", "maybe")])
def test_closed_value_sets_are_enforced(db, field, value):
    with pytest.raises(IntegrityError):
        record_decision(db, **_fields(**{field: value}))


def test_correction_is_a_new_row_that_supersedes_the_original(db):
    original = record_decision(db, **_fields(output={"tier": "T1"}))
    db.commit()
    correction = record_decision(
        db,
        **_fields(
            trigger="human",
            attempt=2,
            human_override={"tier": "T2"},
            supersedes_id=original.id,
            actor_user_id=7,
        ),
    )
    db.commit()
    db.expire_all()
    first = db.get(AuditDecision, original.id)
    assert first.output == {"tier": "T1"} and first.human_override is None
    assert correction.supersedes_id == original.id
    assert count_decisions(db) == 2


def test_no_update_or_delete_is_exposed():
    names = [name for name, _ in inspect.getmembers(audit_module, inspect.isfunction)]
    for name in names:
        assert not any(word in name for word in ("update", "delete", "remove", "edit")), name
    source = inspect.getsource(audit_module)
    for call in ("update(", "delete(", ".merge("):
        assert call not in source


def test_money_and_tokens_round_trip(db):
    row = record_decision(
        db,
        **_fields(model="claude-opus-5-5", input_tokens=10, output_tokens=20, cost_usd="0.5"),
    )
    db.commit()
    db.expire_all()
    stored = db.get(AuditDecision, row.id)
    assert stored.cost_usd == Decimal("0.50")
    assert (stored.input_tokens, stored.output_tokens) == (10, 20)


def _seed(db):
    same_time = datetime(2026, 10, 31, 12, 0, 0)
    specs = [
        {"agent": "triage", "subject_id": "1", "status": "ok", "tier": "T1"},
        {"agent": "triage", "subject_id": "2", "status": "error", "tier": "T2"},
        {"agent": "builder", "subject_id": "1", "status": "ok", "tier": "T2"},
        {"agent": "builder", "subject_id": "2", "status": "rejected", "tier": "T3"},
        {"agent": "reviewer", "subject_id": "1", "status": "skipped", "tier": "T2"},
    ]
    # Rows are built directly so they can share one timestamp; record_decision always sets it.
    rows = [AuditDecision(**_fields(created_at=same_time, **spec)) for spec in specs]
    db.add_all(rows)
    db.commit()
    return rows


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({}, 5),
        ({"agent": "triage"}, 2),
        ({"subject_type": "issue", "subject_id": "1"}, 3),
        ({"status": "ok"}, 2),
        ({"tier": "T2"}, 3),
        ({"agent": "builder", "tier": "T2"}, 1),
        ({"agent": "nobody"}, 0),
    ],
)
def test_list_and_count_share_filters(db, filters, expected):
    _seed(db)
    assert count_decisions(db, **filters) == expected
    rows = list_decisions(db, limit=100, **filters)
    assert len(rows) == expected
    for key, value in filters.items():
        assert all(getattr(row, key) == value for row in rows)


def test_list_is_newest_first_and_stable_when_timestamps_tie(db):
    rows = _seed(db)
    later = AuditDecision(**_fields(agent="late", created_at=datetime(2026, 11, 1)))
    db.add(later)
    db.commit()
    seen = []
    for offset in (0, 2, 4):
        seen += [row.id for row in list_decisions(db, limit=2, offset=offset)]
    # Newest first, then id descending among the tied timestamps; pages never overlap.
    assert seen == [later.id] + sorted((row.id for row in rows), reverse=True)


def test_api_returns_a_page_with_money_as_a_string(db, client):
    _seed(db)
    record_decision(db, **_fields(agent="costed", cost_usd=Decimal("1.5"), output={"ok": True}))
    db.commit()

    body = client.get("/api/v1/audit", params={"agent": "costed"}).json()
    assert body["total"] == 1 and body["limit"] == 50 and body["offset"] == 0
    item = body["items"][0]
    assert item["cost_usd"] == "1.50"
    assert item["trigger"] == "agent" and item["output"] == {"ok": True}

    page = client.get("/api/v1/audit", params={"limit": 2, "offset": 1, "tier": "T2"}).json()
    assert page["total"] == 4 and len(page["items"]) == 2
    assert page["items"][0]["cost_usd"] is None


def test_api_validates_paging(db, client):
    assert client.get("/api/v1/audit", params={"limit": 101}).status_code == 422
    assert client.get("/api/v1/audit", params={"offset": -1}).status_code == 422
