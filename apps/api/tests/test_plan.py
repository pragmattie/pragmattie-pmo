"""A project's plan (item 5.1): scope, deliverables, milestones, estimate and reporting plan.

The builder's prefill is checked where the builder is tested (test_project_from_request.py);
here: the migration's backfill, the computed total, `missing`, and the write rules.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.api import intake
from app.api.plan import (
    BAD_DUE_DAY,
    BAD_MILESTONE,
    BAD_OWNER,
    DAY_NEEDS_PERIOD,
    NOT_PLANNING,
    PERIOD_NEEDS_DAY,
    STALE,
)
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    PlanDeliverable,
    Project,
    ProjectBrief,
    ProjectPlan,
    User,
)
from app.plans import budget_fit, compute_total, new_plan

API_DIR = Path(__file__).resolve().parents[1]
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


def _project(db: Session, *, stage: str = "planning", low="1000", high="2000") -> Project:
    project = Project(name="Forecast", stage=stage, sponsor="S", pm="P", status="on_track")
    db.add(project)
    db.flush()
    brief = ProjectBrief(
        project_id=project.id,
        purpose="Why",
        objectives="What",
        scope_in="Two warehouses",
        scope_out="Stores",
        budget_low=Decimal(low) if low is not None else None,
        budget_high=Decimal(high) if high is not None else None,
        decision_rights=[],
    )
    db.add(brief)
    db.add(new_plan(project.id, brief))
    db.commit()
    return project


@pytest.fixture
def project(db, pm) -> Project:
    return _project(db)


def _url(project: Project, suffix: str = "") -> str:
    return f"{PROJECTS}/{project.id}/plan{suffix}"


def _plan(web: TestClient, project: Project) -> dict:
    response = web.get(_url(project))
    assert response.status_code == 200, response.text
    return response.json()


def _add(web: TestClient, project: Project, kind: str, **body) -> dict:
    version = _plan(web, project)["version"]
    response = web.post(_url(project, f"/{kind}"), json={"version": version, **body})
    assert response.status_code == 201, response.text
    return response.json()


def _audit(db: Session) -> list[AuditDecision]:
    db.expire_all()
    return list(
        db.scalars(
            select(AuditDecision).where(AuditDecision.agent == "project").order_by(AuditDecision.id)
        )
    )


def _complete(web: TestClient, project: Project, owner: User) -> None:
    milestone = _add(
        web, project, "milestones", name="Pilot", target_date="2027-03-01", owner_user_id=owner.id
    )
    _add(web, project, "deliverables", name="Forecast model", milestone_id=milestone["id"])
    _add(web, project, "estimate-lines", description="Team", category="people", amount="1000.00")
    version = _plan(web, project)["version"]
    response = web.patch(
        _url(project), json={"version": version, "report_period": "weekly", "report_due_day": 5}
    )
    assert response.status_code == 200, response.text


# --- The migration


def test_the_migration_gives_every_existing_project_a_plan(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'plans.db'}")
    get_settings.cache_clear()
    try:
        config = Config(str(API_DIR / "alembic.ini"))
        command.upgrade(config, "0015")
        migrated = create_engine(get_settings().database_url)
        with migrated.begin() as conn:
            for pid in (1, 2):
                conn.execute(
                    text(
                        "INSERT INTO projects (id, name, stage, sponsor, pm, status, created_at)"
                        " VALUES (:id, 'P', 'planning', 'S', 'M', 'on_track', '2026-11-01')"
                    ),
                    {"id": pid},
                )
            conn.execute(
                text(
                    "INSERT INTO project_briefs (project_id, purpose, objectives, scope_in,"
                    " scope_out, decision_rights, version, created_at, updated_at) VALUES"
                    " (1, 'Why', 'What', 'In', 'Out', '[]', 1, '2026-11-01', '2026-11-01')"
                )
            )
        command.upgrade(config, "0016")
        with migrated.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT project_id, scope_in, scope_out, contingency_pct, report_period,"
                    " report_due_day, version FROM project_plans ORDER BY project_id"
                )
            ).all()
        assert [tuple(r) for r in rows] == [
            (1, "In", "Out", 0, None, None, 1),
            (2, None, None, 0, None, None, 1),
        ]
        command.downgrade(config, "0015")
        migrated.dispose()
    finally:
        get_settings.cache_clear()


# --- The total


@pytest.mark.parametrize(
    ("amounts", "pct", "total"),
    [
        ([], 0, "0.00"),
        (["100.00", "50.50"], 0, "150.50"),
        (["100.00"], 10, "110.00"),
        # 0.05 * 1.10 = 0.055, rounded half-up.
        (["0.05"], 10, "0.06"),
        (["333.33"], 15, "383.33"),
        (["0.01"], 50, "0.02"),
    ],
)
def test_the_total_is_lines_plus_contingency_rounded_half_up(amounts, pct, total):
    assert str(compute_total([Decimal(a) for a in amounts], pct)) == total


@pytest.mark.parametrize(
    ("total", "low", "high", "fit"),
    [
        ("1500", "1000", "2000", "within"),
        ("1000", "1000", "2000", "within"),
        ("2000", "1000", "2000", "within"),
        ("999.99", "1000", "2000", "below"),
        ("2000.01", "1000", "2000", "above"),
        ("10", None, "2000", "within"),
        ("2000.01", None, "2000", "above"),
        ("999.99", "1000", None, "below"),
        ("1000", "1000", None, "within"),
        ("99999", "1000", None, "within"),
        ("10", None, None, "no_range"),
    ],
)
def test_budget_fit(total, low, high, fit):
    dec = lambda v: Decimal(v) if v is not None else None  # noqa: E731
    assert budget_fit(Decimal(total), dec(low), dec(high)) == fit


def test_the_api_shows_the_total_against_the_budget_range(web, db, pm, project):
    plan = _plan(web, project)
    assert (plan["total"], plan["budget_fit"]) == ("0.00", "below")
    assert (plan["budget_low"], plan["budget_high"]) == ("1000.00", "2000.00")
    _add(web, project, "estimate-lines", description="Team", category="people", amount="1200")
    line = _add(
        web, project, "estimate-lines", description="Licence", category="software", amount=300
    )
    version = _plan(web, project)["version"]
    assert (
        web.patch(_url(project), json={"version": version, "contingency_pct": 50}).status_code
        == 200
    )
    plan = _plan(web, project)
    assert (plan["lines_total"], plan["contingency_amount"], plan["total"]) == (
        "1500.00",
        "750.00",
        "2250.00",
    )
    assert plan["budget_fit"] == "above"
    assert line["amount"] == "300.00"
    lines = web.get(_url(project, "/estimate-lines")).json()
    assert [i["amount"] for i in lines["items"]] == ["1200.00", "300.00"]


def test_no_budget_is_no_range(web, db, pm):
    project = _project(db, low=None, high=None)
    assert _plan(web, project)["budget_fit"] == "no_range"


def test_a_brief_with_only_a_low_bound_is_compared_against_it(web, db, pm):
    project = _project(db, low="1000", high=None)
    assert _plan(web, project)["budget_fit"] == "below"
    _add(web, project, "estimate-lines", description="Team", category="people", amount="5000")
    plan = _plan(web, project)
    assert (plan["budget_low"], plan["budget_high"], plan["budget_fit"]) == (
        "1000.00",
        None,
        "within",
    )


def test_a_brief_with_only_a_high_bound_is_compared_against_it(web, db, pm):
    project = _project(db, low=None, high="2000")
    assert _plan(web, project)["budget_fit"] == "within"
    _add(web, project, "estimate-lines", description="Team", category="people", amount="2000.01")
    plan = _plan(web, project)
    assert (plan["budget_low"], plan["budget_high"], plan["budget_fit"]) == (
        None,
        "2000.00",
        "above",
    )


@pytest.mark.parametrize("kind", ["plan", "line"])
def test_a_total_from_the_client_is_refused(web, db, pm, project, kind):
    version = _plan(web, project)["version"]
    if kind == "plan":
        response = web.patch(_url(project), json={"version": version, "total": "1.00"})
    else:
        response = web.post(
            _url(project, "/estimate-lines"),
            json={
                "version": version,
                "description": "X",
                "category": "other",
                "amount": "5",
                "total": "9",
            },
        )
    assert response.status_code == 422
    assert _audit(db) == []


@pytest.mark.parametrize("amount", ["0", "0.00", "-1", "1.005", "abc"])
def test_an_amount_must_be_above_zero_with_two_places(web, db, pm, project, amount):
    version = _plan(web, project)["version"]
    response = web.post(
        _url(project, "/estimate-lines"),
        json={"version": version, "description": "X", "category": "other", "amount": amount},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("pct", [-1, 51, 10.5, "10"])
def test_contingency_is_a_whole_number_from_0_to_50(web, db, pm, project, pct):
    response = web.patch(_url(project), json={"version": 1, "contingency_pct": pct})
    assert response.status_code == 422


# --- Missing


def test_a_new_plan_lists_every_gap(web, db, pm):
    project = _project(db)
    db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id)).scope_in = None
    db.commit()
    assert _plan(web, project)["missing"] == [
        "Scope in is empty",
        "No deliverables",
        "No milestones",
        "No estimate lines",
        "No reporting plan",
    ]


def test_a_complete_plan_misses_nothing(web, db, pm, project):
    _complete(web, project, pm)
    assert _plan(web, project)["missing"] == []


def test_owner_and_milestone_gaps_are_named(web, db, pm, project, sign_in):
    _complete(web, project, pm)
    _add(web, project, "milestones", name="Go live", target_date="2027-06-01")
    _add(web, project, "deliverables", name="Runbook")
    gone = User(
        email="gone@pmo.example", display_name="Gone", password_hash="x", role="project_manager"
    )
    db.add(gone)
    db.commit()
    _add(
        web, project, "milestones", name="Handover", target_date="2027-07-01", owner_user_id=gone.id
    )
    gone.is_active = False
    db.commit()
    assert _plan(web, project)["missing"] == [
        "Milestone 'Go live' has no owner",
        "Milestone 'Handover': its owner is no longer active",
        "Deliverable 'Runbook' has no milestone",
    ]
    milestones = web.get(_url(project, "/milestones")).json()["items"]
    assert [(m["name"], m["owner_active"]) for m in milestones] == [
        ("Pilot", True),
        ("Go live", False),
        ("Handover", False),
    ]


def test_a_period_without_a_due_day_is_no_reporting_plan(web, db, pm, project):
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    plan.report_period = "monthly"
    db.commit()
    assert "No reporting plan" in _plan(web, project)["missing"]


# --- Milestones and deliverables


def test_milestones_are_listed_by_target_date_then_id_and_paged(web, db, pm, project):
    for name, when in (("C", "2027-05-01"), ("A", "2027-01-01"), ("B", "2027-05-01")):
        _add(web, project, "milestones", name=name, target_date=when, owner_user_id=pm.id)
    page = web.get(_url(project, "/milestones"), params={"limit": 2}).json()
    assert ([m["name"] for m in page["items"]], page["total"]) == (["A", "C"], 3)
    page = web.get(_url(project, "/milestones"), params={"limit": 2, "offset": 2}).json()
    assert [m["name"] for m in page["items"]] == ["B"]
    assert page["items"][0]["owner_name"] == pm.display_name


def test_a_milestone_owner_must_be_an_active_user(web, db, pm, project):
    gone = User(email="g@pmo.example", display_name="G", password_hash="x", role="auditor")
    gone.is_active = False
    db.add(gone)
    db.commit()
    for owner in (gone.id, 999):
        response = web.post(
            _url(project, "/milestones"),
            json={"version": 1, "name": "M", "target_date": "2027-01-01", "owner_user_id": owner},
        )
        assert (response.status_code, response.json()["detail"]) == (422, BAD_OWNER)


def test_a_deliverable_milestone_must_be_this_plans(web, db, pm, project):
    other = _project(db)
    theirs = _add(web, other, "milestones", name="Theirs", target_date="2027-01-01")
    for milestone_id in (theirs["id"], 999):
        response = web.post(
            _url(project, "/deliverables"),
            json={"version": 1, "name": "D", "milestone_id": milestone_id},
        )
        assert (response.status_code, response.json()["detail"]) == (422, BAD_MILESTONE)


def test_deleting_a_milestone_unassigns_its_deliverables(web, db, pm, project):
    milestone = _add(web, project, "milestones", name="Pilot", target_date="2027-01-01")
    first = _add(web, project, "deliverables", name="One", milestone_id=milestone["id"])
    second = _add(web, project, "deliverables", name="Two", milestone_id=milestone["id"])
    before = len(_audit(db))
    response = web.delete(_url(project, f"/milestones/{milestone['id']}"), params={"version": 1})
    assert response.status_code == 204, response.text
    rows = _audit(db)
    assert len(rows) == before + 1
    row = rows[-1]
    assert (row.subject_type, row.subject_id, row.output) == (
        "plan_milestone",
        str(milestone["id"]),
        None,
    )
    assert row.signals["name"] == "Pilot"
    assert row.action_taken["action"] == "delete"
    assert row.action_taken["unassigned_deliverable_ids"] == [first["id"], second["id"]]
    db.expire_all()
    assert [d.milestone_id for d in db.scalars(select(PlanDeliverable))] == [None, None]
    assert "Deliverable 'One' has no milestone" in _plan(web, project)["missing"]


def test_deliverables_and_lines_are_edited_and_deleted(web, db, pm, project):
    deliverable = _add(web, project, "deliverables", name="Model")
    response = web.patch(
        _url(project, f"/deliverables/{deliverable['id']}"),
        json={"version": 1, "description": "A weekly forecast"},
    )
    assert (response.status_code, response.json()["version"]) == (200, 2)
    line = _add(web, project, "estimate-lines", description="Team", category="people", amount=10)
    response = web.patch(
        _url(project, f"/estimate-lines/{line['id']}"), json={"version": 1, "amount": "12.5"}
    )
    assert response.json()["amount"] == "12.50"
    assert _plan(web, project)["total"] == "12.50"
    for path in (f"/deliverables/{deliverable['id']}", f"/estimate-lines/{line['id']}"):
        assert web.delete(_url(project, path), params={"version": 2}).status_code == 204
    assert web.get(_url(project, "/deliverables")).json()["total"] == 0
    assert _plan(web, project)["total"] == "0.00"


def test_a_stale_plan_says_the_plan_changed_and_intake_keeps_its_message():
    assert STALE == "This plan has changed since you loaded it. Reload and try again."
    assert intake.STALE == "this request has changed since you read it; reload it and try again"


# --- The reporting plan


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"report_period": "weekly"}, PERIOD_NEEDS_DAY),
        ({"report_period": "weekly", "report_due_day": None}, PERIOD_NEEDS_DAY),
        ({"report_period": "weekly", "report_due_day": 8}, BAD_DUE_DAY),
        ({"report_period": "fortnightly", "report_due_day": 8}, BAD_DUE_DAY),
        ({"report_due_day": 3}, DAY_NEEDS_PERIOD),
    ],
)
def test_the_due_day_must_fit_the_period(web, db, pm, project, body, detail):
    response = web.patch(_url(project), json={"version": 1, **body})
    assert (response.status_code, response.json()["detail"]) == (422, detail)


def test_changing_the_period_needs_a_due_day_that_fits_it(web, db, pm, project):
    ok = web.patch(
        _url(project), json={"version": 1, "report_period": "monthly", "report_due_day": 28}
    )
    assert ok.status_code == 200, ok.text
    # 28 is no weekday, and the period can't change without a new due day.
    response = web.patch(_url(project), json={"version": 2, "report_period": "weekly"})
    assert response.json()["detail"] == PERIOD_NEEDS_DAY
    response = web.patch(
        _url(project), json={"version": 2, "report_period": "weekly", "report_due_day": 28}
    )
    assert response.json()["detail"] == BAD_DUE_DAY
    response = web.patch(
        _url(project), json={"version": 2, "report_period": "weekly", "report_due_day": 1}
    )
    assert (response.status_code, response.json()["report_due_day"]) == (200, 1)
    for day in (0, 29):
        response = web.patch(_url(project), json={"version": 3, "report_due_day": day})
        assert response.status_code == 422


@pytest.mark.parametrize("extra", [{}, {"report_due_day": None}])
def test_clearing_the_period_clears_the_due_day(web, db, pm, project, extra):
    ok = web.patch(
        _url(project), json={"version": 1, "report_period": "monthly", "report_due_day": 28}
    )
    assert ok.status_code == 200, ok.text
    response = web.patch(_url(project), json={"version": 2, "report_period": None, **extra})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["report_period"], body["report_due_day"]) == (None, None)
    assert "No reporting plan" in body["missing"]


def test_clearing_the_period_with_a_due_day_is_refused(web, db, pm, project):
    web.patch(_url(project), json={"version": 1, "report_period": "weekly", "report_due_day": 5})
    response = web.patch(
        _url(project), json={"version": 2, "report_period": None, "report_due_day": 3}
    )
    assert (response.status_code, response.json()["detail"]) == (422, DAY_NEEDS_PERIOD)


def test_resending_the_same_period_needs_no_due_day(web, db, pm, project):
    web.patch(_url(project), json={"version": 1, "report_period": "weekly", "report_due_day": 5})
    response = web.patch(_url(project), json={"version": 2, "report_period": "weekly"})
    assert response.status_code == 200, response.text
    assert response.json()["report_due_day"] == 5


# --- Write rules


def _writes(ids: dict[str, int], item: int = 1) -> list[tuple[str, str, dict | None]]:
    """One of each write. Items are edited at version `item`, then deleted at `item + 1`."""
    m, d, line, gone = ids["milestone"], ids["deliverable"], ids["line"], item + 1
    return [
        ("PATCH", "", {"version": ids["plan"], "scope_out": "None"}),
        ("POST", "/milestones", {"version": ids["plan"], "name": "M", "target_date": "2027-01-01"}),
        ("PATCH", f"/milestones/{m}", {"version": item, "name": "M2"}),
        ("DELETE", f"/milestones/{m}?version={gone}", None),
        ("POST", "/deliverables", {"version": ids["plan"], "name": "D"}),
        ("PATCH", f"/deliverables/{d}", {"version": item, "name": "D2"}),
        ("DELETE", f"/deliverables/{d}?version={gone}", None),
        (
            "POST",
            "/estimate-lines",
            {"version": ids["plan"], "description": "L", "category": "other", "amount": "1"},
        ),
        ("PATCH", f"/estimate-lines/{line}", {"version": item, "amount": "2"}),
        ("DELETE", f"/estimate-lines/{line}?version={gone}", None),
    ]


def _items(web: TestClient, project: Project) -> dict[str, int]:
    milestone = _add(web, project, "milestones", name="Pilot", target_date="2027-01-01")
    deliverable = _add(web, project, "deliverables", name="Model")
    line = _add(web, project, "estimate-lines", description="Team", category="people", amount=1)
    return {
        "milestone": milestone["id"],
        "deliverable": deliverable["id"],
        "line": line["id"],
        "plan": _plan(web, project)["version"],
    }


@pytest.mark.parametrize("stage", ["delivery", "closed"])
def test_every_write_outside_planning_is_409(web, db, pm, project, stage):
    ids = _items(web, project)
    db.get(Project, project.id).stage = stage
    db.commit()
    before = len(_audit(db))
    for method, path, body in _writes(ids):
        response = web.request(method, _url(project, path), json=body)
        assert (response.status_code, response.json()["detail"]) == (409, NOT_PLANNING), path
    assert len(_audit(db)) == before
    assert _plan(web, project)["editable"] is False


def test_every_write_with_a_stale_version_is_409(web, db, pm, project):
    ids = _items(web, project)
    stale = {**ids, "plan": ids["plan"] - 1}
    before = len(_audit(db))
    for method, path, body in _writes(stale, item=5):
        response = web.request(method, _url(project, path), json=body)
        assert (response.status_code, response.json()["detail"]) == (409, STALE), path
    assert len(_audit(db)) == before


def test_each_write_is_one_audit_row_with_before_and_after(web, db, pm, project):
    ids = _items(web, project)
    rows = _audit(db)
    assert [(r.subject_type, r.action_taken["action"]) for r in rows] == [
        ("plan_milestone", "create"),
        ("plan_deliverable", "create"),
        ("plan_estimate_line", "create"),
    ]
    assert all(r.actor_user_id == pm.id and r.signals is None for r in rows)
    plan_version = ids["plan"]
    plan_id = db.scalar(select(ProjectPlan.id).where(ProjectPlan.project_id == project.id))
    for method, path, body in _writes(ids):
        if method == "POST" or not path:
            body = {**body, "version": plan_version}
        count = len(_audit(db))
        response = web.request(method, _url(project, path), json=body)
        assert response.status_code in (200, 201, 204), (path, response.text)
        rows = _audit(db)
        assert len(rows) == count + 1, path
        row = rows[-1]
        assert row.action_taken["project_id"] == project.id
        assert row.action_taken["plan_id"] == plan_id
        if method == "PATCH":
            assert row.signals is not None and row.output is not None
        plan_version = _plan(web, project)["version"]
    plan_row = _audit(db)[3]
    assert plan_row.subject_type == "project_plan"
    assert (plan_row.signals["scope_out"], plan_row.output["scope_out"]) == ("Stores", "None")


def test_a_write_that_changes_nothing_writes_no_audit_row(web, db, pm, project):
    response = web.patch(_url(project), json={"version": 1, "scope_in": "Two warehouses"})
    assert (response.status_code, response.json()["version"]) == (200, 1)
    assert _audit(db) == []


def test_every_write_moves_the_plan_version_on(web, db, pm, project):
    assert _plan(web, project)["version"] == 1
    _add(web, project, "milestones", name="Pilot", target_date="2027-01-01")
    assert _plan(web, project)["version"] == 2


def test_a_sponsor_reads_but_does_not_write(db, project, sign_in):
    sponsor = TestClient(app, base_url="http://localhost")
    sign_in(sponsor, db, "sponsor")
    assert sponsor.get(_url(project)).status_code == 200
    assert sponsor.patch(_url(project), json={"version": 1, "scope_out": "X"}).status_code == 403


def test_another_project_manager_may_edit_and_is_recorded(web, db, project, sign_in):
    other = TestClient(app, base_url="http://localhost")
    user = sign_in(other, db, "project_manager", email="other.pm@pmo.example")
    assert other.patch(_url(project), json={"version": 1, "scope_out": "X"}).status_code == 200
    assert _audit(db)[-1].actor_user_id == user.id


def test_an_unknown_project_or_item_is_404(web, pm, project):
    assert web.get(f"{PROJECTS}/999/plan").status_code == 404
    response = web.patch(_url(project, "/milestones/999"), json={"version": 1, "name": "X"})
    assert response.status_code == 404
