"""From approval to project (item 4.6): passing the approval gate creates the project, its brief,
stakeholders and first risks in one transaction; later edits are one audit row each."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_gates import REASON, TICKS, _client, _submitted
from test_intake import URL, _create, _move, add_readiness, add_value_case

from app.api import gates as gates_api
from app.api.gates import ALREADY_A_PROJECT, BAD_PROJECT_MANAGER, NOT_CREATED
from app.api.intake import STALE
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    Client,
    GateDecision,
    Project,
    ProjectAssumption,
    ProjectBrief,
    ProjectRequest,
    ProjectStakeholder,
    ReadinessAction,
    ReadinessCheck,
    Risk,
    User,
)
from app.projects import RESPONSE_PLAN, create_from_request, decision_rights

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
def web(db, gates_policy) -> TestClient:
    """The project manager's client."""
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def pm(web, db, sign_in) -> User:
    return sign_in(web, db, "project_manager")


@pytest.fixture
def sponsor(db, sign_in, pm) -> tuple[TestClient, User]:
    return _client(db, sign_in, "sponsor")


@pytest.fixture
def client_row(db) -> Client:
    row = Client(name="Northwind Example Ltd", contact_name="Robin Contact")
    db.add(row)
    db.commit()
    return row


def _pass(client: TestClient, rid: int, manager_id: int | None, **changes):
    body = {
        "version": 2,
        "outcome": "pass",
        "reason": REASON,
        "criteria": TICKS,
        "project_manager_id": manager_id,
        **changes,
    }
    return client.post(f"{URL}/{rid}/gate", json=body)


def _count(db: Session, model: type) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(model)) or 0


def _nothing_created(db: Session, rid: int) -> None:
    for model in (Project, ProjectBrief, ProjectStakeholder, ProjectAssumption, Risk, GateDecision):
        assert _count(db, model) == 0, model.__name__
    request = db.get(ProjectRequest, rid)
    assert (request.status, request.project_id, request.version) == ("submitted", None, 2)
    actions = db.scalars(select(AuditDecision.action_taken).where(AuditDecision.agent == "gate"))
    assert all(a["action"] != "gate_pass" for a in actions)


def _risks(db: Session, project_id: int) -> list[Risk]:
    db.expire_all()
    return list(db.scalars(select(Risk).where(Risk.project_id == project_id).order_by(Risk.id)))


# --- Passing the gate


def test_passing_the_gate_creates_the_project_and_its_records(web, db, pm, sponsor, client_row):
    sponsor_client, sponsor_user = sponsor
    rid = _create(
        web,
        client_id=client_row.id,
        wanted_by="2027-06-30",
        budget_low="5000",
        budget_high="9000",
        known_risks="Store managers may not trust the forecast",
    ).json()["id"]
    add_value_case(rid, pm.id)
    assert _move(web, rid, "submit", 1).status_code == 200

    response = _pass(sponsor_client, rid, pm.id)
    assert response.status_code == 200, response.text
    project_id = response.json()["project_id"]
    db.expire_all()

    project = db.get(Project, project_id)
    assert (project.name, project.stage, project.status) == (
        "Warehouse demand forecast",
        "planning",
        "on_track",
    )
    assert project.client_id == client_row.id
    assert (project.sponsor, project.sponsor_email, project.sponsor_user_id) == (
        sponsor_user.display_name,
        sponsor_user.email,
        sponsor_user.id,
    )
    assert (project.pm, project.pm_email, project.pm_user_id) == (pm.display_name, pm.email, pm.id)
    request = db.get(ProjectRequest, rid)
    assert (request.status, request.project_id, request.version) == ("approved", project_id, 3)

    brief = db.scalars(select(ProjectBrief).where(ProjectBrief.project_id == project_id)).one()
    assert brief.purpose.startswith("Stock-outs every quarter cost sales")
    assert "Strategic fit: The client's goal of fewer stock-outs" in brief.purpose
    assert brief.objectives.startswith("Half as many stock-outs within a year")
    assert (
        "- Fewer stock-outs (measure: Stock-outs a quarter, target: 6, by 2027-12-31)"
        in brief.objectives
    )
    assert brief.scope_in == "A weekly demand forecast for the two northern warehouses"
    assert brief.scope_out is None
    assert brief.wanted_by.isoformat() == "2027-06-30"
    # The value case's cost range, not the request's rough budget.
    assert (str(brief.budget_low), str(brief.budget_high)) == ("10000.00", "20000.00")
    assert brief.decision_rights == decision_rights()

    stakeholders = db.scalars(
        select(ProjectStakeholder)
        .where(ProjectStakeholder.project_id == project_id)
        .order_by(ProjectStakeholder.id)
    ).all()
    assert [(s.name, s.user_id, s.role_or_interest) for s in stakeholders] == [
        (sponsor_user.display_name, sponsor_user.id, "Sponsor"),
        (pm.display_name, pm.id, "Project manager"),
        ("Robin Contact", None, "Client contact, Northwind Example Ltd"),
    ]
    assert _count(db, ProjectAssumption) == 0

    [risk] = _risks(db, project_id)
    assert (risk.kind, risk.category, risk.status, risk.source) == (
        "threat",
        "project",
        "proposed",
        "person",
    )
    assert risk.cause == "Store managers may not trust the forecast"
    assert (risk.probability, risk.impact, risk.score, risk.band) == (None, None, None, None)
    assert (risk.response, risk.response_plan, risk.objectives) == ("reduce", RESPONSE_PLAN, [])
    assert risk.escalated_to is None

    [decision] = db.scalars(select(GateDecision)).all()
    row = db.scalars(
        select(AuditDecision).where(AuditDecision.agent == "gate", AuditDecision.status == "ok")
    ).one()
    assert row.action_taken["decision_id"] == decision.id
    assert row.action_taken["created"] == {
        "project_id": project_id,
        "brief_id": brief.id,
        "stakeholder_ids": [s.id for s in stakeholders],
        "assumption_ids": [],
        "risk_ids": [risk.id],
    }
    assert row.output["project_id"] == project_id

    # The request links to its project, and the project back to its request.
    assert web.get(f"{URL}/{rid}").json()["project_id"] == project_id
    detail = web.get(f"{PROJECTS}/{project_id}").json()
    assert (detail["request_id"], detail["client_name"]) == (rid, "Northwind Example Ltd")


def test_readiness_yellows_become_proposed_unscored_ai_risks(web, db, pm, sponsor):
    sponsor_client, _ = sponsor
    rid = _create(web, uses_ai=True).json()["id"]
    add_value_case(rid, pm.id)
    add_readiness(
        rid,
        pm.id,
        pm.id,
        problem_rating="yellow",
        problem_note="A person must judge the reputational concern of automating this",
    )
    # A yellow area needs an owned action before the request can be submitted.
    with Session(engine) as session:
        check = session.scalars(
            select(ReadinessCheck).where(ReadinessCheck.request_id == rid)
        ).one()
        check.actions.append(
            ReadinessAction(
                area="problem",
                position=0,
                description="Ask the client's communications lead",
                owner_id=pm.id,
                due_date=check.actions[0].due_date,
            )
        )
        session.commit()
    assert _move(web, rid, "submit", 1).status_code == 200

    response = _pass(sponsor_client, rid, pm.id)
    assert response.status_code == 200, response.text
    risks = _risks(db, response.json()["project_id"])
    assert [(r.title, r.category, r.status, r.needs_judgement) for r in risks] == [
        ("Readiness: problem rated yellow (the problem is worth solving)", "ai", "proposed", True),
        ("Readiness: approach rated yellow (AI is the right tool)", "ai", "proposed", False),
    ]
    assert [r.cause for r in risks] == [
        "A person must judge the reputational concern of automating this",
        "A simple reorder rule may do as well",
    ]
    for risk in risks:
        assert (risk.probability, risk.impact, risk.score, risk.band) == (None, None, None, None)
        assert (risk.source, risk.escalated_to) == ("person", None)


def test_a_red_area_also_becomes_a_risk(web, db, pm, sponsor):
    # The approval gate refuses red, so call the builder straight, as a gates file without that
    # criterion would.
    sponsor_client, sponsor_user = sponsor
    rid = _submitted(web, pm, uses_ai=True, data_rating="red", data_note="Legal sign-off needed")
    with Session(engine) as session:
        request = session.get(ProjectRequest, rid)
        created = create_from_request(
            session, request, session.get(User, sponsor_user.id), session.get(User, pm.id)
        )
        risks = [(r.title, r.category, r.needs_judgement) for r in created.risks]
        session.rollback()
    assert risks == [
        ("Readiness: approach rated yellow (AI is the right tool)", "ai", False),
        ("Readiness: data rated red (the data is good enough)", "ai", True),
    ]


def test_no_client_contact_means_two_stakeholders(web, db, pm, sponsor):
    rid = _submitted(web, pm)
    response = _pass(sponsor[0], rid, pm.id)
    assert response.status_code == 200
    names = [
        s["role_or_interest"]
        for s in web.get(f"{PROJECTS}/{response.json()['project_id']}/stakeholders").json()["items"]
    ]
    assert names == ["Sponsor", "Project manager"]
    assert _risks(db, response.json()["project_id"]) == []


# --- The project manager


def test_a_pass_without_a_project_manager_is_422(web, db, pm, sponsor):
    rid = _submitted(web, pm)
    body = {"version": 2, "outcome": "pass", "reason": REASON, "criteria": TICKS}
    assert sponsor[0].post(f"{URL}/{rid}/gate", json=body).status_code == 422
    _nothing_created(db, rid)


@pytest.mark.parametrize("who", ["sponsor", "auditor", "inactive", "unknown"])
def test_the_project_manager_must_be_an_active_project_manager(web, db, sign_in, pm, sponsor, who):
    rid = _submitted(web, pm)
    if who == "sponsor":
        manager_id = sponsor[1].id
    elif who == "auditor":
        manager_id = _client(db, sign_in, "auditor")[1].id
    elif who == "inactive":
        manager = _client(db, sign_in, "project_manager", "gone.pm@pmo.example")[1]
        manager.is_active = False
        db.commit()
        manager_id = manager.id
    else:
        manager_id = 9999
    response = _pass(sponsor[0], rid, manager_id)
    assert response.status_code == 422
    assert response.json()["detail"] == BAD_PROJECT_MANAGER
    _nothing_created(db, rid)


@pytest.mark.parametrize("outcome", ["return", "decline"])
def test_return_and_decline_refuse_a_project_manager(web, db, pm, sponsor, outcome):
    rid = _submitted(web, pm)
    response = _pass(sponsor[0], rid, pm.id, outcome=outcome, criteria=[])
    assert response.status_code == 422
    assert _count(db, GateDecision) == 0


def test_a_request_already_a_project_is_409(web, db, pm, sponsor):
    rid = _submitted(web, pm)
    with Session(engine) as session:
        project = Project(name="Earlier", stage="planning", sponsor="S", pm="P", status="on_track")
        session.add(project)
        session.flush()
        session.get(ProjectRequest, rid).project_id = project.id
        session.commit()
    response = _pass(sponsor[0], rid, pm.id, version=3)
    assert response.status_code == 409
    assert response.json()["detail"] == ALREADY_A_PROJECT
    assert _count(db, Project) == 1
    assert _count(db, GateDecision) == 0
    refusal = db.scalars(select(AuditDecision).where(AuditDecision.agent == "gate")).one()
    assert refusal.action_taken["refusal"] == "already_a_project"


# --- A failure creates nothing


def test_a_failure_in_the_builder_creates_nothing(web, db, pm, sponsor, client_row, monkeypatch):
    rid = _create(web, client_id=client_row.id, known_risks="Gaps").json()["id"]
    add_value_case(rid, pm.id)
    assert _move(web, rid, "submit", 1).status_code == 200

    def broken(*_args, **_kwargs):
        raise RuntimeError("the risk register is unavailable")

    # The project, brief and stakeholders are flushed by the time the risks fail.
    monkeypatch.setattr("app.projects._first_risks", broken)
    response = _pass(sponsor[0], rid, pm.id)
    assert response.status_code == 500
    assert response.json()["detail"] == NOT_CREATED
    _nothing_created(db, rid)


def test_a_failure_recording_the_decision_creates_nothing(web, db, pm, sponsor, monkeypatch):
    rid = _submitted(web, pm, uses_ai=True)

    def broken(*_args, **_kwargs):
        raise RuntimeError("the audit trail is unavailable")

    monkeypatch.setattr(gates_api, "record_decision", broken)
    quiet = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
    quiet.cookies, quiet.headers = sponsor[0].cookies, sponsor[0].headers
    assert _pass(quiet, rid, pm.id).status_code == 500
    _nothing_created(db, rid)


# --- Reading and editing the brief, stakeholders and assumptions


@pytest.fixture
def project_id(web, db, pm, sponsor) -> int:
    rid = _submitted(web, pm)
    response = _pass(sponsor[0], rid, pm.id)
    assert response.status_code == 200, response.text
    return response.json()["project_id"]


def _project_rows(db: Session, subject_type: str) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(
        AuditDecision.agent == "project", AuditDecision.subject_type == subject_type
    )
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def test_the_brief_is_read_and_edited_with_one_audit_row_per_edit(web, db, pm, project_id):
    brief = web.get(f"{PROJECTS}/{project_id}/brief").json()
    assert brief["version"] == 1
    assert brief["pm"] == pm.display_name
    assert brief["budget_low"] == "10000.00"
    assert {r["role"] for r in brief["decision_rights"]} == {"project_manager", "sponsor"}

    url = f"{PROJECTS}/{project_id}/brief"
    response = web.patch(url, json={"version": 1, "scope_out": "Stores outside the north"})
    assert response.status_code == 200, response.text
    assert (response.json()["scope_out"], response.json()["version"]) == (
        "Stores outside the north",
        2,
    )
    [row] = _project_rows(db, "project_brief")
    assert (row.subject_id, row.version, row.actor_user_id) == (str(brief["id"]), 2, pm.id)
    assert row.action_taken == {"action": "update", "project_id": project_id}
    assert (row.signals["scope_out"], row.output["scope_out"]) == (
        None,
        "Stores outside the north",
    )

    # The same values again change nothing and write nothing.
    same = web.patch(url, json={"version": 2, "scope_out": "Stores outside the north"})
    assert same.json()["version"] == 2
    assert len(_project_rows(db, "project_brief")) == 1

    assert web.patch(url, json={"version": 1, "scope_out": "Late"}).json()["detail"] == STALE
    assert web.patch(url, json={"version": 2, "purpose": None}).status_code == 422
    assert web.patch(url, json={"version": 2, "budget_low": "30000.00"}).status_code == 422
    assert web.patch(url, json={"version": 2, "owner": "me"}).status_code == 422
    assert len(_project_rows(db, "project_brief")) == 1


def test_stakeholders_are_added_edited_and_removed(web, db, project_id, sign_in):
    url = f"{PROJECTS}/{project_id}/stakeholders"
    created = web.post(
        url,
        json={
            "name": "Store managers",
            "role_or_interest": "Use the forecast",
            "needs_to_hear": "What changes in their weekly order",
            "how_often": "Weekly",
        },
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    edited = web.patch(f"{url}/{sid}", json={"version": 1, "how_often": "Fortnightly"})
    assert (edited.json()["how_often"], edited.json()["version"]) == ("Fortnightly", 2)
    assert web.delete(f"{url}/{sid}?version=1").status_code == 409
    assert web.delete(f"{url}/{sid}?version=2").status_code == 204
    assert web.patch(f"{url}/{sid}", json={"version": 3, "how_often": "x"}).status_code == 404

    rows = _project_rows(db, "project_stakeholder")
    assert [(r.action_taken["action"], r.version) for r in rows] == [
        ("create", 1),
        ("update", 2),
        ("delete", 3),
    ]
    assert rows[-1].signals["name"] == "Store managers"
    assert rows[-1].output is None
    assert (
        web.post(url, json={"name": "X", "role_or_interest": "Y", "user_id": 9999}).status_code
        == 422
    )

    page = web.get(f"{url}?limit=1&offset=1").json()
    assert (page["total"], len(page["items"])) == (2, 1)
    assert page["items"][0]["role_or_interest"] == "Project manager"


def test_assumptions_are_added_edited_and_removed(web, db, pm, project_id):
    url = f"{PROJECTS}/{project_id}/assumptions"
    assert web.get(url).json()["total"] == 0
    body = {"text": "Three years of sales history exist", "owner_user_id": pm.id}
    created = web.post(url, json={**body, "confirm_by": "2026-12-01"})
    assert created.status_code == 201, created.text
    assert (created.json()["status"], created.json()["owner_name"]) == ("open", pm.display_name)
    aid = created.json()["id"]
    edited = web.patch(f"{url}/{aid}", json={"version": 1, "status": "wrong"})
    assert edited.json()["status"] == "wrong"
    assert web.patch(f"{url}/{aid}", json={"version": 2, "status": "maybe"}).status_code == 422
    assert web.patch(f"{url}/{aid}", json={"version": 2, "owner_user_id": 9999}).status_code == 422
    assert (
        web.post(url, json={**body, "confirm_by": "2026-12-01", "owner_user_id": 9999}).status_code
        == 422
    )
    assert web.post(url, json=body).status_code == 422  # no confirm-by date
    assert web.delete(f"{url}/{aid}?version=2").status_code == 204
    rows = _project_rows(db, "project_assumption")
    assert [r.action_taken["action"] for r in rows] == ["create", "update", "delete"]


def test_a_sponsor_reads_but_does_not_edit(db, project_id, sponsor):
    sponsor_client, _ = sponsor
    url = f"{PROJECTS}/{project_id}"
    assert sponsor_client.get(f"{url}/brief").status_code == 200
    assert (
        sponsor_client.patch(f"{url}/brief", json={"version": 1, "scope_out": "x"}).status_code
        == 403
    )
    assert (
        sponsor_client.post(f"{url}/stakeholders", json={"name": "A", "role_or_interest": "B"})
    ).status_code == 403


def test_another_project_manager_may_edit_and_is_recorded(web, db, sign_in, project_id):
    other, user = _client(db, sign_in, "project_manager", "other.pm@pmo.example")
    url = f"{PROJECTS}/{project_id}/brief"
    assert other.patch(url, json={"version": 1, "scope_out": "Handover"}).status_code == 200
    [row] = _project_rows(db, "project_brief")
    assert row.actor_user_id == user.id


def test_an_unknown_project_is_404(web, pm):
    for path in ("", "/brief", "/stakeholders", "/assumptions"):
        assert web.get(f"{PROJECTS}/9999{path}").status_code == 404


def test_a_stakeholder_of_another_project_is_404(web, db, project_id):
    with Session(engine) as session:
        other = Project(name="Other", stage="planning", sponsor="S", pm="P", status="on_track")
        session.add(other)
        session.commit()
        other_id = other.id
    [sid, *_] = [s["id"] for s in web.get(f"{PROJECTS}/{project_id}/stakeholders").json()["items"]]
    url = f"{PROJECTS}/{other_id}/stakeholders/{sid}"
    assert web.patch(url, json={"version": 1, "how_often": "x"}).status_code == 404


def test_people_can_be_listed_by_role_for_the_project_manager_picker(web, db, sign_in, pm):
    _client(db, sign_in, "sponsor")
    other = _client(db, sign_in, "project_manager", "other.pm@pmo.example")[1]
    page = web.get("/api/v1/users?role=project_manager").json()
    assert [p["id"] for p in page["items"]] == sorted([pm.id, other.id])
    assert web.get("/api/v1/users?role=superuser").status_code == 422
