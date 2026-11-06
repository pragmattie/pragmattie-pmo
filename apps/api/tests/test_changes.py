"""Change requests (item 5.5): the policy loader, the effect and decider computed from the
baseline, who may decide, applying an approved change atomically, and every refusal audited."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from policy_files import changes_dir
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import policy
from app.api import changes as change_api
from app.authz import FORBIDDEN
from app.changes import compute_effect, working_days
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    ChangeRequest,
    Client,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectBaseline,
    ProjectBrief,
    ProjectPlan,
    ProjectProposal,
    User,
)
from app.policy import ChangesPolicy, PolicyError, load_changes, parse_changes
from app.proposals import plan_facts, refresh_from_plan

GATE_REASON = "The plan is complete and inside the budget"
TICKS = [
    {"id": "team_and_dates_realistic", "met": True, "note": "Owners agreed every date"},
    {"id": "risks_acceptable", "met": True, "note": "Nothing open we can't carry"},
]
REASON = "Agreed with the client in the weekly review"
# The baseline's milestone date: a Monday.
PILOT = date(2027, 3, 1)


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


def _client(db, sign_in, role: str, email: str | None = None) -> tuple[TestClient, User]:
    client = TestClient(app, base_url="http://localhost")
    return client, sign_in(client, db, role, email)


@pytest.fixture
def people(db, sign_in, gates_policy):
    """The project's project manager, another project manager who raises changes, a sponsor and
    an ethics reviewer, each with their own signed-in client."""
    return {
        "pm": _client(db, sign_in, "project_manager"),
        "raiser": _client(db, sign_in, "project_manager", "raiser@pmo.example"),
        "sponsor": _client(db, sign_in, "sponsor"),
        "ethics": _client(db, sign_in, "ethics_reviewer"),
        "auditor": _client(db, sign_in, "auditor"),
    }


def _planning_project(db: Session, pm: User, *, client: bool = False) -> int:
    """A project in planning managed by `pm` with a complete plan totalling 1000.00 (one line, no
    contingency), one milestone on `PILOT` and one deliverable."""
    client_id = None
    if client:
        row = Client(name="Quayside Example")
        db.add(row)
        db.flush()
        client_id = row.id
    project = Project(
        name="Forecast",
        client_id=client_id,
        stage="planning",
        sponsor="S",
        pm="P",
        pm_user_id=pm.id,
        status="on_track",
    )
    db.add(project)
    db.flush()
    db.add(
        ProjectBrief(
            project_id=project.id,
            purpose="Fewer stock-outs",
            objectives="Forecast weekly demand",
            decision_rights=[],
            budget_high="5000.00",
        )
    )
    plan = ProjectPlan(
        project_id=project.id,
        scope_in="Weekly forecast for the pilot stores",
        scope_out="Other stores",
        contingency_pct=0,
        report_period="weekly",
        report_due_day=5,
    )
    db.add(plan)
    db.flush()
    milestone = PlanMilestone(
        plan_id=plan.id, name="Pilot live", target_date=PILOT, owner_user_id=pm.id
    )
    db.add(milestone)
    db.flush()
    db.add_all(
        [
            PlanDeliverable(plan_id=plan.id, name="Forecast model", milestone_id=milestone.id),
            PlanEstimateLine(
                plan_id=plan.id, description="Analysts", category="people", amount="1000.00"
            ),
        ]
    )
    if client:
        db.flush()
        proposal = ProjectProposal(project_id=project.id, status="accepted", created_by=pm.id)
        refresh_from_plan(proposal, plan_facts(db, plan))
        db.add(proposal)
    db.commit()
    return project.id


@pytest.fixture
def project(db, people) -> int:
    """A project in delivery with baseline 1, passed at the go-ahead gate by the sponsor."""
    return _in_delivery(db, people)


def _in_delivery(db: Session, people, *, client: bool = False) -> int:
    pid = _planning_project(db, people["pm"][1], client=client)
    response = people["sponsor"][0].post(
        f"/api/v1/projects/{pid}/gate",
        json={"version": 1, "outcome": "pass", "reason": GATE_REASON, "criteria": TICKS},
    )
    assert response.status_code == 200, response.text
    return pid


def _ids(db: Session, pid: int) -> dict[str, int]:
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == pid))
    return {
        "milestone": db.scalar(select(PlanMilestone.id).where(PlanMilestone.plan_id == plan.id)),
        "deliverable": db.scalar(
            select(PlanDeliverable.id).where(PlanDeliverable.plan_id == plan.id)
        ),
        "line": db.scalar(select(PlanEstimateLine.id).where(PlanEstimateLine.plan_id == plan.id)),
    }


def _url(pid: int, cid: int | None = None, action: str = "") -> str:
    url = f"/api/v1/projects/{pid}/changes"
    if cid is not None:
        url += f"/{cid}"
    return url + (f"/{action}" if action else "")


def _raise(client: TestClient, pid: int, changes: dict, **extra) -> dict:
    body = {"title": "Extra work", "reason": REASON, "changes": changes, **extra}
    response = client.post(_url(pid), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _submitted(client: TestClient, pid: int, changes: dict) -> dict:
    change = _raise(client, pid, changes)
    response = client.post(_url(pid, change["id"], "submit"), json={"version": change["version"]})
    assert response.status_code == 200, response.text
    return response.json()


def _decide(client: TestClient, pid: int, change: dict, outcome: str = "approve"):
    return client.post(
        _url(pid, change["id"], "decide"),
        json={"version": change["version"], "outcome": outcome, "reason": REASON},
    )


def _rows(db: Session, **filters) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(AuditDecision.agent == "change")
    for name, value in filters.items():
        stmt = stmt.where(getattr(AuditDecision, name) == value)
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def _refusals(db: Session) -> list[str]:
    return [r.action_taken["refusal"] for r in _rows(db, status="rejected")]


def _plan_state(db: Session, pid: int) -> dict:
    db.expire_all()
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == pid))
    return {
        "version": plan.version,
        "scope_out": plan.scope_out,
        "contingency": plan.contingency_pct,
        "dates": [
            m.target_date
            for m in db.scalars(select(PlanMilestone).where(PlanMilestone.plan_id == plan.id))
        ],
        "deliverables": sorted(
            db.scalars(select(PlanDeliverable.name).where(PlanDeliverable.plan_id == plan.id))
        ),
        "amounts": sorted(
            db.scalars(select(PlanEstimateLine.amount).where(PlanEstimateLine.plan_id == plan.id))
        ),
    }


def _baselines(db: Session, pid: int) -> list[ProjectBaseline]:
    db.expire_all()
    stmt = select(ProjectBaseline).where(ProjectBaseline.project_id == pid)
    return list(db.scalars(stmt.order_by(ProjectBaseline.number)))


def _line(amount: str) -> dict:
    return {"add_lines": [{"description": "More analysis", "category": "people", "amount": amount}]}


# --- The policy file


def test_the_loader_accepts_the_proposed_file():
    loaded = load_changes(changes_dir())
    assert loaded == ChangesPolicy(
        max_cost_increase_pct=Decimal("5"), max_date_move_working_days=5, escalate_to="sponsor"
    )


GOOD = {
    "version": 1,
    "project_manager": {"max_cost_increase_pct": 5, "max_date_move_working_days": 5},
    "escalate_to": "sponsor",
}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"extra": 1}, "unknown keys: extra"),
        ({"escalate_to": None}, "unknown role None"),
        ({"escalate_to": "project_manager"}, "unknown role 'project_manager'"),
        ({"project_manager": []}, "expected a mapping"),
        ({"project_manager": {"max_cost_increase_pct": 5}}, "missing keys"),
        (
            {"project_manager": {"max_cost_increase_pct": 5, "max_date_move_working_days": True}},
            "max_date_move_working_days: expected a whole number",
        ),
        (
            {"project_manager": {"max_cost_increase_pct": 5, "max_date_move_working_days": -1}},
            "max_date_move_working_days: expected a whole number",
        ),
        (
            {"project_manager": {"max_cost_increase_pct": "5", "max_date_move_working_days": 5}},
            "max_cost_increase_pct: expected a number",
        ),
        (
            {"project_manager": {"max_cost_increase_pct": -1, "max_date_move_working_days": 5}},
            "max_cost_increase_pct: expected a number",
        ),
        (
            {"project_manager": {"max_cost_increase_pct": 5.125, "max_date_move_working_days": 5}},
            "at most 2 decimal places",
        ),
        (
            {
                "project_manager": {
                    "max_cost_increase_pct": 5,
                    "max_date_move_working_days": 5,
                    "scope": True,
                }
            },
            "unknown keys: scope",
        ),
    ],
)
def test_the_loader_refuses_a_bad_file_clearly(change, message):
    with pytest.raises(PolicyError, match="changes.yaml") as raised:
        parse_changes({**GOOD, **change})
    assert message in str(raised.value)


def test_a_missing_or_wrong_version_file_is_refused(tmp_path: Path):
    with pytest.raises(PolicyError, match="changes.yaml: policy file not found"):
        load_changes(tmp_path)
    (tmp_path / "changes.yaml").write_text("version: 2\n")
    with pytest.raises(PolicyError, match="version must be 1"):
        load_changes(tmp_path)


def test_a_decimal_limit_is_kept_exactly():
    data = {**GOOD, "project_manager": {**GOOD["project_manager"], "max_cost_increase_pct": 2.5}}
    assert parse_changes(data).max_cost_increase_pct == Decimal("2.5")


def test_without_the_policy_every_change_route_is_503(db, people, project, monkeypatch):
    def missing():
        raise PolicyError("changes.yaml: policy file not found")

    monkeypatch.setattr(policy, "get_changes", missing)
    pm = people["pm"][0]
    assert pm.get(_url(project)).status_code == 503
    assert (
        pm.post(
            _url(project), json={"title": "x", "reason": "y", "changes": _line("1.00")}
        ).status_code
        == 503
    )
    # Other routes keep working.
    assert pm.get(f"/api/v1/projects/{project}/plan").status_code == 200


# --- Working days and the effect


@pytest.mark.parametrize(
    ("start", "end", "days"),
    [
        (date(2027, 3, 5), date(2027, 3, 8), 1),  # Friday to Monday
        (date(2027, 3, 1), date(2027, 3, 8), 5),  # a week
        (date(2027, 3, 1), date(2027, 3, 9), 6),
        (date(2027, 3, 6), date(2027, 3, 7), 0),  # Saturday to Sunday
        (date(2027, 3, 8), date(2027, 3, 1), -5),
        (date(2027, 3, 1), date(2027, 3, 1), 0),
    ],
)
def test_working_days_count_monday_to_friday(start, end, days):
    assert working_days(start, end) == days


RULES = ChangesPolicy(Decimal("5"), 5, "sponsor")


def _baseline(total: str = "1000.00") -> dict:
    return {
        "scope_in": "In",
        "scope_out": "Out",
        "contingency_pct": 0,
        "total": total,
        "milestones": [{"id": 1, "name": "M", "target_date": "2027-03-01"}],
        "deliverables": [{"id": 1, "name": "D"}],
        "estimate_lines": [{"id": 1, "description": "L", "amount": total}],
    }


def _set(**changes) -> dict:
    empty = {
        "scope_in": None,
        "scope_out": None,
        "add_deliverables": [],
        "remove_deliverables": [],
        "move_milestones": [],
        "add_lines": [],
        "remove_lines": [],
        "change_lines": [],
        "contingency_pct": None,
    }
    return {**empty, **changes}


def _effect(baseline: dict, changes: dict, **who) -> tuple[dict, str]:
    who = {"has_project_manager": True, "raised_by_project_manager": False, **who}
    return compute_effect(baseline, changes, RULES, baseline_number=1, **who)


def test_any_cost_increase_on_a_zero_baseline_goes_to_the_sponsor():
    effect, decider = _effect(
        _baseline("0.00"),
        _set(add_lines=[{"description": "x", "category": "other", "amount": "0.01"}]),
    )
    assert decider == "sponsor"
    assert effect["cost_difference_pct"] is None
    assert effect["escalations"] == ["cost_increase_on_zero_baseline"]


def test_no_project_manager_or_one_who_raised_it_sends_it_to_the_sponsor():
    small = _set(contingency_pct=1)
    assert _effect(_baseline(), small)[1] == "project_manager"
    effect, decider = _effect(_baseline(), small, has_project_manager=False)
    assert (decider, effect["escalations"]) == ("sponsor", ["no_project_manager"])
    effect, decider = _effect(_baseline(), small, raised_by_project_manager=True)
    assert (decider, effect["escalations"]) == ("sponsor", ["raised_by_project_manager"])


def test_scope_text_set_to_what_the_baseline_has_is_not_a_scope_change():
    effect, decider = _effect(_baseline(), _set(scope_in="In"))
    assert (effect["scope_changes"], decider) == (False, "project_manager")


# --- Thresholds through the API: measured against baseline 1, raised by another project manager


def _move(days_later: int) -> dict:
    target = {5: "2027-03-08", 6: "2027-03-09", -10: "2027-02-15"}[days_later]
    return {"move_milestones": [{"milestone_id": "{milestone}", "target_date": target}]}


CASES = [
    ("exactly 5%", _line("50.00"), "project_manager", [], "50.00", "5.00"),
    ("just over 5%", _line("50.10"), "sponsor", ["cost_above_limit"], "50.10", "5.01"),
    ("5 working days later", _move(5), "project_manager", [], "0.00", "0.00"),
    ("6 working days later", _move(6), "sponsor", ["dates_above_limit"], "0.00", "0.00"),
    ("10 working days earlier", _move(-10), "project_manager", [], "0.00", "0.00"),
    (
        "a cost decrease",
        {"change_lines": [{"line_id": "{line}", "amount": "600.00"}]},
        "project_manager",
        [],
        "-400.00",
        "-40.00",
    ),
    ("contingency of 5%", {"contingency_pct": 5}, "project_manager", [], "50.00", "5.00"),
    ("contingency of 6%", {"contingency_pct": 6}, "sponsor", ["cost_above_limit"], "60.00", "6.00"),
    ("scope out text", {"scope_out": "Every store"}, "sponsor", ["scope_change"], "0.00", "0.00"),
    (
        "a deliverable added",
        {"add_deliverables": [{"name": "Dashboard", "milestone_id": "{milestone}"}]},
        "sponsor",
        ["scope_change"],
        "0.00",
        "0.00",
    ),
    (
        "a deliverable removed",
        {"remove_deliverables": ["{deliverable}"]},
        "sponsor",
        ["scope_change"],
        "0.00",
        "0.00",
    ),
    (
        "a cost decrease with a 6-day move",
        {
            "change_lines": [{"line_id": "{line}", "amount": "600.00"}],
            **_move(6),
        },
        "sponsor",
        ["dates_above_limit"],
        "-400.00",
        "-40.00",
    ),
]


def _fill(value, ids: dict[str, int]):
    """The change set with `{milestone}`, `{deliverable}` and `{line}` replaced by real ids."""
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, ids) for v in value]
    if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
        return ids[value[1:-1]]
    return value


@pytest.mark.parametrize(
    ("changes", "decider", "escalations", "difference", "percent"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_the_effect_and_decider_are_computed_from_the_baseline(
    db, people, project, changes, decider, escalations, difference, percent
):
    change = _raise(people["raiser"][0], project, _fill(changes, _ids(db, project)))
    assert change["decider"] == decider
    effect = change["effect"]
    assert effect["escalations"] == escalations
    assert (effect["cost_difference"], effect["cost_difference_pct"]) == (difference, percent)
    assert effect["baseline_number"] == change["measured_baseline_number"] == 1
    assert effect["baseline_total"] == "1000.00"


def test_date_moves_are_reported_in_working_days(db, people, project):
    ids = _ids(db, project)
    later = _raise(people["raiser"][0], project, _fill(_move(6), ids))["effect"]
    earlier = _raise(people["raiser"][0], project, _fill(_move(-10), ids))["effect"]
    assert (later["max_later_working_days"], later["max_earlier_working_days"]) == (6, 0)
    assert (earlier["max_later_working_days"], earlier["max_earlier_working_days"]) == (0, 10)


@pytest.mark.parametrize(
    "extra",
    [
        {"effect": {"cost_difference": "0.00"}},
        {"decider": "project_manager"},
        {"measured_baseline_number": 9},
        {"status": "approved"},
    ],
)
def test_computed_fields_sent_by_the_client_are_refused(db, people, project, extra):
    body = {"title": "Extra work", "reason": REASON, "changes": _line("500.00"), **extra}
    response = people["raiser"][0].post(_url(project), json=body)
    assert response.status_code == 422
    assert db.scalar(select(ChangeRequest.id)) is None


def test_a_change_set_must_change_something_and_name_baseline_items(db, people, project):
    raiser = people["raiser"][0]
    body = {"title": "Nothing", "reason": REASON, "changes": {}}
    assert raiser.post(_url(project), json=body).status_code == 422
    body["changes"] = {"remove_lines": [999]}
    response = raiser.post(_url(project), json=body)
    assert response.status_code == 422
    assert "estimate line 999 is not in the baseline" in response.json()["detail"]
    line = _ids(db, project)["line"]
    body["changes"] = {
        "remove_lines": [line],
        "change_lines": [{"line_id": line, "amount": "1.00"}],
    }
    assert raiser.post(_url(project), json=body).status_code == 422


def test_a_change_the_project_manager_raised_goes_to_the_sponsor(db, people, project):
    change = _raise(people["pm"][0], project, {"contingency_pct": 1})
    assert change["decider"] == "sponsor"
    assert change["effect"]["escalations"] == ["raised_by_project_manager"]


# --- Who decides


def test_the_project_manager_approves_a_small_change(db, people, project):
    pm, pm_user = people["pm"]
    change = _submitted(people["raiser"][0], project, _line("50.00"))
    before = _plan_state(db, project)
    response = _decide(pm, project, change)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["decided_by"], body["baseline_number"]) == (
        "approved",
        pm_user.id,
        2,
    )
    after = _plan_state(db, project)
    assert after["amounts"] == [Decimal("50.00"), Decimal("1000.00")]
    assert after["version"] > before["version"]
    baselines = _baselines(db, project)
    assert [(b.number, b.change_request_id) for b in baselines] == [(1, None), (2, change["id"])]
    assert baselines[0].gate_decision_id is not None and baselines[1].gate_decision_id is None
    assert baselines[1].total == Decimal("1050.00")
    [row] = _rows(db, status="ok", subject_id=str(change["id"]))[-1:]
    assert row.action_taken["action"] == "change_approved"
    assert row.action_taken["baseline_id"] == baselines[1].id
    assert row.action_taken["decider_level"] == "project_manager"
    assert row.actor_user_id == pm_user.id


def test_a_sponsor_approves_a_scope_change(db, people, project):
    ids = _ids(db, project)
    change = _submitted(
        people["raiser"][0],
        project,
        {
            "add_deliverables": [{"name": "Dashboard", "milestone_id": ids["milestone"]}],
            "remove_deliverables": [ids["deliverable"]],
            "scope_out": "Stores outside the pilot region",
            **_fill(_move(6), ids),
        },
    )
    assert change["decider"] == "sponsor"
    response = _decide(people["sponsor"][0], project, change)
    assert response.status_code == 200, response.text
    state = _plan_state(db, project)
    assert state["deliverables"] == ["Dashboard"]
    assert state["dates"] == [date(2027, 3, 9)]
    assert state["scope_out"] == "Stores outside the pilot region"
    assert _baselines(db, project)[-1].plan["scope_out"] == "Stores outside the pilot region"


def test_rejecting_changes_nothing_but_the_change(db, people, project):
    change = _submitted(people["raiser"][0], project, _line("50.00"))
    before = _plan_state(db, project)
    response = _decide(people["pm"][0], project, change, "reject")
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["baseline_number"]) == ("rejected", None)
    assert _plan_state(db, project) == before
    assert len(_baselines(db, project)) == 1


@pytest.mark.parametrize(
    ("changes", "who", "refusal", "detail"),
    [
        # A project-manager-level change.
        (_line("50.00"), "sponsor", "not_the_project_manager", change_api.NOT_THE_PROJECT_MANAGER),
        (_line("50.00"), "raiser", "own_change", change_api.OWN_CHANGE),
        (_line("50.00"), "ethics", "not_the_project_manager", change_api.NOT_THE_PROJECT_MANAGER),
        (_line("50.00"), "auditor", "no_permission", FORBIDDEN),
        # A sponsor-level change.
        (_line("500.00"), "pm", "no_permission", FORBIDDEN),
        (_line("500.00"), "raiser", "own_change", change_api.OWN_CHANGE),
        (_line("500.00"), "ethics", "role_not_a_decider", "decided only by: sponsor"),
        (_line("500.00"), "auditor", "no_permission", FORBIDDEN),
    ],
)
def test_the_wrong_person_deciding_gets_403_audited(
    db, people, project, changes, who, refusal, detail
):
    change = _submitted(people["raiser"][0], project, changes)
    before = _plan_state(db, project)
    response = _decide(people[who][0], project, change)
    assert response.status_code == 403
    assert detail in response.json()["detail"]
    assert _refusals(db) == [refusal]
    [row] = _rows(db, status="rejected")
    assert (row.subject_type, row.subject_id) == ("change_request", str(change["id"]))
    assert row.actor_user_id == people[who][1].id
    assert _plan_state(db, project) == before
    db.expire_all()
    assert db.get(ChangeRequest, change["id"]).status == "submitted"


def test_a_project_manager_cant_decide_their_own_change(db, people, project):
    """The project's project manager raises it, so it goes to the sponsor, and they still can't
    decide it themselves."""
    pm = people["pm"][0]
    change = _submitted(pm, project, _line("1.00"))
    assert change["decider"] == "sponsor"
    assert _decide(pm, project, change).status_code == 403
    assert _decide(people["sponsor"][0], project, change).status_code == 200


def test_a_sponsor_who_manages_the_project_cant_decide_its_changes(db, people, project):
    sponsor, sponsor_user = people["sponsor"]
    db.get(Project, project).pm_user_id = sponsor_user.id
    db.commit()
    change = _submitted(people["raiser"][0], project, _line("500.00"))
    response = _decide(sponsor, project, change)
    assert response.status_code == 403
    assert _refusals(db) == ["own_project"]


# --- Atomic approval


def test_a_failure_during_approval_changes_nothing(db, people, project, monkeypatch):
    change = _submitted(people["raiser"][0], project, _line("50.00"))
    before = _plan_state(db, project)

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(change_api, "freeze", broken)
    response = _decide(people["pm"][0], project, change)
    assert response.status_code == 500
    assert response.json()["detail"] == change_api.NOT_SAVED
    assert _plan_state(db, project) == before
    assert len(_baselines(db, project)) == 1
    db.expire_all()
    assert db.get(ChangeRequest, change["id"]).status == "submitted"
    assert [r.action_taken["action"] for r in _rows(db, status="ok")] == [
        "change_raised",
        "change_submitted",
    ]


def test_approval_refuses_a_change_that_would_leave_an_invalid_plan(db, people, project):
    ids = _ids(db, project)
    change = _submitted(people["raiser"][0], project, {"remove_deliverables": [ids["deliverable"]]})
    # The deliverable went from the plan some other way since the change was measured.
    db.delete(db.get(PlanDeliverable, ids["deliverable"]))
    db.commit()
    before = _plan_state(db, project)
    response = _decide(people["sponsor"][0], project, change)
    assert response.status_code == 409
    assert "deliverable 'Forecast model' is no longer in the plan" in response.json()["detail"]
    assert _refusals(db) == ["invalid_plan"]
    assert _plan_state(db, project) == before
    assert len(_baselines(db, project)) == 1


def test_a_change_measured_on_an_older_baseline_must_be_resubmitted(db, people, project):
    raiser = people["raiser"][0]
    first = _submitted(raiser, project, _line("40.00"))
    second = _submitted(raiser, project, _line("20.00"))
    assert _decide(people["pm"][0], project, first).status_code == 200
    response = _decide(people["pm"][0], project, second)
    assert response.status_code == 409
    assert response.json()["detail"] == change_api.NEWER_BASELINE
    assert _refusals(db) == ["newer_baseline"]
    # Resubmitting measures it against baseline 2 (1040.00): 20.00 is now 1.92%.
    resubmitted = raiser.post(
        _url(project, second["id"], "submit"), json={"version": second["version"]}
    ).json()
    assert resubmitted["measured_baseline_number"] == 2
    assert resubmitted["effect"]["cost_difference_pct"] == "1.92"
    assert _decide(people["pm"][0], project, resubmitted).status_code == 200
    assert [b.number for b in _baselines(db, project)] == [1, 2, 3]


# --- Lifecycle and refusals


def test_change_requests_are_refused_for_a_project_not_in_delivery(db, people):
    pid = _planning_project(db, people["pm"][1])
    body = {"title": "Extra work", "reason": REASON, "changes": _line("1.00")}
    response = people["pm"][0].post(_url(pid), json=body)
    assert response.status_code == 409
    assert response.json()["detail"] == change_api.NOT_IN_DELIVERY
    [row] = _rows(db, status="rejected")
    assert (row.subject_type, row.action_taken["refusal"]) == ("project", "not_in_delivery")


def test_only_a_draft_can_be_edited_and_edits_are_measured_again(db, people, project):
    raiser = people["raiser"][0]
    change = _raise(raiser, project, _line("50.00"))
    edited = raiser.patch(
        _url(project, change["id"]),
        json={"version": change["version"], "changes": _line("80.00")},
    )
    assert edited.status_code == 200
    assert (edited.json()["decider"], edited.json()["effect"]["cost_difference"]) == (
        "sponsor",
        "80.00",
    )
    stale = raiser.patch(_url(project, change["id"]), json={"version": 1, "title": "Again"})
    assert stale.status_code == 409
    current = edited.json()
    submitted = raiser.post(
        _url(project, change["id"], "submit"), json={"version": current["version"]}
    ).json()
    response = raiser.patch(
        _url(project, change["id"]), json={"version": submitted["version"], "title": "Later"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == change_api.NOT_DRAFT
    assert _refusals(db) == ["stale_version", "not_draft"]


def test_final_states_stay_final(db, people, project):
    change = _submitted(people["raiser"][0], project, _line("10.00"))
    decided = _decide(people["pm"][0], project, change, "reject").json()
    for client, url, body in (
        (
            people["pm"][0],
            _url(project, change["id"], "decide"),
            {"version": decided["version"], "outcome": "approve", "reason": REASON},
        ),
        (
            people["raiser"][0],
            _url(project, change["id"], "submit"),
            {"version": decided["version"]},
        ),
        (
            people["raiser"][0],
            _url(project, change["id"], "withdraw"),
            {"version": decided["version"]},
        ),
    ):
        response = client.post(url, json=body)
        assert response.status_code == 409, url
        assert response.json()["detail"] == change_api.FINAL
    assert _refusals(db) == ["final_status"] * 3


def test_a_decision_needs_a_reason_of_20_characters(db, people, project):
    change = _submitted(people["raiser"][0], project, _line("10.00"))
    response = people["pm"][0].post(
        _url(project, change["id"], "decide"),
        json={"version": change["version"], "outcome": "approve", "reason": "Fine"},
    )
    assert response.status_code == 422


def test_withdrawing_is_for_the_raiser_or_the_project_manager(db, people, project, sign_in):
    stranger, _ = _client(db, sign_in, "project_manager", "stranger@pmo.example")
    change = _submitted(people["raiser"][0], project, _line("10.00"))
    response = stranger.post(_url(project, change["id"], "withdraw"), json={"version": 2})
    assert response.status_code == 403
    assert _refusals(db) == ["not_raiser_or_project_manager"]
    response = people["pm"][0].post(
        _url(project, change["id"], "withdraw"), json={"version": change["version"]}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "withdrawn"


def test_a_role_without_project_write_cant_raise_and_is_audited(db, people, project):
    body = {"title": "Extra work", "reason": REASON, "changes": _line("1.00")}
    response = people["sponsor"][0].post(_url(project), json=body)
    assert response.status_code == 403
    assert _refusals(db) == ["no_permission"]


# --- The change log


def test_the_change_log_lists_newest_first_with_effect_decider_and_baseline(db, people, project):
    raiser = people["raiser"][0]
    first = _submitted(raiser, project, _line("10.00"))
    _decide(people["pm"][0], project, first)
    _raise(raiser, project, _line("20.00"))
    body = people["auditor"][0].get(_url(project)).json()
    assert body["total"] == 2
    assert body["current_baseline_number"] == 2
    newest, oldest = body["items"]
    assert (newest["status"], oldest["status"]) == ("draft", "approved")
    assert oldest["baseline_number"] == 2
    assert oldest["decided_by_name"] == "Project Manager"
    assert oldest["decision_reason"] == REASON
    assert oldest["effect"]["cost_difference"] == "10.00"
    assert oldest["decider"] == "project_manager"
    paged = people["auditor"][0].get(_url(project) + "?limit=1&offset=1").json()
    assert [c["id"] for c in paged["items"]] == [first["id"]]


def test_an_approved_change_leaves_the_proposal_flagged(db, people):
    pid = _in_delivery(db, people, client=True)
    pm = people["pm"][0]
    assert pm.get(_url(pid)).json()["proposal_out_of_date"] is False
    change = _submitted(people["raiser"][0], pid, _line("30.00"))
    assert _decide(pm, pid, change).status_code == 200
    assert pm.get(_url(pid)).json()["proposal_out_of_date"] is True
    flags = pm.get(f"/api/v1/projects/{pid}/proposal").json()["flags"]
    assert "stale_price" in {f["rule"] for f in flags}
    db.expire_all()
    assert db.scalar(select(ProjectProposal.status)) == "accepted"
