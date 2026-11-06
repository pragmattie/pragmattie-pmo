"""The orchestrator (item 6.2): works out each open request's and project's next steps, runs the
agent steps the autonomy policy allows, and hands every other step to a person.

- `next_steps(db, policy)` is rules only, with no Claude call: it reads each request, project,
  risk, proposal and change request as it is now and returns its next steps, each with a step id
  from `app.autonomy`, a plain reason, how it runs now and who or what it waits on. The inbox and
  the board read the same function, so they never disagree.
- `run_pass(db, settings, client)` runs one pass. In `shadow` it records what it would do (once
  per step and subject, and again only when that changes) and changes nothing. In `enforce` an
  `agent` step calls its action, a `window` step waits for its due time unless a person holds it,
  and a `person` step gets one open inbox item. Every agent step that runs counts against the
  per-pass and daily caps, which are enforced here, never left to a model.
- The actions are a registry (`ACTIONS`, step id to function), so later items plug new ones in
  without changing the engine. A step with no action goes to a person: "no agent does this yet".
- A person step is never performed here, whatever the policy says (R1): the floor steps and the
  steps no agent may do (`PERSON_ONLY`) are refused at import if anyone registers an action for
  them, and a pass sends them to a person.

Run it with `python -m app.orchestrator once` (one pass) or `run` (a pass every
`ORCHESTRATOR_INTERVAL_SECONDS` until Ctrl+C).
"""

import argparse
import logging
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents import intake_agent, proposal_agent, risk_agent
from app.audit import next_attempt, record_decision
from app.autonomy import (
    AGENT,
    FLOOR_STEPS,
    PERSON,
    SCORE_RISK,
    STEP_AGENTS,
    WINDOW,
    AutonomyPolicy,
    effective_mode,
)
from app.config import Settings
from app.gates import PROJECT_CHECKS
from app.models import (
    AuditDecision,
    ChangeRequest,
    InboxItem,
    OrchestratorAction,
    OrchestratorLease,
    Project,
    ProjectPlan,
    ProjectProposal,
    ProjectRequest,
    ReadinessCheck,
    Risk,
    ValueCase,
    utcnow,
)
from app.models.orchestrator import REASON_MAX, open_key
from app.models.portfolio import CANCELLED
from app.models.proposal import TEXT_SECTIONS
from app.plans import missing
from app.policy import RiskPolicy
from app.proposals import plan_facts, unreviewed_sections
from app.risks import band_owner, risk_library, risk_policy

logger = logging.getLogger(__name__)

ORCHESTRATOR = "orchestrator"
TRIGGER = "orchestrator"

# Subject types, as the inbox and the audit rows name them.
REQUEST = "request"
PROJECT = "project"
RISK = "risk"
PROPOSAL = "proposal"
CHANGE_REQUEST = "change_request"

MAX_RETRIES = 3
RETRY_AFTER = timedelta(minutes=5)
LEASE_NAME = "pass"
# Longer than a pass can take: each agent call times out after 120 seconds.
LEASE_TTL = timedelta(minutes=15)
MIN_HOLD_REASON = 10
MAX_ERROR = 500
PROJECT_MANAGER = "project_manager"

# Steps a pass never performs: the floor steps (R1), and every step no agent may do (a change
# decision), whatever a policy says.
PERSON_ONLY = FLOOR_STEPS | {step for step, agents in STEP_AGENTS.items() if not agents}

NO_AGENT = "no agent does this yet"
AGENT_ALREADY_RAN = "an agent already did this step, but it is still open"
NO_LONGER_NEEDED = "the step is no longer needed"
PASS_CAP = "waiting for the next pass: this pass reached its cap of {n} agent calls"
DAY_CAP = "waiting for the next UTC day: today's agent runs reached the cap of {cap} US dollars"


# --- The next-step engine (rules only)


@dataclass(frozen=True)
class NextStep:
    """One next step for one subject.

    `mode` is how it runs now: `agent`, `window` or `person`. `waits_on` names the agent for an
    agent or window step and the role for a person step. `handoff` says why a step the policy
    gives an agent goes to a person instead (its agent is off, no agent does it yet, and so on).
    """

    step: str
    subject_type: str
    subject_id: int
    reason: str
    waits_on: str
    mode: str
    handoff: str | None = None

    @property
    def key(self) -> str:
        return open_key(self.step, self.subject_type, str(self.subject_id))

    @property
    def full_reason(self) -> str:
        text = f"{self.reason}; {self.handoff}" if self.handoff else self.reason
        return text[:REASON_MAX]


def _handoff(
    policy: AutonomyPolicy, step: str, agent_mode: str, risk: Risk | None
) -> tuple[str, str | None]:
    """How `step` runs now, and why it went to a person when the policy gave it to an agent."""
    rule = policy.steps[step]
    if step in PERSON_ONLY or rule.mode == PERSON:
        return PERSON, None
    if effective_mode(policy, step, risk) == PERSON:
        if rule.agent is not None and not policy.enabled[rule.agent]:
            return PERSON, f"the {rule.agent} is switched off"
        if step == SCORE_RISK and risk is not None and risk.needs_judgement:
            return PERSON, "the risk needs a person's judgement, so a person scores it"
        return PERSON, None
    if step not in ACTIONS:
        return PERSON, NO_AGENT
    if agent_mode != "enforce":
        return PERSON, f"the agents are off (AGENT_MODE={agent_mode})"
    return rule.mode, None


def _make(
    policy: AutonomyPolicy,
    agent_mode: str,
    step: str,
    subject_type: str,
    subject_id: int,
    reason: str,
    *,
    risk: Risk | None = None,
    who: str | None = None,
) -> NextStep:
    mode, handoff = _handoff(policy, step, agent_mode, risk)
    rule = policy.steps[step]
    waits_on = (who or rule.who) if mode == PERSON else rule.agent
    return NextStep(step, subject_type, subject_id, reason, waits_on or rule.who, mode, handoff)


def _agent_ran(db: Session, agent: str, subject_type: str, subject_id: int) -> bool:
    """Whether `agent` has a successful run recorded for the subject."""
    stmt = select(AuditDecision.id).where(
        AuditDecision.agent == agent,
        AuditDecision.subject_type == subject_type,
        AuditDecision.subject_id == str(subject_id),
        AuditDecision.status == "ok",
    )
    return db.scalar(stmt.limit(1)) is not None


def _exists(db: Session, model: type, request_id: int) -> bool:
    return db.scalar(select(model.id).where(model.request_id == request_id).limit(1)) is not None


def _request_steps(db: Session, request: ProjectRequest, make: Callable) -> list[NextStep]:
    if request.status == "submitted":
        return [
            make("pass_gate", REQUEST, request.id, "the request is waiting at the approval gate")
        ]
    if request.status != "draft":
        return []
    has_value_case = _exists(db, ValueCase, request.id)
    needs_readiness = request.uses_ai and not _exists(db, ReadinessCheck, request.id)
    if not has_value_case:
        reason = "the request has no value case"
        if needs_readiness:
            # The intake agent drafts both in one run.
            reason += " and, as it uses AI, no readiness check"
        return [make("draft_value_case", REQUEST, request.id, reason)]
    if needs_readiness:
        reason = "the request uses AI and has no readiness check"
        return [make("draft_readiness", REQUEST, request.id, reason)]
    return []


def _planning_steps(db: Session, project: Project, make: Callable) -> list[NextStep]:
    steps = []
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    gaps = ["the project has no plan"] if plan is None else missing(db, plan)
    if gaps and not _agent_ran(db, "planner_agent", PROJECT, project.id):
        reason = "the plan is incomplete: " + "; ".join(gaps)
        steps.append(make("draft_plan", PROJECT, project.id, reason[:REASON_MAX]))
    if not _agent_ran(db, risk_agent.AGENT, risk_agent.AUDIT_SUBJECT, project.id):
        reason = "the risk agent hasn't looked at this project yet"
        steps.append(make("propose_risks", PROJECT, project.id, reason))
    if all(check(db, project).met for check in PROJECT_CHECKS.values()):
        reason = f"the {len(PROJECT_CHECKS)} go-ahead checks are met"
        steps.append(make("pass_gate", PROJECT, project.id, reason))
    return steps


def _risk_steps(risk: Risk, risk_rules: RiskPolicy, make: Callable) -> list[NextStep]:
    if risk.status == "proposed":
        if risk.probability is None or risk.impact is None:
            return [
                make("score_risk", RISK, risk.id, "the risk is proposed and not scored", risk=risk)
            ]
        if band_owner(risk_rules, risk.band) == PROJECT_MANAGER:
            reason = f"the risk is proposed and scored {risk.band}"
            return [make("open_low_risk", RISK, risk.id, reason, risk=risk)]
        return []
    if risk.status == "open" and risk.escalated_to not in (None, PROJECT_MANAGER):
        reason = f"the risk is escalated to the {risk.escalated_to} and waiting for a decision"
        return [make("accept_risk", RISK, risk.id, reason, risk=risk, who=risk.escalated_to)]
    return []


def _proposal_steps(proposal: ProjectProposal, make: Callable) -> list[NextStep]:
    status = proposal.status
    if status == "draft":
        empty = [name for name in TEXT_SECTIONS if not (getattr(proposal, name) or "").strip()]
        if empty:
            reason = "the proposal has empty sections: " + ", ".join(empty)
            return [make("draft_proposal", PROPOSAL, proposal.id, reason)]
        if not unreviewed_sections(proposal):
            reason = "every section is written and reviewed, so the proposal can be submitted"
            return [make("submit_proposal", PROPOSAL, proposal.id, reason)]
        return []
    reasons = {
        "submitted": ("approve_proposal", "the proposal is submitted and waiting for approval"),
        "approved": ("send_proposal", "the proposal is approved and waiting to be sent"),
        "sent": ("client_answer", "the proposal was sent and is waiting for the client's answer"),
    }
    if status in reasons:
        step, reason = reasons[status]
        return [make(step, PROPOSAL, proposal.id, reason)]
    return []


def _change_steps(change: ChangeRequest, make: Callable) -> list[NextStep]:
    if change.status != "submitted":
        return []
    step = "decide_change_sponsor" if change.decider == "sponsor" else "decide_change_pm"
    return [make(step, CHANGE_REQUEST, change.id, "the change request is waiting for a decision")]


def next_steps(
    db: Session,
    policy: AutonomyPolicy,
    *,
    agent_mode: str = "enforce",
    risk_rules: RiskPolicy | None = None,
) -> list[NextStep]:
    """Every open subject's next steps, in a stable order: requests, then each live project with
    its risks, proposal and change requests. `agent_mode` is `AGENT_MODE`: unless it is
    `enforce`, no agent may act, so agent and window steps go to a person."""
    rules = risk_rules or risk_policy()

    def make(*args: Any, **kwargs: Any) -> NextStep:
        return _make(policy, agent_mode, *args, **kwargs)

    steps: list[NextStep] = []
    requests = select(ProjectRequest).where(ProjectRequest.status.in_(("draft", "submitted")))
    for request in db.scalars(requests.order_by(ProjectRequest.id)):
        steps.extend(_request_steps(db, request, make))
    projects = select(Project).where(
        Project.status != CANCELLED, Project.stage.in_(("planning", "delivery"))
    )
    for project in db.scalars(projects.order_by(Project.id)):
        if project.stage == "planning":
            steps.extend(_planning_steps(db, project, make))
        risks = select(Risk).where(
            Risk.project_id == project.id, Risk.status.in_(("proposed", "open"))
        )
        for risk in db.scalars(risks.order_by(Risk.id)):
            steps.extend(_risk_steps(risk, rules, make))
        proposals = select(ProjectProposal).where(ProjectProposal.project_id == project.id)
        for proposal in db.scalars(proposals.order_by(ProjectProposal.id)):
            steps.extend(_proposal_steps(proposal, make))
        changes = select(ChangeRequest).where(ChangeRequest.project_id == project.id)
        for change in db.scalars(changes.order_by(ChangeRequest.id)):
            steps.extend(_change_steps(change, make))
    return steps


# --- The actions an agent or window step runs


@dataclass(frozen=True)
class Context:
    db: Session
    settings: Settings
    client: risk_agent.ClaudeClient


@dataclass(frozen=True)
class StepAction:
    """What a step does when it runs. `uses_claude` counts it against the caps."""

    run: Callable[[Context, int], int | None]  # returns the agent's audit row id, if any
    uses_claude: bool = True


def _draft_request(ctx: Context, request_id: int) -> int:
    request = ctx.db.get(ProjectRequest, request_id)
    result = intake_agent.run(
        ctx.db,
        ctx.client,
        request,
        settings=ctx.settings,
        save_it=True,
        actor_user_id=None,
        trigger=TRIGGER,
    )
    return result.audit_id


def _propose_risks(ctx: Context, project_id: int) -> int:
    result = risk_agent.run(
        ctx.db,
        ctx.client,
        ctx.db.get(Project, project_id),
        library=risk_library(),
        policy=risk_policy(),
        settings=ctx.settings,
        save=True,
        actor_user_id=None,
        trigger=TRIGGER,
    )
    return result.audit_id


def _draft_proposal(ctx: Context, proposal_id: int) -> int:
    proposal = ctx.db.get(ProjectProposal, proposal_id)
    plan = ctx.db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == proposal.project_id))
    result = proposal_agent.run(
        ctx.db,
        ctx.client,
        proposal,
        plan_facts(ctx.db, plan),
        settings=ctx.settings,
        save_it=True,
        actor_user_id=None,
        trigger=TRIGGER,
    )
    return result.audit_id


# The same functions the "Draft with the agent" buttons use. Items 6.7 and 6.8 add score_risk,
# open_low_risk, submit_proposal and client_answer here.
ACTIONS: dict[str, StepAction] = {
    "draft_value_case": StepAction(_draft_request),
    "draft_readiness": StepAction(_draft_request),
    "propose_risks": StepAction(_propose_risks),
    "draft_proposal": StepAction(_draft_proposal),
}
assert not PERSON_ONLY & ACTIONS.keys(), "a person always takes these steps (R1)"


# --- Recording


def _record(db: Session, *, subject: tuple[str, str], **fields: Any) -> int:
    """One orchestrator audit row at the next free attempt for its subject, moving on when a
    parallel writer (a person holding an action) took the number first."""
    subject_type, subject_id = subject
    attempt = 0
    for tried in range(1, 6):
        attempt = max(
            next_attempt(
                db,
                agent=ORCHESTRATOR,
                subject_type=subject_type,
                subject_id=subject_id,
                version=1,
            ),
            attempt + 1,
        )
        try:
            with db.begin_nested():
                fields.setdefault("trigger", TRIGGER)
                row = record_decision(
                    db,
                    agent=ORCHESTRATOR,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    version=1,
                    attempt=attempt,
                    **fields,
                )
                return row.id
        except IntegrityError:
            if tried == 5:
                raise
    raise AssertionError("unreachable")


def _signals(step: NextStep, digest: str, mode: str) -> dict[str, Any]:
    return {
        "step": step.step,
        "reason": step.full_reason,
        "mode": step.mode,
        "waits_on": step.waits_on,
        "policy_digest": digest,
        "orchestrator_mode": mode,
    }


def _subject(step: NextStep) -> tuple[str, str]:
    return step.subject_type, str(step.subject_id)


# --- One pass


@dataclass
class Outcome:
    step: str
    subject_type: str
    subject_id: int
    outcome: str  # done, failed, pending, waiting, inbox, skipped, cancelled, closed, error
    detail: str


@dataclass
class PassReport:
    mode: str
    ran: bool
    busy: bool = False
    steps: int = 0
    agent_calls: int = 0
    outcomes: list[Outcome] = field(default_factory=list)


def _today_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def spent_today(db: Session, now: datetime) -> Decimal:
    """The cost of today's (UTC) agent runs the orchestrator started, from the audit rows."""
    stmt = select(func.coalesce(func.sum(AuditDecision.cost_usd), 0)).where(
        AuditDecision.trigger == TRIGGER, AuditDecision.created_at >= _today_start(now)
    )
    return Decimal(str(db.scalar(stmt) or 0))


def _take_lease(db: Session, holder: str, now: datetime) -> bool:
    """Take the pass lease, or give up when another pass holds an unexpired one."""
    lease = db.get(OrchestratorLease, LEASE_NAME, with_for_update=True, populate_existing=True)
    if lease is None:
        db.add(OrchestratorLease(name=LEASE_NAME, holder=holder, expires_at=now + LEASE_TTL))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
        return True
    if lease.expires_at > now and lease.holder != holder:
        db.rollback()
        return False
    lease.holder = holder
    lease.expires_at = now + LEASE_TTL
    db.commit()
    return True


def _release_lease(db: Session, holder: str) -> None:
    db.rollback()
    lease = db.get(OrchestratorLease, LEASE_NAME, with_for_update=True, populate_existing=True)
    if lease is not None and lease.holder == holder:
        lease.expires_at = utcnow()
    db.commit()


def _last_shadow(db: Session, step: NextStep) -> dict[str, Any] | None:
    """The signals of the newest shadow row for this step and subject."""
    stmt = (
        select(AuditDecision.signals)
        .where(
            AuditDecision.agent == ORCHESTRATOR,
            AuditDecision.subject_type == step.subject_type,
            AuditDecision.subject_id == str(step.subject_id),
            AuditDecision.status == "skipped",
        )
        .order_by(AuditDecision.id.desc())
    )
    for signals in db.scalars(stmt):
        if isinstance(signals, dict) and signals.get("step") == step.step:
            return signals
    return None


def _shadow(db: Session, steps: list[NextStep], digest: str, report: PassReport) -> None:
    """Record each would-do step once, and again only when its reason or route changes."""
    for step in steps:
        signals = _signals(step, digest, "shadow")
        last = _last_shadow(db, step)
        keys = ("reason", "mode", "waits_on")
        if last is not None and all(last.get(k) == signals[k] for k in keys):
            continue
        would = {AGENT: "run", WINDOW: "open a window", PERSON: "send to a person"}[step.mode]
        _record(
            db,
            subject=_subject(step),
            status="skipped",
            signals=signals,
            action_taken={"action": "would_" + would.replace(" ", "_"), "mode": "shadow"},
        )
        report.outcomes.append(
            Outcome(step.step, step.subject_type, step.subject_id, "skipped", f"would {would}")
        )
    db.commit()


class _Pass:
    """One enforce pass: its session, settings, client, policy and counters."""

    def __init__(
        self,
        db: Session,
        settings: Settings,
        client: risk_agent.ClaudeClient,
        policy: AutonomyPolicy,
        now: datetime,
        report: PassReport,
    ):
        self.db = db
        self.settings = settings
        self.client = client
        self.policy = policy
        self.now = now
        self.report = report
        self.calls = 0

    def outcome(self, step: NextStep, outcome: str, detail: str) -> None:
        self.report.outcomes.append(
            Outcome(step.step, step.subject_type, step.subject_id, outcome, detail)
        )

    def audit(self, step: NextStep, status: str, action: dict[str, Any], **fields: Any) -> int:
        return _record(
            self.db,
            subject=_subject(step),
            status=status,
            signals=_signals(step, self.policy.digest, "enforce"),
            action_taken=action,
            **fields,
        )

    # Inbox items and actions

    def open_inbox(self, step: NextStep, *, reason: str | None = None, action_id=None) -> InboxItem:
        item = InboxItem(
            step=step.step,
            subject_type=step.subject_type,
            subject_id=str(step.subject_id),
            who=self.policy.steps[step.step].who if step.mode != PERSON else step.waits_on,
            reason=(reason or step.full_reason)[:REASON_MAX],
            status="open",
            action_id=action_id,
            policy_digest=self.policy.digest,
            open_key=step.key,
        )
        self.db.add(item)
        self.db.flush()
        return item

    def to_person(self, step: NextStep, reason: str, action_id: int | None = None) -> None:
        item = self.open_inbox(step, reason=reason, action_id=action_id)
        self.audit(
            step,
            "ok",
            {"action": "inbox", "inbox_item_id": item.id, "who": item.who, "reason": item.reason},
        )
        self.db.commit()
        self.outcome(step, "inbox", f"for the {item.who}: {item.reason}")

    def close_stale(self, live: set[str]) -> None:
        """Close the open inbox items and pending actions whose step is no longer needed."""
        db = self.db
        items = select(InboxItem).where(InboxItem.status == "open").order_by(InboxItem.id)
        actions = (
            select(OrchestratorAction)
            .where(OrchestratorAction.status == "pending")
            .order_by(OrchestratorAction.id)
        )
        for row in [*db.scalars(items), *db.scalars(actions)]:
            if row.open_key in live:
                continue
            is_item = isinstance(row, InboxItem)
            row.status = "done" if is_item else "cancelled"
            row.open_key = None
            row.closed_at = self.now
            row.closed_reason = NO_LONGER_NEEDED
            stale = NextStep(
                row.step, row.subject_type, int(row.subject_id), NO_LONGER_NEEDED, row.who, PERSON
            )
            what = "close_inbox" if is_item else "cancel_action"
            self.audit(stale, "ok", {"action": what, "id": row.id, "reason": NO_LONGER_NEEDED})
            db.commit()
            self.outcome(stale, "closed" if is_item else "cancelled", NO_LONGER_NEEDED)

    def pending(self, step: NextStep) -> OrchestratorAction | None:
        stmt = select(OrchestratorAction).where(OrchestratorAction.open_key == step.key)
        return self.db.scalar(stmt.with_for_update())

    def already_done(self, step: NextStep) -> bool:
        stmt = select(OrchestratorAction.id).where(
            OrchestratorAction.step == step.step,
            OrchestratorAction.subject_type == step.subject_type,
            OrchestratorAction.subject_id == str(step.subject_id),
            OrchestratorAction.status == "done",
        )
        return self.db.scalar(stmt.limit(1)) is not None

    def has_open_item(self, step: NextStep) -> bool:
        stmt = select(InboxItem.id).where(InboxItem.open_key == step.key)
        return self.db.scalar(stmt) is not None

    # The caps

    def cap_reason(self) -> str | None:
        if self.calls >= self.settings.orchestrator_max_agent_calls:
            return PASS_CAP.format(n=self.settings.orchestrator_max_agent_calls)
        cap = self.settings.orchestrator_daily_cap_usd
        if spent_today(self.db, self.now) >= cap:
            self.record_day_cap(cap)
            return DAY_CAP.format(cap=f"{cap:.2f}")
        return None

    def record_day_cap(self, cap: Decimal) -> None:
        """Once per UTC day, a row saying the pass stopped for the daily cap."""
        day = self.now.date().isoformat()
        stmt = select(AuditDecision.id).where(
            AuditDecision.agent == ORCHESTRATOR,
            AuditDecision.subject_type == "orchestrator_day",
            AuditDecision.subject_id == day,
        )
        if self.db.scalar(stmt.limit(1)) is not None:
            return
        _record(
            self.db,
            subject=("orchestrator_day", day),
            status="skipped",
            signals={"daily_cap_usd": f"{cap:.2f}", "policy_digest": self.policy.digest},
            action_taken={"action": "daily_cap", "detail": DAY_CAP.format(cap=f"{cap:.2f}")},
        )
        self.db.commit()

    # One step

    def handle(self, step: NextStep) -> None:
        if self.has_open_item(step):
            return
        action = self.pending(step)
        if step.mode == PERSON or step.step in PERSON_ONLY:
            if action is not None:
                # The policy or a switch moved the step to a person while it waited.
                self.give_up(step, action, step.full_reason)
            else:
                self.to_person(step, step.full_reason)
            return
        if action is None:
            if self.already_done(step):
                self.to_person(step, f"{step.full_reason}; {AGENT_ALREADY_RAN}")
                return
            action = self.start(step)
            if action is None:
                return
        if action.next_try_at is not None and self.now < action.next_try_at:
            self.db.rollback()
            return
        if action.mode == WINDOW and self.now < action.due_at:
            self.db.rollback()
            return
        self.run(step, action)

    def start(self, step: NextStep) -> OrchestratorAction | None:
        rule = self.policy.steps[step.step]
        window = timedelta(minutes=rule.window_minutes or 0)
        action = OrchestratorAction(
            step=step.step,
            subject_type=step.subject_type,
            subject_id=str(step.subject_id),
            mode=step.mode,
            agent=rule.agent,
            who=rule.who,
            reason=step.full_reason,
            status="pending",
            due_at=self.now + window if step.mode == WINDOW else self.now,
            attempts=0,
            policy_digest=self.policy.digest,
            open_key=step.key,
        )
        self.db.add(action)
        self.db.flush()
        if step.mode == WINDOW:
            due = action.due_at.isoformat(timespec="seconds")
            self.audit(
                step,
                "ok",
                {"action": "window", "action_id": action.id, "due_at": due, "who": rule.who},
            )
            self.db.commit()
            self.outcome(step, "pending", f"runs at {due} unless the {rule.who} holds it")
            return None
        self.db.commit()
        return action

    def run(self, step: NextStep, action: OrchestratorAction) -> None:
        step_action = ACTIONS.get(step.step)
        if step_action is None:  # the registry changed since the step was found
            self.give_up(step, action, NO_AGENT)
            return
        if step_action.uses_claude:
            wait = self.cap_reason()
            if wait is not None:
                if action.wait_reason != wait:
                    action.wait_reason = wait
                    self.db.commit()
                else:
                    self.db.rollback()
                self.outcome(step, "waiting", wait)
                return
            self.calls += 1
            self.report.agent_calls += 1
        context = Context(self.db, self.settings, self.client)
        try:
            agent_audit_id = step_action.run(context, step.subject_id)
        except risk_agent.AgentError as exc:
            # The agent's own error row is flushed and kept.
            self.failed(step, action, str(exc), exc.audit_id)
            return
        except Exception as exc:
            logger.exception("orchestrator step %s failed for %s", step.step, step.key)
            self.db.rollback()
            self.failed(step, action, f"{type(exc).__name__}: {exc}", None)
            return
        action.status = "done"
        action.open_key = None
        action.wait_reason = None
        action.closed_at = self.now
        action.closed_reason = "done"
        self.audit(
            step,
            "ok",
            {"action": "run", "action_id": action.id, "agent_audit_id": agent_audit_id},
        )
        self.db.commit()
        self.outcome(step, "done", f"the {action.agent} ran")

    def failed(
        self, step: NextStep, action: OrchestratorAction, error: str, agent_audit_id: int | None
    ) -> None:
        action = self.db.get(OrchestratorAction, action.id, populate_existing=True)
        action.attempts += 1
        action.last_error = error[:MAX_ERROR]
        retry = action.attempts <= MAX_RETRIES
        details = {"action": "run", "action_id": action.id, "agent_audit_id": agent_audit_id}
        if retry:
            action.next_try_at = self.now + RETRY_AFTER
            details["next_try_at"] = action.next_try_at.isoformat(timespec="seconds")
        details["attempts"] = action.attempts
        self.audit(step, "error", details, error=error[:MAX_ERROR])
        self.db.commit()
        if retry:
            self.outcome(step, "failed", f"try {action.attempts} failed; retrying in 5 minutes")
            return
        reason = f"{step.full_reason}; the {action.agent} failed {action.attempts} times"
        self.give_up(step, action, reason)

    def give_up(self, step: NextStep, action: OrchestratorAction, reason: str) -> None:
        action.status = "cancelled"
        action.open_key = None
        action.closed_at = self.now
        action.closed_reason = reason[:REASON_MAX]
        self.db.flush()
        self.to_person(step, reason, action_id=action.id)


def run_pass(
    db: Session,
    settings: Settings,
    client: risk_agent.ClaudeClient,
    *,
    policy: AutonomyPolicy | None = None,
    now: datetime | None = None,
) -> PassReport:
    """One pass. `off` does nothing; another pass running makes this one give up (`busy`).
    An error on one subject is recorded and the pass carries on with the rest."""
    mode = settings.orchestrator_mode
    report = PassReport(mode=mode, ran=False)
    if mode == "off":
        return report
    from app.policy import get_autonomy

    now = now or utcnow()
    holder = uuid.uuid4().hex
    if not _take_lease(db, holder, now):
        report.busy = True
        return report
    try:
        rules = policy or get_autonomy()
        steps = next_steps(db, rules, agent_mode=settings.agent_mode)
        report.ran = True
        report.steps = len(steps)
        if mode == "shadow":
            _shadow(db, steps, rules.digest, report)
            return report
        work = _Pass(db, settings, client, rules, now, report)
        work.close_stale({step.key for step in steps})
        for step in steps:
            try:
                work.handle(step)
            except Exception as exc:
                logger.exception("orchestrator failed on %s", step.key)
                db.rollback()
                error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR]
                work.audit(step, "error", {"action": "handle"}, error=error)
                db.commit()
                work.outcome(step, "error", error)
        return report
    finally:
        _release_lease(db, holder)


# --- Holding a window


class HoldRefused(Exception):
    def __init__(self, code: int, refusal: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.refusal = refusal
        self.detail = detail


def hold(
    db: Session, action: OrchestratorAction, *, user_id: int, role: str, reason: str
) -> InboxItem:
    """A person holds a pending action: it stops, and its step becomes an inbox item. The caller
    has locked the row, checked the role and the reason, and commits."""
    now = utcnow()
    key = action.open_key
    action.status = "held"
    action.open_key = None
    action.held_by = user_id
    action.hold_reason = reason
    action.held_at = now
    action.closed_at = now
    action.closed_reason = "held by a person"
    db.flush()
    item = db.scalar(select(InboxItem).where(InboxItem.open_key == key))
    if item is None:
        item = InboxItem(
            step=action.step,
            subject_type=action.subject_type,
            subject_id=action.subject_id,
            who=action.who,
            reason=f"{action.reason}; held by the {role}: {reason}"[:REASON_MAX],
            status="open",
            action_id=action.id,
            policy_digest=action.policy_digest,
            open_key=key,
        )
        db.add(item)
        db.flush()
    _record(
        db,
        subject=(action.subject_type, action.subject_id),
        trigger="human",
        status="ok",
        actor_user_id=user_id,
        signals={
            "step": action.step,
            "reason": action.reason,
            "policy_digest": action.policy_digest,
        },
        action_taken={
            "action": "hold",
            "action_id": action.id,
            "inbox_item_id": item.id,
            "reason": reason,
        },
    )
    return item


# --- The command line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.orchestrator", description="Run the orchestrator."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("once", help="run one pass")
    commands.add_parser("run", help="run a pass every ORCHESTRATOR_INTERVAL_SECONDS until Ctrl+C")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    from app.config import get_settings
    from app.db import SessionLocal

    settings = get_settings()
    client = risk_agent.AnthropicClient(settings)

    def one_pass() -> None:
        with SessionLocal() as db:
            report = run_pass(db, settings, client)
        print(
            f"orchestrator: mode {report.mode}, ran {report.ran}, busy {report.busy}, "
            f"{report.steps} step(s), {report.agent_calls} agent call(s), "
            f"{len(report.outcomes)} outcome(s)"
        )
        for o in report.outcomes:
            print(f"  {o.step} {o.subject_type} {o.subject_id}: {o.outcome} ({o.detail})")

    try:
        one_pass()
        while args.command == "run":
            time.sleep(settings.orchestrator_interval_seconds)
            one_pass()
    except KeyboardInterrupt:
        print("orchestrator: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
