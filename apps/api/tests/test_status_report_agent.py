"""The status report agent (item 7.4), with a fake Claude client: numbers only through figure
keys (a stray number or an unknown key refuses the run and saves nothing), failed runs audited
and saving nothing, a person's section never overwritten, Confirm, publishing refused while an
agent's draft is unreviewed, and AGENT_MODE, dry runs and what the agent reads."""

import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_status_reports import PERIOD_END, TODAY, _project

from app import delivery
from app import reports as rules
from app.agents import status_report_agent as agent
from app.agents.risk_agent import ClaudeReply
from app.api import status_reports as reports_api
from app.config import get_settings
from app.db import engine
from app.main import app
from app.models import AuditDecision, Base, ChangeRequest, StatusReport, User
from app.models.report import SECTIONS

GOOD = {
    "summary": "Spend to date is {{spend_to_date}}; {{milestones_late}} milestones are late.",
    "done": "The team worked on the pilot.",
    "next": "From {{next_period_start}} to {{next_period_end}} the team prepares the handover; "
    "the next report is due on {{next_period_due}}.",
    "issues": "The records don't say why the pilot is late: the project manager to fill in.",
    "decisions": "",
}


def _reply(output: dict | None = None, **fields) -> ClaudeReply:
    return ClaudeReply(
        text=fields.get("text") or json.dumps(output if output is not None else GOOD),
        input_tokens=fields.get("input_tokens", 1200),
        output_tokens=fields.get("output_tokens", 500),
        stop_reason=fields.get("stop_reason", "end_turn"),
    )


class FakeClaude:
    """Returns `reply` (or raises `error`) and keeps every request it was given."""

    def __init__(self, reply: ClaudeReply | None = None, error: Exception | None = None):
        self.reply = reply or _reply()
        self.error = error
        self.requests: list[dict] = []

    def create(self, request: dict) -> ClaudeReply:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.reply


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
    monkeypatch.setattr(delivery, "today", lambda: TODAY)


@pytest.fixture
def web(db) -> TestClient:
    return TestClient(app, base_url="http://localhost")


@pytest.fixture
def pm(web, db, sign_in) -> User:
    return sign_in(web, db, "project_manager")


@pytest.fixture
def fake():
    client = FakeClaude()
    app.dependency_overrides[reports_api.get_claude_client] = lambda: client
    yield client
    app.dependency_overrides.pop(reports_api.get_claude_client, None)


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value: str) -> None:
        monkeypatch.setattr(get_settings(), "agent_mode", value)

    set_mode("enforce")
    return set_mode


def _url(pid: int, suffix: str = "") -> str:
    return f"/api/v1/projects/{pid}/status-reports{suffix}"


@pytest.fixture
def report(db, web, pm) -> dict:
    """A project in delivery and its draft for the period ending `PERIOD_END`."""
    ids = _project(db, pm)
    response = web.post(_url(ids["pid"]), json={"period_end": PERIOD_END.isoformat()})
    assert response.status_code == 201, response.text
    return {"pid": ids["pid"], **response.json()}


def _draft(web: TestClient, report: dict, query: str = "") -> object:
    return web.post(_url(report["pid"], f"/{report['id']}/draft{query}"))


def _get(web: TestClient, report: dict) -> dict:
    return web.get(_url(report["pid"], f"/{report['id']}")).json()


def _runs(db: Session) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(AuditDecision.agent == agent.AGENT)
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


# --- Saving


def test_enforce_saves_unreviewed_drafts_with_figures_filled_in_and_the_checker_clean(
    db, web, pm, fake, mode, report
):
    response = _draft(web, report)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "enforce"
    assert body["replaced"] == ["summary", "done", "next", "issues"]
    assert body["kept"] == [{"section": "decisions", "reason": agent.LEFT_EMPTY}]
    assert body["figure_keys"] == [
        "milestones_late",
        "next_period_due",
        "next_period_end",
        "next_period_start",
        "spend_to_date",
    ]
    saved = _get(web, report)
    # Next period runs Monday 8 to Sunday 14 March, due Monday 15 March.
    assert saved["sections"]["next"] == (
        "From 8 March 2027 to 14 March 2027 the team prepares the handover; the next report is "
        "due on 15 March 2027."
    )
    assert saved["sections"]["summary"] == "Spend to date is not recorded; 1 milestones are late."
    # The agent's text, next period's dates included, passes the checker.
    assert saved["flags"] == []
    states = saved["section_states"]
    assert states["summary"] == {
        "source": "agent",
        "reviewed_by": None,
        "reviewed_at": None,
        "unreviewed": True,
    }
    assert states["decisions"]["source"] is None
    # The ratings are the rules', never the agent's.
    assert saved["ratings"] == report["ratings"]
    (run,) = _runs(db)
    assert run.status == "ok"
    assert run.subject_type == "status_report" and run.subject_id == str(report["id"])
    assert run.model == get_settings().status_report_agent_model
    assert (run.input_tokens, run.output_tokens) == (1200, 500)
    assert run.cost_usd is not None and run.cost_usd > 0
    assert run.action_taken["figure_keys"] == body["figure_keys"]
    assert run.action_taken["replaced"] == body["replaced"]
    assert run.actor_user_id == pm.id


def test_the_next_period_figures_come_from_the_plans_reporting_fields(web, pm, report):
    figures = {f["key"]: f for f in report["figures"]}
    assert figures["next_period_start"]["value"] == "2027-03-08"
    assert figures["next_period_end"]["value"] == "2027-03-14"
    assert figures["next_period_due"]["value"] == "2027-03-15"
    assert {s["field"] for s in figures["next_period_due"]["sources"]} == {
        "report_period",
        "report_due_day",
    }
    assert figures["next_period_due"]["sources"][0]["table"] == "project_plans"


@pytest.mark.parametrize(
    "text",
    [
        "Spend is $4,000 this period.",
        "The pilot is 3 days late.",
        "Due on 2027-03-15.",
        "Phase 2 starts soon.",
    ],
)
def test_a_number_outside_a_figure_key_refuses_the_run_and_saves_nothing(
    db, web, pm, fake, mode, report, text
):
    fake.reply = _reply({**GOOD, "done": text})
    response = _draft(web, report)
    assert response.status_code == 502
    assert response.json()["detail"] == reports_api.AGENT_FAILED
    saved = _get(web, report)
    assert saved["sections"] == dict.fromkeys(SECTIONS)
    assert saved["version"] == report["version"]
    (run,) = _runs(db)
    assert run.status == "error"
    assert "isn't a figure key" in run.error
    assert run.input_tokens == 1200 and run.cost_usd > 0
    assert run.action_taken["replaced"] == []


def test_an_unknown_figure_key_refuses_the_run_and_saves_nothing(db, web, pm, fake, mode, report):
    fake.reply = _reply({**GOOD, "summary": "We spent {{budget_left}}."})
    assert _draft(web, report).status_code == 502
    assert _get(web, report)["sections"] == dict.fromkeys(SECTIONS)
    (run,) = _runs(db)
    assert run.status == "error" and "budget_left" in run.error


@pytest.mark.parametrize(
    "fake_error",
    [RuntimeError("the API is down"), None],
)
def test_a_failed_run_is_audited_and_saves_nothing(db, web, pm, fake, mode, report, fake_error):
    if fake_error is not None:
        fake.error = fake_error
    else:
        fake.reply = _reply(text="not json")
    assert _draft(web, report).status_code == 502
    saved = _get(web, report)
    assert saved["sections"] == dict.fromkeys(SECTIONS)
    assert saved["section_states"]["summary"]["source"] is None
    (run,) = _runs(db)
    assert run.status == "error"
    assert run.model == get_settings().status_report_agent_model


def test_a_persons_section_is_never_overwritten(db, web, pm, fake, mode, report):
    edited = web.patch(
        _url(report["pid"], f"/{report['id']}"),
        json={"version": report["version"], "sections": {"summary": "A person's summary."}},
    ).json()
    assert edited["section_states"]["summary"]["source"] == "person"
    assert edited["section_states"]["summary"]["reviewed_by"] == pm.id
    body = _draft(web, report).json()
    assert "summary" not in body["replaced"]
    assert {"section": "summary", "reason": agent.PERSON_OWNS} in body["kept"]
    saved = _get(web, report)
    assert saved["sections"]["summary"] == "A person's summary."
    # A confirmed agent draft is the person's too: a second run keeps it.
    confirmed = web.post(
        _url(report["pid"], f"/{report['id']}/sections/done/confirm"),
        json={"version": saved["version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    fake.reply = _reply({**GOOD, "done": "Something else."})
    body = _draft(web, report).json()
    assert {"section": "done", "reason": agent.PERSON_OWNS} in body["kept"]
    assert _get(web, report)["sections"]["done"] == "The team worked on the pilot."
    # Its own unreviewed draft is replaced.
    assert "next" in body["replaced"]


def test_the_orchestrators_run_fills_only_empty_sections(db, web, pm, fake, mode, report):
    _draft(web, report)
    row = db.get(StatusReport, report["id"])
    db.refresh(row)
    fake.reply = _reply({**GOOD, "decisions": "Nothing for the sponsor."})
    result = agent.run(
        db,
        fake,
        row,
        settings=get_settings(),
        save_it=True,
        actor_user_id=None,
        trigger="orchestrator",
        only_empty=True,
    )
    db.commit()
    assert result.replaced == ["decisions"]
    assert {"section": "summary", "reason": agent.WAITS_FOR_REVIEW} in result.kept


def test_publishing_waits_until_every_agent_section_is_reviewed(db, web, pm, fake, mode, report):
    _draft(web, report)
    saved = _get(web, report)
    publish = _url(report["pid"], f"/{report['id']}/publish")
    refused = web.post(publish, json={"version": saved["version"]})
    assert refused.status_code == 409
    assert refused.json()["detail"]["sections"] == ["summary", "done", "next", "issues"]
    refusal = _refusal_rows(db)[-1]
    assert refusal.action_taken["refusal"] == "unreviewed_sections"
    version = saved["version"]
    for name in ("summary", "done", "next"):
        response = web.post(
            _url(report["pid"], f"/{report['id']}/sections/{name}/confirm"),
            json={"version": version},
        )
        assert response.status_code == 200, response.text
        version = response.json()["version"]
        assert response.json()["section_states"][name]["reviewed_by"] == pm.id
    # Editing is the other way to review one.
    version = web.patch(
        _url(report["pid"], f"/{report['id']}"),
        json={"version": version, "sections": {"issues": "Fixed by the project manager."}},
    ).json()["version"]
    published = web.post(publish, json={"version": version})
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"


def _refusal_rows(db: Session) -> list[AuditDecision]:
    db.expire_all()
    stmt = select(AuditDecision).where(
        AuditDecision.agent == "status_report", AuditDecision.status == "rejected"
    )
    return list(db.scalars(stmt.order_by(AuditDecision.id)))


def test_confirm_refuses_a_section_that_isnt_an_unreviewed_agent_draft(db, web, pm, report):
    response = web.post(
        _url(report["pid"], f"/{report['id']}/sections/summary/confirm"),
        json={"version": report["version"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == reports_api.NOT_AGENT_DRAFT
    assert _refusal_rows(db)[-1].action_taken["refusal"] == "not_agent_draft"
    bad = web.post(
        _url(report["pid"], f"/{report['id']}/sections/overall/confirm"),
        json={"version": report["version"]},
    )
    assert bad.status_code == 422


def test_the_agent_cannot_set_a_rating(db, web, pm, fake, mode, report):
    fake.reply = _reply({**GOOD, "overall": "green", "ratings": {"schedule": "green"}})
    body = _draft(web, report).json()
    assert body["replaced"]
    saved = _get(web, report)
    assert saved["ratings"] == report["ratings"]
    (run,) = _runs(db)
    assert {d["part"] for d in run.output["dropped"]} == {"overall", "ratings"}
    assert "rating" not in json.dumps(agent.OUTPUT_SCHEMA["properties"])


# --- AGENT_MODE and dry runs


def test_shadow_runs_and_saves_nothing(db, web, pm, fake, mode, report):
    mode("shadow")
    body = _draft(web, report).json()
    assert body["mode"] == "shadow"
    assert body["replaced"] == []
    assert body["sections"]["summary"].startswith("Spend to date is")
    assert _get(web, report)["sections"] == dict.fromkeys(SECTIONS)
    (run,) = _runs(db)
    assert run.status == "ok" and run.action_taken["mode"] == "shadow"


def test_off_refuses_and_records_it(db, web, pm, fake, mode, report):
    mode("off")
    response = _draft(web, report)
    assert response.status_code == 409
    assert response.json()["detail"] == reports_api.AGENT_OFF
    assert fake.requests == []
    assert _refusal_rows(db)[-1].action_taken["refusal"] == "agent_off"


def test_a_dry_run_shows_the_request_and_calls_nothing(db, web, pm, fake, mode, report):
    body = _draft(web, report, "?dry_run=true").json()
    assert body["dry_run"] is True
    assert fake.requests == []
    assert _runs(db) == []
    request = body["request"]
    assert request["model"] == get_settings().status_report_agent_model
    assert request["output_config"]["format"]["schema"] == agent.OUTPUT_SCHEMA
    assert "temperature" not in request
    assert body["cost_ceiling_usd"] is not None


def test_a_published_report_cant_be_drafted(db, web, pm, fake, mode, report):
    row = db.get(StatusReport, report["id"])
    row.status = "published"
    row.published_at = datetime(2027, 3, 9)
    db.commit()
    response = _draft(web, report)
    assert response.status_code == 409
    assert fake.requests == []


def test_a_role_without_project_write_cant_run_it(db, web, pm, fake, mode, report, sign_in):
    sign_in(web, db, "sponsor", "sponsor@pragmattie.example")
    assert _draft(web, report).status_code == 403
    assert fake.requests == []


# --- What it reads


def test_it_reads_the_previous_published_report_and_the_periods_decisions(
    db, web, pm, fake, mode, report
):
    earlier = web.post(
        _url(report["pid"]), json={"period_end": date(2027, 2, 28).isoformat()}
    ).json()
    web.patch(
        _url(report["pid"], f"/{earlier['id']}"),
        json={"version": earlier["version"], "sections": {"summary": "Last week went well."}},
    )
    web.post(_url(report["pid"], f"/{earlier['id']}/publish"), json={"version": 2})
    change = ChangeRequest(
        project_id=report["pid"],
        title="Add a dashboard",
        reason="The client asked",
        changes={},
        effect={"cost_difference": "0.00"},
        decider="project_manager",
        measured_baseline_number=1,
        status="approved",
        raised_by=pm.id,
    )
    db.add(change)
    db.flush()
    for when, action in (
        (datetime(2027, 3, 3, 10), "change_approved"),
        (datetime(2027, 2, 20, 10), "change_rejected"),  # before the period
    ):
        db.add(
            AuditDecision(
                agent="change",
                trigger="human",
                subject_type="change_request",
                subject_id=str(change.id),
                version=1,
                attempt=1 if action == "change_approved" else 2,
                status="ok",
                actor_user_id=pm.id,
                action_taken={"action": action, "reason": "Worth it for the pilot"},
                created_at=when,
            )
        )
    db.commit()
    data = agent.request_data(db, db.get(StatusReport, report["id"]))["report"]
    assert data["previous_report"]["period_end"] == "2027-02-28"
    assert data["previous_report"]["sections"]["summary"] == "Last week went well."
    assert data["decisions_this_period"] == [
        {
            "on": "2027-03-03",
            "subject": "change_request",
            "title": "Add a dashboard",
            "decision": "change_approved",
            "reason": "Worth it for the pilot",
            "escalated_to": None,
        }
    ]
    keys = {f["key"] for f in data["figures"]}
    assert {"next_period_start", "next_period_end", "next_period_due"} <= keys
    assert data["project"]["plan"]["milestones"][0]["figure_keys"]
    # No person's name or email reaches the model.
    text = json.dumps(data)
    assert pm.email not in text and pm.display_name not in text


def test_the_figure_values_read_back_as_the_same_figures():
    """Every kind of figure, filled in, passes the checker."""
    figures = [
        {"key": "a", "kind": "money", "value": "12500.00"},
        {"key": "b", "kind": "money", "value": "-1500.00"},
        {"key": "c", "kind": "percent", "value": "12.50"},
        {"key": "d", "kind": "percent", "value": "-4.10"},
        {"key": "e", "kind": "date", "value": "2027-03-15"},
        {"key": "f", "kind": "count", "value": 3},
        {"key": "g", "kind": "days", "value": 12},
        {"key": "h", "kind": "number", "value": 2},
        {"key": "i", "kind": "money", "value": None},
    ]
    values = agent.key_values(figures)
    text = " and ".join(values[k] for k in "abcdefghi")
    period = rules.Period(date(2027, 3, 1), date(2027, 3, 7), date(2027, 3, 8))
    assert rules.check({"summary": text}, figures, period) == []
