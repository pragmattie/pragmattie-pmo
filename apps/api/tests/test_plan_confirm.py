"""Confirming the planner agent's plan items as a person's (item 6.6).

`POST /projects/{id}/plan/confirm` marks agent items `person` without changing them, under the
plan's usual rules: only in planning, with the item's version for one item and the plan's for all,
and one audit row per confirmation.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.plan import NOT_PLANNING, STALE
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectPlan,
    User,
)

PROJECTS = "/api/v1/projects"


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


@pytest.fixture
def web(db) -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def pm(web, db, sign_in) -> User:
    return sign_in(web, db, "project_manager")


@pytest.fixture
def plan(db) -> ProjectPlan:
    """A project in planning whose plan holds one agent and one person item of each kind."""
    project = Project(name="Forecast", stage="planning", sponsor="S", pm="P", status="on_track")
    db.add(project)
    db.flush()
    plan = ProjectPlan(project_id=project.id, contingency_pct=0)
    db.add(plan)
    db.flush()
    for source in ("agent", "person"):
        milestone = PlanMilestone(
            plan_id=plan.id, name=f"Pilot {source}", target_date=date(2027, 1, 1), source=source
        )
        db.add(milestone)
        db.flush()
        db.add(
            PlanDeliverable(
                plan_id=plan.id, name=f"Model {source}", milestone_id=milestone.id, source=source
            )
        )
        db.add(
            PlanEstimateLine(
                plan_id=plan.id,
                description=f"Team {source}",
                category="people",
                amount=Decimal("1200.50"),
                source=source,
            )
        )
    db.commit()
    return plan


def _url(plan: ProjectPlan, suffix: str = "") -> str:
    return f"{PROJECTS}/{plan.project_id}/plan{suffix}"


def _items(web: TestClient, plan: ProjectPlan, kind: str) -> list[dict]:
    response = web.get(_url(plan, f"/{kind}"))
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _agent(web: TestClient, plan: ProjectPlan, kind: str) -> dict:
    return next(item for item in _items(web, plan, kind) if item["source"] == "agent")


def _plan_version(web: TestClient, plan: ProjectPlan) -> int:
    return web.get(_url(plan)).json()["version"]


def _audit(db: Session) -> list[AuditDecision]:
    db.expire_all()
    return list(
        db.scalars(
            select(AuditDecision).where(AuditDecision.agent == "project").order_by(AuditDecision.id)
        )
    )


def _without(item: dict, *names: str) -> dict:
    return {k: v for k, v in item.items() if k not in ("source", "version", *names)}


LISTS = {
    "milestone": "milestones",
    "deliverable": "deliverables",
    "estimate_line": "estimate-lines",
}


@pytest.mark.parametrize("kind", list(LISTS))
def test_confirming_one_item_marks_it_a_persons_and_changes_nothing_else(web, db, pm, plan, kind):
    before = _agent(web, plan, LISTS[kind])
    plan_version = _plan_version(web, plan)
    response = web.post(
        _url(plan, "/confirm"),
        json={"kind": kind, "id": before["id"], "version": before["version"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == plan_version + 1
    after = next(i for i in _items(web, plan, LISTS[kind]) if i["id"] == before["id"])
    assert after["source"] == "person"
    assert after["version"] == before["version"] + 1
    assert _without(after) == _without(before)
    # The other agent items are untouched.
    others = [k for k in LISTS if k != kind]
    for other in others:
        assert _agent(web, plan, LISTS[other])["source"] == "agent"

    rows = _audit(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.action_taken["action"] == "confirm"
    assert row.actor_user_id == pm.id
    assert row.trigger == "human"
    assert row.subject_id == str(before["id"])
    assert row.signals["source"] == "agent"
    assert row.output["source"] == "person"


def test_confirm_all_marks_every_agent_item_in_one_audit_row(web, db, pm, plan):
    before = {kind: _items(web, plan, name) for kind, name in LISTS.items()}
    version = _plan_version(web, plan)
    response = web.post(_url(plan, "/confirm"), json={"version": version})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == version + 1
    for kind, name in LISTS.items():
        after = _items(web, plan, name)
        assert {i["source"] for i in after} == {"person"}
        assert [_without(i) for i in after] == [_without(i) for i in before[kind]]

    rows = _audit(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.subject_type == "project_plan"
    assert row.subject_id == str(plan.id)
    assert row.actor_user_id == pm.id
    assert row.action_taken["action"] == "confirm"
    agent_ids = {
        kind: [i["id"] for i in items if i["source"] == "agent"] for kind, items in before.items()
    }
    assert row.action_taken["confirmed"] == agent_ids


def test_confirming_a_persons_item_or_a_plan_with_no_agent_items_writes_nothing(web, db, pm, plan):
    person = next(i for i in _items(web, plan, "milestones") if i["source"] == "person")
    version = _plan_version(web, plan)
    response = web.post(
        _url(plan, "/confirm"),
        json={"kind": "milestone", "id": person["id"], "version": person["version"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == version
    assert _audit(db) == []
    first = web.post(_url(plan, "/confirm"), json={"version": version})
    assert first.json()["version"] == version + 1
    again = web.post(_url(plan, "/confirm"), json={"version": version + 1})
    assert again.status_code == 200, again.text
    assert again.json()["version"] == version + 1
    assert len(_audit(db)) == 1


def test_a_stale_version_is_409(web, db, pm, plan):
    item = _agent(web, plan, "deliverables")
    stale_item = web.post(
        _url(plan, "/confirm"),
        json={"kind": "deliverable", "id": item["id"], "version": item["version"] + 1},
    )
    assert stale_item.status_code == 409
    assert stale_item.json()["detail"] == STALE
    version = _plan_version(web, plan)
    stale_plan = web.post(_url(plan, "/confirm"), json={"version": version + 1})
    assert stale_plan.status_code == 409
    assert stale_plan.json()["detail"] == STALE
    assert _audit(db) == []


def test_confirming_outside_planning_is_409(web, db, pm, plan):
    project = db.get(Project, plan.project_id)
    project.stage = "delivery"
    db.commit()
    response = web.post(_url(plan, "/confirm"), json={"version": _plan_version(web, plan)})
    assert response.status_code == 409
    assert response.json()["detail"] == NOT_PLANNING


@pytest.mark.parametrize(
    "body",
    [
        {"version": 1, "kind": "milestone"},
        {"version": 1, "id": 1},
        {"version": 1, "kind": "risk", "id": 1},
        {"version": 1, "source": "person"},
        {"version": 0},
    ],
)
def test_a_bad_body_is_422(web, pm, plan, body):
    assert web.post(_url(plan, "/confirm"), json=body).status_code == 422


def test_an_item_of_another_plan_or_unknown_is_404(web, db, pm, plan):
    response = web.post(
        _url(plan, "/confirm"), json={"kind": "estimate_line", "id": 9999, "version": 1}
    )
    assert response.status_code == 404


def test_a_sponsor_cannot_confirm(web, db, plan, sign_in):
    sign_in(web, db, "sponsor")
    assert web.post(_url(plan, "/confirm"), json={"version": 1}).status_code == 403
