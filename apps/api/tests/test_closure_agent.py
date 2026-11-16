"""The closure agent (item 7.7), with a fake Claude client: it never writes a benefit's value now
(a reply carrying one changes nothing), never puts a number outside the figures (a stray number or
an unknown key refuses the run and saves nothing), a failed run is audited and changes nothing, a
person's section is kept, lessons are saved as suggestions, and AGENT_MODE and dry runs."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_closure import REPORT, TODAY, closing_project, create

from app import delivery
from app.agents import closure_agent as agent
from app.agents.risk_agent import ClaudeReply
from app.api import closure as closure_api
from app.config import get_settings
from app.db import engine
from app.main import app
from app.models import (
    AuditDecision,
    Base,
    ClosureBenefit,
    ClosureReport,
    Lesson,
    User,
    ValueCaseBenefit,
)

GOOD = {
    "delivered": "The pilot was delivered for {{final_spend}} against a final baseline of "
    "{{final_baseline_total}}.",
    "value_case": "The value case's benefit, days to plan, isn't measured yet.",
    "remaining": "Days to plan still needs an owner and a date.",
    "lessons": [
        {
            "category": "scope",
            "what_happened": "Two changes moved the baseline by {{baseline_moved}}.",
            "do_again": "",
            "do_differently": "Agree the regions before the go-ahead.",
        }
    ],
}


def _reply(output: dict | None = None, **fields) -> ClaudeReply:
    return ClaudeReply(
        text=fields.get("text") or json.dumps(output if output is not None else GOOD),
        input_tokens=fields.get("input_tokens", 1500),
        output_tokens=fields.get("output_tokens", 600),
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
    app.dependency_overrides[closure_api.get_claude_client] = lambda: client
    yield client
    app.dependency_overrides.pop(closure_api.get_claude_client, None)


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value: str) -> None:
        monkeypatch.setattr(get_settings(), "agent_mode", value)

    set_mode("enforce")
    return set_mode


def _draft(web: TestClient, pid: int, **params):
    return web.post(REPORT.format(pid=pid) + "/draft", params=params)


def _report(db: Session, pid: int) -> ClosureReport:
    db.expire_all()
    return db.scalar(select(ClosureReport).where(ClosureReport.project_id == pid))


def _runs(db: Session) -> list[AuditDecision]:
    db.expire_all()
    return list(
        db.scalars(
            select(AuditDecision)
            .where(AuditDecision.agent == agent.AGENT)
            .order_by(AuditDecision.id)
        )
    )


def _ready(db, web, pm) -> int:
    pid = closing_project(db, pm)
    assert create(web, pid).status_code == 201
    return pid


def test_a_draft_fills_the_figures_marks_sections_unreviewed_and_suggests_lessons(
    db, web, pm, fake, mode
):
    pid = _ready(db, web, pm)
    done = _draft(web, pid)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["replaced"] == ["delivered", "value_case", "remaining"]
    report = _report(db, pid)
    assert report.sections["delivered"] == (
        "The pilot was delivered for $1,400.00 against a final baseline of $1,000.00."
    )
    assert all(report.section_sources[n]["source"] == "agent" for n in report.sections)
    assert report.flags == []
    lessons = db.scalars(select(Lesson).where(Lesson.project_id == pid)).all()
    assert [(lesson.source, lesson.status, lesson.suggest_as_risk) for lesson in lessons] == [
        ("agent", "suggested", False)
    ]
    assert lessons[0].what_happened == "Two changes moved the baseline by $0.00."
    assert lessons[0].do_again is None
    [run] = _runs(db)
    assert run.status == "ok" and run.model == get_settings().closure_agent_model
    assert run.action_taken["lesson_ids"] == [lessons[0].id]
    assert run.cost_usd is not None


def test_the_agent_never_writes_a_benefits_value_now(db, web, pm, fake, mode):
    pid = _ready(db, web, pm)
    fake.reply = _reply(
        {
            **GOOD,
            "value_now": "three days",
            "benefits": [{"measure": "Days to plan", "value_now": "three days"}],
        }
    )
    done = _draft(web, pid)
    assert done.status_code == 200, done.text
    db.expire_all()
    # Nothing is measured: only a person enters a value now.
    assert db.scalar(select(func.count()).select_from(ClosureBenefit)) == 0
    assert web.get(f"/api/v1/projects/{pid}/closure").json()["benefits"][0]["value_now"] is None
    [run] = _runs(db)
    assert {d["part"] for d in run.output["dropped"]} == {"benefits", "value_now"}
    # And the schema has no field for one.
    assert "value_now" not in json.dumps(agent.OUTPUT_SCHEMA)


@pytest.mark.parametrize(
    "delivered",
    [
        "The pilot cost $1,500.00 in the end.",  # a number outside the keys
        "Delivered in 3 sprints.",  # a plain digit
        "Spend was {{spend_to_date}}.",  # a key the closure report doesn't have
    ],
)
def test_a_number_outside_the_figures_refuses_the_run_and_saves_nothing(
    db, web, pm, fake, mode, delivered
):
    pid = _ready(db, web, pm)
    fake.reply = _reply({**GOOD, "delivered": delivered})
    refused = _draft(web, pid)
    assert refused.status_code == 502
    report = _report(db, pid)
    assert report.sections == dict.fromkeys(("delivered", "value_case", "remaining"))
    assert report.version == 1
    assert db.scalar(select(func.count()).select_from(Lesson)) == 0
    [run] = _runs(db)
    assert run.status == "error"
    assert run.action_taken["lesson_ids"] == []


def test_a_lesson_with_a_stray_number_is_dropped(db, web, pm, fake, mode):
    pid = _ready(db, web, pm)
    lessons = [
        {**GOOD["lessons"][0], "what_happened": "It took 6 weeks to get data."},
        {**GOOD["lessons"][0], "category": "astrology"},
    ]
    fake.reply = _reply({**GOOD, "lessons": lessons})
    assert _draft(web, pid).status_code == 200
    assert db.scalar(select(func.count()).select_from(Lesson)) == 0
    [run] = _runs(db)
    assert [d["part"] for d in run.output["dropped"]] == ["lessons[0]", "lessons[1]"]


def test_a_failed_run_is_audited_and_changes_nothing(db, web, pm, fake, mode):
    pid = _ready(db, web, pm)
    fake.error = RuntimeError("overloaded")
    assert _draft(web, pid).status_code == 502
    report = _report(db, pid)
    assert report.version == 1 and report.section_sources == {}
    [run] = _runs(db)
    assert run.status == "error" and "overloaded" in run.error
    fake.error = None
    fake.reply = _reply(stop_reason="max_tokens")
    assert _draft(web, pid).status_code == 502
    assert [r.status for r in _runs(db)] == ["error", "error"]
    assert _report(db, pid).version == 1


def test_what_remains_gives_a_benefits_measure_by_date_as_a_figure(db, web, pm, fake, mode):
    pid = _ready(db, web, pm)
    bid = db.scalar(select(ValueCaseBenefit.id))
    url = REPORT.format(pid=pid)
    owned = web.put(
        f"{url}/benefits/{bid}",
        json={"version": 0, "measure_owner_id": pm.id, "measure_by": "2027-09-01"},
    )
    assert owned.status_code == 200, owned.text
    key = f"benefit_{bid}_measure_by"
    # Until the figures are read again, the agent has no key for the date.
    dry = _draft(web, pid, dry_run="true").json()["request"]["messages"][0]["content"]
    assert '"measure_by_key": null' in dry
    refreshed = web.post(f"{url}/refresh", json={"version": 1})
    assert refreshed.status_code == 200, refreshed.text
    figure = {f["key"]: f for f in refreshed.json()["report"]["figures"]}[key]
    assert figure["kind"] == "date" and figure["value"] == "2027-09-01"
    row_id = db.scalar(select(ClosureBenefit.id).where(ClosureBenefit.benefit_id == bid))
    assert figure["sources"] == [{"table": "closure_benefits", "id": row_id, "field": "measure_by"}]
    remaining = f"Days to plan has an owner, who measures it by {{{{{key}}}}}."
    fake.reply = _reply({**GOOD, "remaining": remaining})
    done = _draft(web, pid)
    assert done.status_code == 200, done.text
    sent = fake.requests[0]["messages"][0]["content"]
    assert f'"measure_by_key": "{key}"' in sent
    assert key in done.json()["figure_keys"]
    report = _report(db, pid)
    date_text = agent.key_values(report.figures)[key]
    assert report.sections["remaining"] == (
        f"Days to plan has an owner, who measures it by {date_text}."
    )
    assert report.flags == []


def test_a_persons_section_is_kept(db, web, pm, fake, mode):
    pid = _ready(db, web, pm)
    web.patch(REPORT.format(pid=pid), json={"version": 1, "sections": {"delivered": "Mine."}})
    body = _draft(web, pid).json()
    assert body["replaced"] == ["value_case", "remaining"]
    assert body["kept"] == [{"section": "delivered", "reason": agent.PERSON_OWNS}]
    assert _report(db, pid).sections["delivered"] == "Mine."
    # A repeated lesson is dropped on the second run.
    second = _draft(web, pid).json()
    assert second["lesson_ids"] == []


def test_confirming_a_section_reviews_it(db, web, pm, fake, mode):
    pid = _ready(db, web, pm)
    version = _draft(web, pid).json()["report_version"]
    confirmed = web.post(
        REPORT.format(pid=pid) + "/sections/delivered/confirm", json={"version": version}
    )
    assert confirmed.status_code == 200, confirmed.text
    states = confirmed.json()["report"]["section_states"]
    assert states["delivered"]["unreviewed"] is False
    assert states["value_case"]["unreviewed"] is True


def test_agent_mode_off_refuses_and_shadow_saves_nothing(db, web, pm, fake, mode):
    pid = _ready(db, web, pm)
    mode("off")
    assert _draft(web, pid).status_code == 409
    assert fake.requests == []
    mode("shadow")
    shadow = _draft(web, pid).json()
    assert shadow["replaced"] == [] and shadow["lesson_ids"] == []
    assert _report(db, pid).version == 1


def test_a_dry_run_calls_nothing_and_shows_the_request(db, web, pm, fake, mode):
    pid = _ready(db, web, pm)
    dry = _draft(web, pid, dry_run="true").json()
    assert fake.requests == [] and _runs(db) == []
    request = dry["request"]
    assert request["model"] == get_settings().closure_agent_model
    assert request["output_config"]["format"]["schema"] == agent.OUTPUT_SCHEMA
    assert "temperature" not in request
    content = request["messages"][0]["content"]
    assert "<closure>" in content and "Days to plan" in content
    assert pm.email not in content and pm.display_name not in content
    assert dry["cost_ceiling_usd"] is not None


def test_the_agent_works_only_in_closure(db, web, pm, fake, mode):
    pid = closing_project(db, pm, stage="delivery")
    refused = _draft(web, pid)
    assert refused.status_code == 409
    assert fake.requests == []
