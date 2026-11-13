"""Deliverable acceptance (item 7.6): a deliverable is accepted only through a recorded answer to an
offer; not accepted sends it back to work with what is missing; every round is kept; refusals are
audited; a project with no client is answered by the sponsor; and the simulated client answers
only for a simulated client."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import delivery as rules
from app import orchestrator, simulated_client
from app.api.delivery import OFFER_WAITING, STALE
from app.baselines import freeze
from app.config import get_settings
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    Client,
    DeliverableAcceptance,
    GateDecision,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectPlan,
    User,
)
from app.models.audit import AppendOnlyError

TODAY = date(2027, 6, 1)


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(rules, "today", lambda: TODAY)


@pytest.fixture
def pm(db, sign_in) -> tuple[TestClient, User]:
    web = TestClient(app, base_url="http://localhost")
    return web, sign_in(web, db, "project_manager")


@pytest.fixture
def sponsor(db, sign_in) -> tuple[TestClient, User]:
    web = TestClient(app, base_url="http://localhost")
    return web, sign_in(web, db, "sponsor")


def make_project(
    db: Session,
    owner: User,
    *,
    client: str | None = "real",
    status: str = "ready_for_acceptance",
    sponsor_id: int | None = None,
) -> tuple[int, int]:
    """A project in delivery managed by `owner`, with one milestone and one deliverable in
    `status`, and baseline 1. `client` is `real`, `simulated` or None. Returns the project and
    deliverable ids."""
    client_id = None
    if client is not None:
        email = "contact@harbour.example" if client == "simulated" else "contact@harbour.test"
        row = Client(name="Harbour Analytics", contact_email=email)
        db.add(row)
        db.flush()
        client_id = row.id
    project = Project(
        name="Forecast",
        stage="delivery",
        sponsor="S",
        pm="P",
        pm_user_id=owner.id,
        sponsor_user_id=sponsor_id,
        client_id=client_id,
        status="on_track",
    )
    db.add(project)
    db.flush()
    plan = ProjectPlan(project_id=project.id, scope_in="The pilot", contingency_pct=0)
    db.add(plan)
    db.flush()
    milestone = PlanMilestone(plan_id=plan.id, name="Pilot live", target_date=date(2027, 3, 1))
    db.add(milestone)
    db.flush()
    deliverable = PlanDeliverable(
        plan_id=plan.id, name="Model", milestone_id=milestone.id, status=status
    )
    db.add_all(
        [
            deliverable,
            PlanEstimateLine(
                plan_id=plan.id, description="Analysts", category="people", amount="1000.00"
            ),
        ]
    )
    decision = GateDecision(
        gate_id="go_ahead",
        subject_type="project",
        subject_id=project.id,
        outcome="pass",
        decided_by=owner.id,
        reason="The plan is complete and inside the budget",
        criteria=[],
        policy_version="test",
    )
    db.add(decision)
    db.flush()
    freeze(db, plan, decision.id)
    db.commit()
    return project.id, deliverable.id


def _url(pid: int, did: int, what: str) -> str:
    return f"/api/v1/projects/{pid}/delivery/deliverables/{did}/{what}"


def offer(web: TestClient, pid: int, did: int, on: str = "2027-05-20", **extra):
    body = {"offered_to": "Dana Reyes, Harbour", "offered_on": on, "means": "email", **extra}
    return web.post(_url(pid, did, "offer"), json=body)


def answer(web: TestClient, pid: int, did: int, value: str = "accepted", **extra):
    body = {"answer": value, "answered_on": "2027-05-25", **extra}
    if value == "not_accepted" and "missing" not in extra:
        body["missing"] = "The model's accuracy report"
    return web.post(_url(pid, did, "answer"), json=body)


def _deliverable(body: dict, did: int) -> dict:
    return next(d for d in body["deliverables"] if d["id"] == did)


def _audit(db: Session, **filters) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(AuditDecision.subject_type == "plan_deliverable")
    for name, value in filters.items():
        stmt = stmt.where(getattr(AuditDecision, name) == value)
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def _refusals(db: Session) -> list[str]:
    return [r.action_taken["refusal"] for r in _audit(db, status="rejected")]


# --- Accepted, not accepted, and a second round


def test_an_accepted_answer_accepts_the_deliverable_and_keeps_the_round(db, pm):
    web, user = pm
    pid, did = make_project(db, user)

    offered = offer(web, pid, did, note="Sent with the handover pack")
    assert offered.status_code == 201, offered.text
    shown = _deliverable(offered.json(), did)
    assert shown["status"] == "ready_for_acceptance"
    assert shown["awaiting_answer"] is True
    assert shown["acceptance"][0] | {"id": 0, "created_at": ""} == {
        "id": 0,
        "round": 1,
        "kind": "offer",
        "offered_to": "Dana Reyes, Harbour",
        "offered_on": "2027-05-20",
        "means": "email",
        "answer": None,
        "answered_on": None,
        "missing": None,
        "note": "Sent with the handover pack",
        "source": "person",
        "recorded_by": user.id,
        "recorded_by_name": user.display_name,
        "created_at": "",
    }

    answered = answer(web, pid, did, note="Signed off by Dana")
    assert answered.status_code == 201, answered.text
    shown = _deliverable(answered.json(), did)
    assert shown["status"] == "accepted"
    assert shown["awaiting_answer"] is False
    assert [(r["round"], r["kind"], r["answer"]) for r in shown["acceptance"]] == [
        (1, "offer", None),
        (1, "answer", "accepted"),
    ]
    assert [r.action_taken["action"] for r in _audit(db, status="ok")] == [
        "deliverable_offered",
        "deliverable_accepted",
    ]
    accepted = _audit(db, status="ok")[-1]
    assert accepted.actor_user_id == user.id and accepted.trigger == "human"
    assert accepted.signals["status"] == "ready_for_acceptance"
    assert accepted.output["status"] == "accepted"
    assert accepted.output["acceptance"]["source"] == "person"


def test_not_accepted_sends_it_back_to_work_and_a_second_round_accepts_it(db, pm):
    web, user = pm
    pid, did = make_project(db, user)
    offer(web, pid, did)

    refused = answer(web, pid, did, "not_accepted", missing="The accuracy report")
    assert refused.status_code == 201, refused.text
    shown = _deliverable(refused.json(), did)
    assert shown["status"] == "in_progress"
    assert shown["acceptance"][-1]["missing"] == "The accuracy report"

    # It can't be offered again until it is ready again.
    assert offer(web, pid, did).status_code == 409
    version = shown["version"]
    ready = web.put(
        f"/api/v1/projects/{pid}/delivery/deliverables/{did}",
        json={"version": version, "status": "ready_for_acceptance"},
    )
    assert ready.status_code == 200, ready.text
    assert offer(web, pid, did, on="2027-05-28").status_code == 201
    second = answer(web, pid, did, answered_on="2027-05-31")
    assert second.status_code == 201, second.text
    shown = _deliverable(second.json(), did)
    assert shown["status"] == "accepted"
    assert [(r["round"], r["kind"], r["answer"]) for r in shown["acceptance"]] == [
        (1, "offer", None),
        (1, "answer", "not_accepted"),
        (2, "offer", None),
        (2, "answer", "accepted"),
    ]


def test_only_an_answer_accepts_a_deliverable(db, pm):
    web, user = pm
    pid, did = make_project(db, user)
    put = web.put(
        f"/api/v1/projects/{pid}/delivery/deliverables/{did}",
        json={"version": 1, "status": "accepted"},
    )
    assert put.status_code == 422
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "ready_for_acceptance"


# --- Refusals, each audited


def test_an_answer_needs_a_waiting_offer_and_takes_one_answer(db, pm):
    web, user = pm
    pid, did = make_project(db, user)
    no_offer = answer(web, pid, did)
    assert no_offer.status_code == 409
    offer(web, pid, did)
    assert answer(web, pid, did, "not_accepted").status_code == 201
    again = answer(web, pid, did)
    assert again.status_code == 409
    assert _refusals(db) == ["no_offer", "no_offer"]
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "in_progress"


def test_an_offer_needs_a_ready_deliverable_and_no_offer_waiting(db, pm):
    web, user = pm
    pid, did = make_project(db, user, status="in_progress")
    assert offer(web, pid, did).status_code == 409
    pid2, did2 = make_project(db, user)
    assert offer(web, pid2, did2).status_code == 201
    assert offer(web, pid2, did2).status_code == 409
    assert _refusals(db) == ["not_ready", "already_offered"]


def test_a_waiting_offer_keeps_the_deliverable_ready_until_its_answer(db, pm):
    web, user = pm
    pid, did = make_project(db, user)
    shown = _deliverable(offer(web, pid, did).json(), did)
    status_url = f"/api/v1/projects/{pid}/delivery/deliverables/{did}"
    for to in ("in_progress", "not_started"):
        moved = web.put(status_url, json={"version": shown["version"], "status": to})
        assert moved.status_code == 409, moved.text
        assert moved.json()["detail"] == OFFER_WAITING
    assert _refusals(db) == ["offer_waiting", "offer_waiting"]
    refused = _audit(db, status="rejected")[-1]
    assert refused.action_taken["action"] == "deliverable_status_refused"
    assert refused.action_taken["requested_status"] == "not_started"
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "ready_for_acceptance"
    # The client's answer is recorded instead: not accepted sends it back to work.
    answered = answer(web, pid, did, "not_accepted")
    assert answered.status_code == 201, answered.text
    shown = _deliverable(answered.json(), did)
    assert shown["status"] == "in_progress"
    # With no offer waiting, the status route moves it again.
    moved = web.put(status_url, json={"version": shown["version"], "status": "not_started"})
    assert moved.status_code == 200, moved.text


def test_an_answer_that_loses_the_race_keeps_nothing_and_is_audited(db, pm, monkeypatch):
    web, user = pm
    pid, did = make_project(db, user)
    offer(web, pid, did)
    # Another write moves the deliverable between the checks and the save.
    monkeypatch.setattr(rules, "set_status", lambda *args: False)
    lost = answer(web, pid, did)
    assert lost.status_code == 409, lost.text
    assert lost.json()["detail"] == STALE
    assert _refusals(db) == ["stale_version"]
    refused = _audit(db, status="rejected")[-1]
    assert refused.action_taken["action"] == "deliverable_answer_refused"
    assert refused.action_taken["answer"] == "accepted"
    assert refused.output["status"] == "ready_for_acceptance"
    kinds = db.scalars(
        select(DeliverableAcceptance.kind).where(DeliverableAcceptance.deliverable_id == did)
    )
    assert list(kinds) == ["offer"]


def test_dates_after_today_or_before_the_offer_are_refused(db, pm):
    web, user = pm
    pid, did = make_project(db, user)
    assert offer(web, pid, did, on="2027-06-02").status_code == 422
    assert offer(web, pid, did, on="2027-05-20").status_code == 201
    assert answer(web, pid, did, answered_on="2027-05-19").status_code == 422
    assert answer(web, pid, did, answered_on="2027-06-02").status_code == 422
    assert _refusals(db) == ["future_date", "before_offer", "future_date"]


def test_not_accepted_needs_what_is_missing(db, pm):
    web, user = pm
    pid, did = make_project(db, user)
    offer(web, pid, did)
    body = {"answer": "not_accepted", "answered_on": "2027-05-25"}
    assert web.post(_url(pid, did, "answer"), json=body).status_code == 422


def test_a_project_not_in_delivery_takes_no_offer(db, pm):
    web, user = pm
    pid, did = make_project(db, user)
    db.execute(update(Project).where(Project.id == pid).values(stage="closure"))
    db.commit()
    assert offer(web, pid, did).status_code == 409
    assert _audit(db, status="rejected") == []  # the project is the subject of that refusal


def test_roles_without_permission_are_refused_and_audited(db, pm, sign_in):
    _, user = pm
    pid, did = make_project(db, user)
    auditor = TestClient(app, base_url="http://localhost")
    sign_in(auditor, db, "auditor")
    assert offer(auditor, pid, did).status_code == 403
    assert answer(auditor, pid, did).status_code == 403
    assert _refusals(db) == ["no_permission", "no_permission"]


def test_a_sponsor_does_not_record_a_client_answer(db, pm, sponsor):
    web, user = pm
    pid, did = make_project(db, user)
    offer(web, pid, did)
    refused = answer(sponsor[0], pid, did)
    assert refused.status_code == 403
    assert _refusals(db) == ["client_answer_needs_project_write"]


# --- A project with no client: the sponsor accepts


def test_with_no_client_the_sponsor_accepts_and_the_project_manager_may_not(db, pm, sponsor):
    web, user = pm
    pid, did = make_project(db, user, client=None)
    offer(web, pid, did, offered_to="The sponsor")
    by_pm = answer(web, pid, did)
    assert by_pm.status_code == 403
    by_sponsor = answer(sponsor[0], pid, did)
    assert by_sponsor.status_code == 201, by_sponsor.text
    assert _deliverable(by_sponsor.json(), did)["status"] == "accepted"
    assert _refusals(db) == ["sponsor_accepts"]
    assert _audit(db, status="ok")[-1].actor_user_id == sponsor[1].id


def test_with_no_client_a_sponsor_who_manages_the_project_may_not_accept(db, sponsor):
    web, user = sponsor
    pid, did = make_project(db, user, client=None)
    db.add(
        DeliverableAcceptance(
            project_id=pid,
            deliverable_id=did,
            deliverable_name="Model",
            round=1,
            kind="offer",
            offered_to="The sponsor",
            offered_on=date(2027, 5, 20),
            means="meeting",
            source="person",
            recorded_by=user.id,
        )
    )
    db.commit()
    assert answer(web, pid, did).status_code == 403
    assert _refusals(db) == ["own_project"]


def test_the_view_says_who_answers(db, pm):
    web, user = pm
    real, _ = make_project(db, user)
    simulated, _ = make_project(db, user, client="simulated")
    none, _ = make_project(db, user, client=None)
    shown = {
        pid: web.get(f"/api/v1/projects/{pid}/delivery").json() for pid in (real, simulated, none)
    }
    assert (shown[real]["has_client"], shown[real]["client_simulated"]) == (True, False)
    assert (shown[simulated]["has_client"], shown[simulated]["client_simulated"]) == (True, True)
    assert (shown[none]["has_client"], shown[none]["client_simulated"]) == (False, False)
    assert shown[real]["pm_user_id"] == user.id


# --- Append-only


def test_acceptance_rows_are_append_only(db, pm):
    web, user = pm
    pid, did = make_project(db, user)
    offer(web, pid, did)
    db.expire_all()
    row = db.scalar(select(DeliverableAcceptance))
    row.means = "post"
    with pytest.raises(AppendOnlyError):
        db.flush()
    db.rollback()
    with pytest.raises(AppendOnlyError):
        db.execute(update(DeliverableAcceptance).values(means="post"))
    db.rollback()
    with pytest.raises(AppendOnlyError):
        db.delete(db.scalar(select(DeliverableAcceptance)))
        db.flush()
    db.rollback()


# --- The simulated client


NOW = datetime(2027, 6, 1, 12, 0)


def _offered_long_ago(db: Session, pm, client: str | None) -> tuple[int, int]:
    web, user = pm
    pid, did = make_project(db, user, client=client)
    assert offer(web, pid, did).status_code == 201
    db.expire_all()
    return pid, did


def test_the_simulated_client_answers_only_a_simulated_client(db, pm):
    _, did = _offered_long_ago(db, pm, "simulated")
    audit_id = simulated_client.accept_now(
        db, did, answer="accepted", delay_minutes=0, now=NOW + timedelta(days=1)
    )
    db.commit()
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "accepted"
    row = db.scalars(select(DeliverableAcceptance).order_by(DeliverableAcceptance.id)).all()[-1]
    assert (row.kind, row.source, row.recorded_by) == ("answer", "simulated", None)
    assert row.note.startswith("Simulated client")
    audit = db.get(AuditDecision, audit_id)
    assert (audit.agent, audit.trigger, audit.actor_user_id) == (
        "simulated_client",
        "orchestrator",
        None,
    )
    assert audit.action_taken["source"] == "simulated"


@pytest.mark.parametrize("client", ["real", None])
def test_the_simulated_client_never_answers_for_a_real_client_or_no_client(db, pm, client):
    _, did = _offered_long_ago(db, pm, client)
    with pytest.raises(simulated_client.Skipped):
        simulated_client.accept_now(
            db, did, answer="accepted", delay_minutes=0, now=NOW + timedelta(days=1)
        )
    db.rollback()
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "ready_for_acceptance"


def test_a_simulated_not_accepted_says_it_is_simulated(db, pm):
    _, did = _offered_long_ago(db, pm, "simulated")
    simulated_client.accept_now(
        db, did, answer="not_accepted", delay_minutes=0, now=NOW + timedelta(days=1)
    )
    db.commit()
    db.expire_all()
    assert db.get(PlanDeliverable, did).status == "in_progress"
    row = db.scalars(select(DeliverableAcceptance).order_by(DeliverableAcceptance.id)).all()[-1]
    assert row.answer == "not_accepted"
    assert "simulated" in row.missing.lower()


def test_a_simulated_answer_that_loses_the_race_keeps_nothing_and_is_skipped(
    db, pm, monkeypatch
):
    _, did = _offered_long_ago(db, pm, "simulated")
    # Another write moves the deliverable between the checks and the save.
    monkeypatch.setattr(rules, "set_status", lambda *args: False)
    ctx = orchestrator.Context(
        db, get_settings(), None, None, "", "deliverable", NOW + timedelta(days=1)
    )
    skip_id = orchestrator._client_acceptance(ctx, did)
    db.commit()
    db.expire_all()
    kinds = db.scalars(
        select(DeliverableAcceptance.kind).where(DeliverableAcceptance.deliverable_id == did)
    )
    assert list(kinds) == ["offer"]
    assert db.get(PlanDeliverable, did).status == "ready_for_acceptance"
    skipped = db.get(AuditDecision, skip_id)
    assert (skipped.status, skipped.subject_type, skipped.subject_id) == (
        "skipped",
        "deliverable",
        str(did),
    )
    assert skipped.action_taken["action"] == "client_acceptance"
    assert skipped.action_taken["reason"] == simulated_client.NOT_OFFERED
    assert not _audit(db, agent="simulated_client")


def test_the_simulated_client_waits_for_its_delay_and_answers_once(db, pm):
    _, did = _offered_long_ago(db, pm, "simulated")
    offered_at = db.scalar(select(DeliverableAcceptance.created_at))
    with pytest.raises(simulated_client.Skipped):
        simulated_client.accept_now(
            db, did, answer="accepted", delay_minutes=60, now=offered_at + timedelta(minutes=59)
        )
    db.rollback()
    simulated_client.accept_now(
        db, did, answer="accepted", delay_minutes=60, now=offered_at + timedelta(minutes=60)
    )
    db.commit()
    with pytest.raises(simulated_client.Skipped):
        simulated_client.accept_now(
            db, did, answer="accepted", delay_minutes=60, now=offered_at + timedelta(minutes=61)
        )
    db.rollback()
