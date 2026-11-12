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
- The proposal (item 6.8): `draft_proposal` creates a client project's proposal once its plan is
  complete and has the proposal agent fill only its empty sections (a section it leaves empty
  fails the step, so the usual retries and then the project manager apply); `submit_proposal`
  submits a draft once every section is written and reviewed by a person and the checker has no
  open flags (open flags send it to the project manager until fixed); `client_answer` is the
  simulated client's answer (`app.simulated_client`), for a simulated client only and only after
  its delay; a real client's answer goes to a person. Each action checks again when it runs and,
  when its subject changed, writes a skipped row and changes nothing. Approving and sending stay
  with people.
- Status reports (item 7.4): `draft_status_report` notices that a project in delivery has a
  report due (today is on or after the latest period's due day) and creates that period's draft
  (no person; its own audit row) and has the status report agent draft its text. Only the latest
  due period is drafted, never an older one, and only once: never when the period has a
  published report, when the agent has already drafted one of its reports, or when its only
  reports were withdrawn. A draft a person already made gets only its empty sections drafted.
  `publish_status_report` puts every other draft in the project manager's inbox: only a person
  publishes (a floor step).
- Report flags (item 7.5), person steps raised in the Inbox, with limits in working days from the
  reporting policy file (`flags`): `report_overdue_pm` when a period of a project in delivery has
  no published report `overdue_pm` working days after its due day, and `report_overdue_sponsor`
  too after `overdue_sponsor` days; `report_unread` when the project's sponsor hasn't opened a
  published report `unread` working days after it was published. A period whose report was
  published and then withdrawn isn't overdue, and a withdrawn report is never unread. Each flag
  leaves by itself once the report is published or read: its step is no longer given, so the
  Inbox drops it at once and the next pass closes it, with an audit row as for any step.

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

from app import delivery, simulated_client
from app import policy as policies
from app import proposals as proposal_rules
from app import reports as report_rules
from app.agents import (
    intake_agent,
    planner_agent,
    proposal_agent,
    risk_agent,
    status_report_agent,
)
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
from app.changes import working_days
from app.config import Settings, get_settings
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
    StatusReport,
    ValueCase,
    utcnow,
)
from app.models.orchestrator import REASON_MAX, open_key
from app.models.portfolio import CANCELLED, LIVE_STAGES, is_live
from app.models.report import DRAFT, PUBLISHED, StatusReportRead
from app.plans import missing
from app.policy import ReportFlags, ReportingPolicy, RiskPolicy, score_risk
from app.proposals import empty_sections, plan_facts, submit_refusal, unreviewed_sections
from app.risks import (
    apply_score,
    band_owner,
    decide_proposed,
    risk_library,
    risk_policy,
    snapshot,
)

logger = logging.getLogger(__name__)

ORCHESTRATOR = "orchestrator"
TRIGGER = "orchestrator"

# Subject types, as the inbox and the audit rows name them.
REQUEST = "request"
PROJECT = "project"
RISK = "risk"
PROPOSAL = "proposal"
CHANGE_REQUEST = "change_request"
STATUS_REPORT = "status_report"

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

# Steps that come back for the same subject on a new occasion (a project's report each period):
# their reason names the occasion, and an action already done counts only for the same reason.
RECURRING_STEPS = frozenset({"draft_status_report"})

NO_AGENT = "no agent does this yet"
AGENT_ALREADY_RAN = "an agent already did this step, but it is still open"
NO_LONGER_NEEDED = "the step is no longer needed"
WHO_CHANGED = "the step now waits on another role"
AGENT_BACK = "agent available again"
NOT_APPLIED = "a person held the action during the agent call, so its result was not applied"
LEASE_LOST = "another pass took over the lease during the agent call, so its result was not applied"
LEASE_LOST_BEFORE = "another pass took over the lease, so this pass stopped before the agent call"
PASS_CAP = "waiting for the next pass: this pass reached its cap of {n} agent calls"
DAY_CAP = "waiting for the next UTC day: today's agent runs reached the cap of {cap} US dollars"


# --- The next-step engine (rules only)


@dataclass(frozen=True)
class NextStep:
    """One next step for one subject.

    `mode` is how it runs now: `agent`, `window` or `person`. `waits_on` names the agent for an
    agent or window step and the role for a person step. `handoff` says why a step the policy
    gives an agent goes to a person instead (its agent is off, no agent does it yet, and so on).
    `unavailable` is set when that is only because the agent can't act (switched off, not built
    yet, or `AGENT_MODE` not `enforce`), so the inbox item can be cancelled when it can again.
    """

    step: str
    subject_type: str
    subject_id: int
    reason: str
    waits_on: str
    mode: str
    handoff: str | None = None
    unavailable: bool = False

    @property
    def key(self) -> str:
        return open_key(self.step, self.subject_type, str(self.subject_id))

    @property
    def full_reason(self) -> str:
        text = f"{self.reason}; {self.handoff}" if self.handoff else self.reason
        return text[:REASON_MAX]


def _handoff(
    policy: AutonomyPolicy, step: str, agent_mode: str, risk: Risk | None
) -> tuple[str, str | None, bool]:
    """How `step` runs now, why it went to a person when the policy gave it to an agent, and
    whether that is only because the agent can't act."""
    rule = policy.steps[step]
    if step in PERSON_ONLY or rule.mode == PERSON:
        return PERSON, None, False
    if effective_mode(policy, step, risk) == PERSON:
        if rule.agent is not None and not policy.enabled[rule.agent]:
            return PERSON, f"the {rule.agent} is switched off", True
        if step == SCORE_RISK and risk is not None and risk.needs_judgement:
            return PERSON, "the risk needs a person's judgement, so a person scores it", False
        return PERSON, None, False
    if step not in ACTIONS:
        return PERSON, NO_AGENT, True
    if agent_mode != "enforce":
        return PERSON, f"the agents are off (AGENT_MODE={agent_mode})", True
    return rule.mode, None, False


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
    person: str | None = None,
    blocked: bool = False,
) -> NextStep:
    """One next step. `person` sends it to a person whatever the policy says, with that text as
    the reason why (a Medium or High risk agent suggestion, which only a person may open).
    `blocked` marks that only until the agent may act again (open checker flags a person fixes),
    so the inbox item gives way to the agent then, as for a switched-off agent."""
    if person is not None:
        mode, handoff, unavailable = PERSON, person, blocked
    else:
        mode, handoff, unavailable = _handoff(policy, step, agent_mode, risk)
    rule = policy.steps[step]
    waits_on = (who or rule.who) if mode == PERSON else rule.agent
    return NextStep(
        step, subject_type, subject_id, reason, waits_on or rule.who, mode, handoff, unavailable
    )


def person_who(policy: AutonomyPolicy, step: NextStep) -> str:
    """The role an inbox item for `step` waits on: the step's `who` for a person step (a risk's
    escalation role, for instance), else the role the policy gives the step."""
    return step.waits_on if step.mode == PERSON else policy.steps[step.step].who


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


def _plan_complete(db: Session, project_id: int) -> bool:
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project_id))
    return plan is not None and not missing(db, plan)


def _has_proposal(db: Session, project_id: int) -> bool:
    stmt = select(ProjectProposal.id).where(ProjectProposal.project_id == project_id)
    return db.scalar(stmt.limit(1)) is not None


NEEDS_PROPOSAL = "the plan is complete and the client project has no proposal yet"


def _needs_proposal(db: Session, project: Project) -> bool:
    """A client project whose plan is complete and which has no proposal (item 6.8)."""
    return (
        project.client_id is not None
        and _plan_complete(db, project.id)
        and not _has_proposal(db, project.id)
    )


def _planning_steps(db: Session, project: Project, make: Callable) -> list[NextStep]:
    steps = []
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    gaps = ["the project has no plan"] if plan is None else missing(db, plan)
    if gaps and not _agent_ran(db, planner_agent.AGENT, planner_agent.AUDIT_SUBJECT, project.id):
        reason = "the plan is incomplete: " + "; ".join(gaps)
        steps.append(make("draft_plan", PROJECT, project.id, reason[:REASON_MAX]))
    if not gaps and _needs_proposal(db, project):
        steps.append(make("draft_proposal", PROJECT, project.id, NEEDS_PROPOSAL))
    if not _agent_ran(db, risk_agent.AGENT, risk_agent.AUDIT_SUBJECT, project.id):
        reason = "the risk agent hasn't looked at this project yet"
        steps.append(make("propose_risks", PROJECT, project.id, reason))
    if all(check(db, project).met for check in PROJECT_CHECKS.values()):
        reason = f"the {len(PROJECT_CHECKS)} go-ahead checks are met"
        steps.append(make("pass_gate", PROJECT, project.id, reason))
    return steps


SUGGESTION_TO_PERSON = "so the project manager opens it or changes the score"


def suggested_score(risk: Risk, risk_rules: RiskPolicy) -> tuple[int, str] | None:
    """The score and band of the risk agent's suggestion, from the risk policy; none without one."""
    if risk.suggested_probability is None or risk.suggested_impact is None:
        return None
    score, band = score_risk(risk_rules, risk.suggested_probability, risk.suggested_impact)
    return (score, band) if score is not None and band is not None else None


def low_risk_reason(risk: Risk, risk_rules: RiskPolicy) -> str | None:
    """Why `open_low_risk` applies to the risk now, or None when it doesn't: a proposed risk
    with no judgement needed, whose own score, or else the agent's suggestion, is in a band the
    project manager owns. The reason names the suggestion's audit row, so a window planned for
    one suggestion never applies another (`_open_low_risk` compares it)."""
    if risk.status != "proposed" or risk.needs_judgement:
        return None
    if risk.probability is not None and risk.impact is not None:
        if band_owner(risk_rules, risk.band) != PROJECT_MANAGER:
            return None
        return f"the risk is proposed and scored {risk.band}"
    suggestion = suggested_score(risk, risk_rules)
    if suggestion is None or band_owner(risk_rules, suggestion[1]) != PROJECT_MANAGER:
        return None
    score, band = suggestion
    return (
        f"the risk agent suggested {band} (probability {risk.suggested_probability}, impact "
        f"{risk.suggested_impact}, score {score}; suggestion {risk.suggested_audit_id})"
    )


def judgement_reason(db: Session, risk: Risk) -> str | None:
    """The reason the risk agent gave when it judged the risk ethical or reputational, from its
    newest scoring run on the risk; none when its newest run made no such judgement."""
    stmt = (
        select(AuditDecision.action_taken)
        .where(
            AuditDecision.agent == risk_agent.AGENT,
            AuditDecision.subject_type == risk_agent.SCORE_SUBJECT,
            AuditDecision.subject_id == str(risk.id),
            AuditDecision.status == "ok",
        )
        .order_by(AuditDecision.id.desc())
        .limit(1)
    )
    action = db.scalar(stmt)
    if not isinstance(action, dict) or action.get("mode") != "enforce":
        return None
    if not action.get("needs_judgement"):
        return None
    return f"the risk agent judged it {action.get('judgement')}: {action.get('reason')}"


def _risk_steps(db: Session, risk: Risk, risk_rules: RiskPolicy, make: Callable) -> list[NextStep]:
    if risk.status == "proposed":
        if risk.probability is None or risk.impact is None:
            if risk.needs_judgement:
                # Never the agent (R9): `score_risk` goes to a person for this risk.
                reason = "the risk is proposed and not scored"
                judged = judgement_reason(db, risk)
                if judged is not None:
                    reason = f"{reason}; {judged}"
                return [make("score_risk", RISK, risk.id, reason[:REASON_MAX], risk=risk)]
            low = low_risk_reason(risk, risk_rules)
            if low is not None:
                return [make("open_low_risk", RISK, risk.id, low[:REASON_MAX], risk=risk)]
            suggestion = suggested_score(risk, risk_rules)
            if suggestion is None:
                reason = "the risk is proposed and not scored"
                return [make("score_risk", RISK, risk.id, reason, risk=risk)]
            # A Medium or High suggestion: opening it escalates it, so a person decides.
            score, band = suggestion
            reason = (
                f"the risk agent suggested {band} (probability {risk.suggested_probability}, "
                f"impact {risk.suggested_impact}, score {score}); opening it escalates it to the "
                f"{(band_owner(risk_rules, band) or 'owner').replace('_', ' ')}"
            )
            return [
                make(
                    "score_risk",
                    RISK,
                    risk.id,
                    reason,
                    risk=risk,
                    person=SUGGESTION_TO_PERSON,
                    who=PROJECT_MANAGER,
                )
            ]
        low = low_risk_reason(risk, risk_rules)
        if low is not None:
            return [make("open_low_risk", RISK, risk.id, low, risk=risk)]
        return []
    if risk.status == "open" and risk.escalated_to not in (None, PROJECT_MANAGER):
        reason = f"the risk is escalated to the {risk.escalated_to} and waiting for a decision"
        return [make("accept_risk", RISK, risk.id, reason, risk=risk, who=risk.escalated_to)]
    return []


FLAGS_OPEN = "the checker has {n} open flag(s), so the project manager fixes them first"
REAL_CLIENT = "the client is real, so a person records its answer"
TO_SIMULATED = (
    "the proposal was sent to a simulated client {minutes} or more minutes ago and has no answer"
)


def _draft_steps(db: Session, proposal: ProjectProposal, make: Callable) -> list[NextStep]:
    """A draft's next step. The agent drafts only empty sections, and only once the plan is
    complete; a written section waits for a person's review; once every section is written and
    reviewed it is submitted, unless the checker has flags open."""
    empty = empty_sections(proposal)
    if empty:
        if not _plan_complete(db, proposal.project_id):
            return []
        reason = "the proposal has empty sections: " + ", ".join(empty)
        return [make("draft_proposal", PROPOSAL, proposal.id, reason)]
    if unreviewed_sections(proposal):
        return []
    reason = "every section is written and reviewed, so the proposal can be submitted"
    refusal = submit_refusal(db, proposal, require_filled=True)
    if refusal is not None and refusal.refusal == "flags_open":
        flags = FLAGS_OPEN.format(n=len(refusal.extra["flags"]))
        return [
            make(
                "submit_proposal",
                PROPOSAL,
                proposal.id,
                reason,
                person=flags,
                who=PROJECT_MANAGER,
                blocked=True,
            )
        ]
    return [make("submit_proposal", PROPOSAL, proposal.id, reason)]


def _answer_steps(
    db: Session, proposal: ProjectProposal, make: Callable, now: datetime
) -> list[NextStep]:
    """A sent proposal waits for the client. A real client's answer is a person's to record; a
    simulated client's is the simulation's once its delay is over (nothing to do until then)."""
    reason = "the proposal was sent and is waiting for the client's answer"
    if not simulated_client.is_simulated(db, proposal):
        return [
            make(
                "client_answer",
                PROPOSAL,
                proposal.id,
                reason,
                person=REAL_CLIENT,
                who=PROJECT_MANAGER,
            )
        ]
    delay = get_settings().simulated_client_delay_minutes
    due = simulated_client.due_at(db, proposal, delay)
    if due is None or now < due:
        return []
    reason = TO_SIMULATED.format(minutes=delay)
    return [make("client_answer", PROPOSAL, proposal.id, reason)]


def _proposal_steps(
    db: Session, proposal: ProjectProposal, make: Callable, now: datetime
) -> list[NextStep]:
    status = proposal.status
    if status == "draft":
        return _draft_steps(db, proposal, make)
    if status == "sent":
        return _answer_steps(db, proposal, make, now)
    reasons = {
        "submitted": ("approve_proposal", "the proposal is submitted and waiting for approval"),
        "approved": ("send_proposal", "the proposal is approved and waiting to be sent"),
    }
    if status in reasons:
        step, reason = reasons[status]
        return [make(step, PROPOSAL, proposal.id, reason)]
    return []


REPORT_DUE = "the status report for {start} to {end} was due on {due} and its text isn't drafted"
REPORT_TO_PUBLISH = (
    "the status report for {start} to {end} is a draft: the project manager reviews it and "
    "publishes it"
)


def _due_period(db: Session, project: Project) -> tuple[ProjectPlan, report_rules.Period] | None:
    """The plan and the latest period whose report is due (today is on or after its due day),
    for a live project in delivery with a reporting period and baseline 1; else None."""
    if delivery.refusal(project) is not None:
        return None
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    if plan is None or plan.report_period is None or plan.report_due_day is None:
        return None
    first = report_rules.first_baseline(db, project.id)
    if first is None:
        return None
    today = delivery.today()
    due = [
        p
        for p in report_rules.periods(
            plan.report_period, plan.report_due_day, first.created_at.date(), today
        )
        if p.due_on <= today
    ]
    return (plan, due[-1]) if due else None


def _agent_drafted(db: Session, report_ids: list[int]) -> bool:
    """Whether the status report agent has saved a draft (an `enforce` run) on any of them."""
    if not report_ids:
        return False
    stmt = select(AuditDecision.action_taken).where(
        AuditDecision.agent == status_report_agent.AGENT,
        AuditDecision.subject_type == status_report_agent.AUDIT_SUBJECT,
        AuditDecision.subject_id.in_([str(i) for i in report_ids]),
        AuditDecision.status == "ok",
    )
    return any(
        isinstance(action, dict) and action.get("mode") == "enforce" for action in db.scalars(stmt)
    )


def report_due(db: Session, project: Project) -> tuple[str, StatusReport | None] | None:
    """Why `draft_status_report` applies to the project now, and the period's draft if a person
    already made one; None when it doesn't apply: no report is due, the period has a published
    report, the agent already drafted one of its reports, its only reports were withdrawn, or
    its draft has no empty section."""
    found = _due_period(db, project)
    if found is None:
        return None
    _, period = found
    reports = list(
        db.scalars(
            select(StatusReport)
            .where(StatusReport.project_id == project.id, StatusReport.period_end == period.end)
            .order_by(StatusReport.id)
        )
    )
    if any(r.status == PUBLISHED for r in reports) or _agent_drafted(db, [r.id for r in reports]):
        return None
    draft = next((r for r in reports if r.status == DRAFT), None)
    if draft is None and reports:
        return None
    if draft is not None and not report_rules.empty_sections(draft):
        return None
    reason = REPORT_DUE.format(
        start=period.start.isoformat(), end=period.end.isoformat(), due=period.due_on.isoformat()
    )
    return reason, draft


# --- Report flags (item 7.5)

REPORT_OVERDUE = (
    "{n} period(s) have no published status report {limit} or more working days after their "
    "due day; the oldest is {start} to {end}"
)
REPORT_UNREAD = (
    "the sponsor hasn't opened the status report for {start} to {end}, published {limit} or more "
    "working days ago"
)
OVERDUE_STEPS = (("report_overdue_pm", "overdue_pm"), ("report_overdue_sponsor", "overdue_sponsor"))


def overdue_periods(db: Session, project: Project, limit: int) -> list[report_rules.Period]:
    """The project's periods, oldest first, with no report published (a report published and
    then withdrawn counts as published) `limit` or more working days after their due day
    (`due_on`), so weekly, fortnightly and monthly plans count alike; none unless the project is
    live and in delivery, with a reporting period and baseline 1."""
    if delivery.refusal(project) is not None:
        return []
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    if plan is None or plan.report_period is None or plan.report_due_day is None:
        return []
    first = report_rules.first_baseline(db, project.id)
    if first is None:
        return []
    today = delivery.today()
    published = set(
        db.scalars(
            select(StatusReport.period_end).where(
                StatusReport.project_id == project.id, StatusReport.published_at.is_not(None)
            )
        )
    )
    return [
        p
        for p in report_rules.periods(
            plan.report_period, plan.report_due_day, first.created_at.date(), today
        )
        if p.end not in published and working_days(p.due_on, today) >= limit
    ]


def _overdue_steps(
    db: Session, project: Project, make: Callable, flags: ReportFlags
) -> list[NextStep]:
    steps = []
    for step, field_name in OVERDUE_STEPS:
        limit = getattr(flags, field_name)
        late = overdue_periods(db, project, limit)
        if late:
            reason = REPORT_OVERDUE.format(
                n=len(late),
                limit=limit,
                start=late[0].start.isoformat(),
                end=late[0].end.isoformat(),
            )
            steps.append(make(step, PROJECT, project.id, reason))
    return steps


def sponsor_opened(db: Session, report: StatusReport, sponsor_id: int) -> bool:
    stmt = select(StatusReportRead.id).where(
        StatusReportRead.report_id == report.id, StatusReportRead.user_id == sponsor_id
    )
    return db.scalar(stmt.limit(1)) is not None


def is_unread(db: Session, report: StatusReport, flags: ReportFlags) -> bool:
    """Whether a published report's project sponsor hasn't opened it `unread` or more working
    days after it was published. A report whose project names no sponsor, or that was
    withdrawn, is never unread."""
    if report.status != PUBLISHED or report.published_at is None:
        return False
    project = db.get(Project, report.project_id)
    if project is None or project.sponsor_user_id is None:
        return False
    if working_days(report.published_at.date(), delivery.today()) < flags.unread:
        return False
    return not sponsor_opened(db, report, project.sponsor_user_id)


def _delivery_steps(
    db: Session, project: Project, make: Callable, flags: Callable[[], ReportFlags]
) -> list[NextStep]:
    steps = []
    due = report_due(db, project)
    if due is not None:
        steps.append(make("draft_status_report", PROJECT, project.id, due[0]))
    steps.extend(_overdue_steps(db, project, make, flags()))
    return steps


def _report_steps(
    db: Session, report: StatusReport, make: Callable, flags: Callable[[], ReportFlags]
) -> list[NextStep]:
    """A draft goes to the project manager to review and publish, unless the agent is about to
    draft it; a published report the sponsor hasn't opened in time goes to the sponsor."""
    if report.status == PUBLISHED:
        if not is_unread(db, report, flags()):
            return []
        reason = REPORT_UNREAD.format(
            start=report.period_start.isoformat(),
            end=report.period_end.isoformat(),
            limit=flags().unread,
        )
        return [make("report_unread", STATUS_REPORT, report.id, reason)]
    if report.status != DRAFT:
        return []
    due = report_due(db, db.get(Project, report.project_id))
    if due is not None and due[1] is not None and due[1].id == report.id:
        return []
    reason = REPORT_TO_PUBLISH.format(
        start=report.period_start.isoformat(), end=report.period_end.isoformat()
    )
    return [make("publish_status_report", STATUS_REPORT, report.id, reason)]


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
    reporting: ReportingPolicy | None = None,
    now: datetime | None = None,
) -> list[NextStep]:
    """Every open subject's next steps, in a stable order: requests, then each live project with
    its risks, proposal, change requests and status reports. `agent_mode` is `AGENT_MODE`:
    unless it is `enforce`, no agent may act, so agent and window steps go to a person. `now`
    (the pass's time) decides whether the simulated client's delay is over. The reporting
    policy (for the report flags) is read only when a project in delivery needs it."""
    rules = risk_rules or risk_policy()
    now = now or utcnow()
    flags = _flags_reader(reporting)

    def make(*args: Any, **kwargs: Any) -> NextStep:
        return _make(policy, agent_mode, *args, **kwargs)

    steps: list[NextStep] = []
    requests = select(ProjectRequest).where(ProjectRequest.status.in_(("draft", "submitted")))
    for request in db.scalars(requests.order_by(ProjectRequest.id)):
        steps.extend(_request_steps(db, request, make))
    projects = select(Project).where(Project.status != CANCELLED, Project.stage.in_(LIVE_STAGES))
    for project in db.scalars(projects.order_by(Project.id)):
        if project.stage == "planning":
            steps.extend(_planning_steps(db, project, make))
        else:
            steps.extend(_delivery_steps(db, project, make, flags))
        risks = select(Risk).where(
            Risk.project_id == project.id, Risk.status.in_(("proposed", "open"))
        )
        for risk in db.scalars(risks.order_by(Risk.id)):
            steps.extend(_risk_steps(db, risk, rules, make))
        proposals = select(ProjectProposal).where(ProjectProposal.project_id == project.id)
        for proposal in db.scalars(proposals.order_by(ProjectProposal.id)):
            steps.extend(_proposal_steps(db, proposal, make, now))
        changes = select(ChangeRequest).where(ChangeRequest.project_id == project.id)
        for change in db.scalars(changes.order_by(ChangeRequest.id)):
            steps.extend(_change_steps(change, make))
        reports = select(StatusReport).where(
            StatusReport.project_id == project.id, StatusReport.status.in_((DRAFT, PUBLISHED))
        )
        for report in db.scalars(reports.order_by(StatusReport.id)):
            steps.extend(_report_steps(db, report, make, flags))
    return steps


def _flags_reader(reporting: ReportingPolicy | None) -> Callable[[], ReportFlags]:
    """The report flags' limits, read from the reporting policy file on first use only."""
    found: list[ReportFlags] = []

    def flags() -> ReportFlags:
        if not found:
            found.append((reporting or policies.get_reporting()).flags)
        return found[0]

    return flags


def subject_steps(
    db: Session,
    policy: AutonomyPolicy,
    subject_type: str,
    subject_id: int,
    *,
    agent_mode: str = "enforce",
    risk_rules: RiskPolicy | None = None,
    reporting: ReportingPolicy | None = None,
    now: datetime | None = None,
) -> list[NextStep]:
    """One subject's next steps, exactly as `next_steps` gives them, without reading the rest.
    The inbox uses it to drop an item a person has just decided, before the next pass."""
    rules = risk_rules or risk_policy()
    now = now or utcnow()
    flags = _flags_reader(reporting)

    def make(*args: Any, **kwargs: Any) -> NextStep:
        return _make(policy, agent_mode, *args, **kwargs)

    if subject_type == REQUEST:
        request = db.get(ProjectRequest, subject_id)
        return _request_steps(db, request, make) if request is not None else []
    if subject_type == PROJECT:
        project = db.get(Project, subject_id)
        if not is_live(project):
            return []
        if project.stage == "planning":
            return _planning_steps(db, project, make)
        return _delivery_steps(db, project, make, flags)
    model = {
        RISK: Risk,
        PROPOSAL: ProjectProposal,
        CHANGE_REQUEST: ChangeRequest,
        STATUS_REPORT: StatusReport,
    }.get(subject_type)
    row = db.get(model, subject_id) if model is not None else None
    if row is None or not is_live(db.get(Project, row.project_id)):
        return []
    if isinstance(row, Risk):
        return _risk_steps(db, row, rules, make) if row.status in ("proposed", "open") else []
    if isinstance(row, ProjectProposal):
        return _proposal_steps(db, row, make, now)
    if isinstance(row, StatusReport):
        return _report_steps(db, row, make, flags)
    return _change_steps(row, make)


# --- The actions an agent or window step runs


@dataclass(frozen=True)
class Context:
    """What an action gets. `policy` is the pass's autonomy policy; `planned_reason` is the
    reason the step had when its action was planned (a window's start), so an action can tell
    whether its subject changed while it waited; `subject_type` is the step's subject type (a
    step may have more than one), and `now` the pass's time."""

    db: Session
    settings: Settings
    client: risk_agent.ClaudeClient
    policy: AutonomyPolicy | None = None
    planned_reason: str = ""
    subject_type: str = ""
    now: datetime | None = None


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


def _draft_plan(ctx: Context, project_id: int) -> int:
    result = planner_agent.run(
        ctx.db,
        ctx.client,
        ctx.db.get(Project, project_id),
        settings=ctx.settings,
        save_it=True,
        actor_user_id=None,
        trigger=TRIGGER,
    )
    return result.audit_id


PROPOSAL_EXISTS = (
    "the project no longer needs a proposal from the orchestrator (it has one, its plan is no "
    "longer complete, or it is no longer a live client project)"
)
NOTHING_TO_DRAFT = (
    "the proposal no longer has an empty section to draft (or is no longer a draft, or its plan "
    "is no longer complete)"
)
LEFT_EMPTY = "the proposal agent left these sections empty: {sections}"
SUBMIT_CHANGED = "the proposal changed while the window waited, so it was not submitted"


def _draft_proposal(ctx: Context, subject_id: int) -> int:
    """Draft a client project's proposal (item 6.8). For a project with none, create it from the
    plan (no person; its own audit row) first. The proposal agent then fills only the empty
    sections, as unreviewed drafts for a person to review."""
    db = ctx.db
    if ctx.subject_type == PROJECT:
        project = db.get(Project, subject_id, with_for_update=True, populate_existing=True)
        if not (is_live(project) and _needs_proposal(db, project)):
            return _skipped(ctx, "draft_proposal", subject_id, PROPOSAL_EXISTS, subject=PROJECT)
        plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
        proposal = proposal_rules.new_proposal(db, plan, None)
        proposal_rules.record(
            db,
            proposal,
            actor_user_id=None,
            trigger=TRIGGER,
            action="create",
            before=None,
            by=ORCHESTRATOR,
        )
    else:
        proposal = db.get(ProjectProposal, subject_id, with_for_update=True, populate_existing=True)
        project = db.get(Project, proposal.project_id) if proposal is not None else None
        if (
            proposal is None
            or proposal.status != "draft"
            or not empty_sections(proposal)
            or not is_live(project)
            or not _plan_complete(db, proposal.project_id)
        ):
            return _skipped(ctx, "draft_proposal", subject_id, NOTHING_TO_DRAFT, subject=PROPOSAL)
        plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == proposal.project_id))
    result = proposal_agent.run(
        db,
        ctx.client,
        proposal,
        plan_facts(db, plan),
        settings=ctx.settings,
        save_it=True,
        actor_user_id=None,
        trigger=TRIGGER,
        only_empty=True,
    )
    left = empty_sections(proposal)
    if left:
        # The agent left a section empty (null): the step failed, so the usual retries and then
        # the project manager's inbox apply, rather than another call on every pass.
        raise risk_agent.AgentError(LEFT_EMPTY.format(sections=", ".join(left)), result.audit_id)
    return result.audit_id


def _submit_proposal(ctx: Context, proposal_id: int) -> int:
    """Submit a draft after its window (item 6.8), through the submit route's rules: only while
    it is still a draft with every section written and reviewed by a person and no checker flag
    open. The frozen copy has no submitter; the proposal's row says the orchestrator did it."""
    db = ctx.db
    proposal = db.get(ProjectProposal, proposal_id, with_for_update=True, populate_existing=True)
    project = db.get(Project, proposal.project_id) if proposal is not None else None
    refusal = submit_refusal(db, proposal, require_filled=True) if proposal is not None else None
    if proposal is None or not is_live(project) or refusal is not None:
        return _skipped(
            ctx,
            "submit_proposal",
            proposal_id,
            SUBMIT_CHANGED,
            subject=PROPOSAL,
            refusal=refusal.refusal if refusal is not None else None,
        )
    window = ctx.policy.steps["submit_proposal"].window_minutes if ctx.policy else None
    return proposal_rules.submit(
        db,
        proposal,
        actor_user_id=None,
        trigger=TRIGGER,
        by=ORCHESTRATOR,
        step="submit_proposal",
        window_minutes=window,
    )


def _client_answer(ctx: Context, proposal_id: int) -> int:
    """The simulated client's answer (item 6.8), checked again now; skipped when it may not."""
    answer = ctx.policy.simulated_answer if ctx.policy else "accepted"
    try:
        return simulated_client.answer_now(
            ctx.db,
            proposal_id,
            answer=answer,
            delay_minutes=ctx.settings.simulated_client_delay_minutes,
            now=ctx.now or utcnow(),
        )
    except simulated_client.Skipped as exc:
        return _skipped(ctx, "client_answer", proposal_id, str(exc), subject=PROPOSAL)


REPORT_CHANGED = (
    "the project no longer needs this report drafted (a person published, withdrew or wrote it, "
    "another period fell due, or it left delivery)"
)


def _draft_status_report(ctx: Context, project_id: int) -> int:
    """Draft the due report (item 7.4): create the period's draft when it has none (no person;
    its own audit row), then have the status report agent fill its empty sections, as
    unreviewed drafts for the project manager to review. Skipped, changing nothing, when the
    step no longer applies or now names another period."""
    db = ctx.db
    project = db.get(Project, project_id, with_for_update=True, populate_existing=True)
    due = report_due(db, project) if is_live(project) else None
    if due is None or due[0][:REASON_MAX] != ctx.planned_reason:
        return _skipped(ctx, "draft_status_report", project_id, REPORT_CHANGED, subject=PROJECT)
    report = due[1]
    if report is None:
        plan, period = _due_period(db, project)
        report = report_rules.new_draft(
            db, project.id, plan, period, policies.get_reporting(), created_by=None
        )
        report_rules.record(
            db,
            report,
            actor_user_id=None,
            before=None,
            action="created",
            trigger=TRIGGER,
            by=ORCHESTRATOR,
            step="draft_status_report",
        )
    result = status_report_agent.run(
        db,
        ctx.client,
        report,
        settings=ctx.settings,
        save_it=True,
        actor_user_id=None,
        trigger=TRIGGER,
        only_empty=True,
    )
    return result.audit_id


SCORE_RISK_SKIPPED = "the risk agent may no longer score this risk"
LOW_RISK_CHANGED = (
    "the risk changed while the window waited (no longer proposed, scored by a person, needs "
    "judgement, or a different suggestion), so it was not opened"
)


def _skipped(
    ctx: Context, step: str, subject_id: int, reason: str, *, subject: str = RISK, **details: Any
) -> int:
    """An orchestrator row saying an action found nothing to do, and why; nothing changes."""
    return _record(
        ctx.db,
        subject=(subject, str(subject_id)),
        status="skipped",
        signals={"step": step, "planned_reason": ctx.planned_reason},
        action_taken={"action": step, "applied": False, "reason": reason, **details},
    )


def _score_risk(ctx: Context, risk_id: int) -> int:
    """The `suggest-score` route's code: the risk agent's score, kept as a suggestion."""
    risk = ctx.db.get(Risk, risk_id)
    try:
        result = risk_agent.score(
            ctx.db,
            ctx.client,
            risk,
            policy=risk_policy(),
            settings=ctx.settings,
            save=True,
            actor_user_id=None,
            trigger=TRIGGER,
        )
    except risk_agent.ScoreRefused as exc:
        # A person scored it, marked it or decided on it while the window waited.
        return _skipped(ctx, "score_risk", risk_id, SCORE_RISK_SKIPPED, refusal=str(exc))
    return result.audit_id


def _open_low_risk(ctx: Context, risk_id: int) -> int | None:
    """Open a proposed Low risk after its window, through the open route's code.

    Only when the step is still the one planned (`low_risk_reason` unchanged): still proposed,
    no judgement needed, and the same score, or the same suggestion while the real score is
    empty. A suggestion becomes the real score here, as a person's would. The opening row is the
    risk register's, with `trigger = orchestrator`, the window it waited and the suggestion's
    audit row. Otherwise nothing changes and a skipped row says why."""
    db = ctx.db
    rules = risk_policy()
    risk = db.get(Risk, risk_id, with_for_update=True, populate_existing=True)
    now = low_risk_reason(risk, rules) if risk is not None else None
    if now is None or now[:REASON_MAX] != ctx.planned_reason:
        return _skipped(ctx, "open_low_risk", risk_id, LOW_RISK_CHANGED, now_reason=now)
    window = ctx.policy.steps["open_low_risk"].window_minutes if ctx.policy else None
    details: dict[str, Any] = {
        "by": ORCHESTRATOR,
        "step": "open_low_risk",
        "window_minutes": window,
    }
    before = snapshot(risk)
    if risk.probability is None:
        risk.probability = risk.suggested_probability
        risk.impact = risk.suggested_impact
        apply_score(risk, rules)
        details["suggested_audit_id"] = risk.suggested_audit_id
    decide_proposed(
        db,
        rules,
        risk,
        new_status="open",
        actor_user_id=None,
        details=details,
        trigger=TRIGGER,
        before=before,
    )
    return None


# The same functions the "Draft with the agent" buttons, the suggest-score route and the submit and
# answer routes use.
ACTIONS: dict[str, StepAction] = {
    "draft_value_case": StepAction(_draft_request),
    "draft_readiness": StepAction(_draft_request),
    "draft_plan": StepAction(_draft_plan),
    "propose_risks": StepAction(_propose_risks),
    "score_risk": StepAction(_score_risk),
    # No Claude call: it applies a suggestion already made, so it doesn't count against the caps.
    "open_low_risk": StepAction(_open_low_risk, uses_claude=False),
    "draft_proposal": StepAction(_draft_proposal),
    # No Claude call: rules only, so neither counts against the caps.
    "submit_proposal": StepAction(_submit_proposal, uses_claude=False),
    "client_answer": StepAction(_client_answer, uses_claude=False),
    "draft_status_report": StepAction(_draft_status_report),
}
assert not PERSON_ONLY & ACTIONS.keys(), "a person always takes these steps (R1)"


# --- Recording


def _record(
    db: Session,
    *,
    subject: tuple[str, str],
    agent: str = ORCHESTRATOR,
    version: int = 1,
    **fields: Any,
) -> int:
    """One audit row (the orchestrator's, unless `agent` says otherwise) at the next free attempt
    for its subject, moving on when a parallel writer (a person holding an action) took the
    number first."""
    subject_type, subject_id = subject
    attempt = 0
    for tried in range(1, 6):
        attempt = max(
            next_attempt(
                db,
                agent=agent,
                subject_type=subject_type,
                subject_id=subject_id,
                version=version,
            ),
            attempt + 1,
        )
        try:
            with db.begin_nested():
                fields.setdefault("trigger", TRIGGER)
                row = record_decision(
                    db,
                    agent=agent,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    version=version,
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
    outcome: str  # done, failed, pending, waiting, inbox, held, skipped, cancelled, closed, error
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


def _renew_lease(db: Session, holder: str, now: datetime) -> bool:
    """Lock the lease and, while this pass still holds it, extend it from `now`. False when
    another pass has taken it over. The caller commits, together with what it guards."""
    lease = db.get(OrchestratorLease, LEASE_NAME, with_for_update=True, populate_existing=True)
    if lease is None or lease.holder != holder:
        return False
    lease.expires_at = max(lease.expires_at, now + LEASE_TTL)
    return True


def _release_lease(db: Session, holder: str) -> None:
    db.rollback()
    lease = db.get(OrchestratorLease, LEASE_NAME, with_for_update=True, populate_existing=True)
    if lease is not None and lease.holder == holder:
        lease.expires_at = utcnow()
    db.commit()


def _last_shadow(db: Session, step: NextStep) -> dict[str, Any] | None:
    """The signals of the newest shadow row for this step and subject. Only shadow rows count:
    an enforce pass's `skipped` row (a step waiting for a cap) is not one."""
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
        if not isinstance(signals, dict) or signals.get("orchestrator_mode") != "shadow":
            continue
        if signals.get("step") == step.step:
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
        holder: str,
    ):
        self.db = db
        self.settings = settings
        self.client = client
        self.policy = policy
        self.now = now
        self.report = report
        self.holder = holder
        self.calls = 0
        # Set once another pass takes over the lease: this pass then stops.
        self.lost_lease = False

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

    def open_inbox(
        self, step: NextStep, *, reason: str | None = None, action_id=None, handoff: bool = False
    ) -> InboxItem:
        item = InboxItem(
            step=step.step,
            subject_type=step.subject_type,
            subject_id=str(step.subject_id),
            who=person_who(self.policy, step),
            reason=(reason or step.full_reason)[:REASON_MAX],
            status="open",
            handoff=handoff,
            action_id=action_id,
            policy_digest=self.policy.digest,
            open_key=step.key,
        )
        self.db.add(item)
        self.db.flush()
        return item

    def to_person(
        self,
        step: NextStep,
        reason: str,
        action_id: int | None = None,
        *,
        handoff: bool = False,
    ) -> None:
        """One open inbox item for the step. `handoff`: only because its agent can't act."""
        item = self.open_inbox(step, reason=reason, action_id=action_id, handoff=handoff)
        self.audit(
            step,
            "ok",
            {"action": "inbox", "inbox_item_id": item.id, "who": item.who, "reason": item.reason},
        )
        self.db.commit()
        self.outcome(step, "inbox", f"for the {item.who}: {item.reason}")

    def close_stale(self, live: dict[str, str]) -> None:
        """Close the open inbox items and pending actions whose step is no longer needed. `live`
        maps each live step's key to the role its inbox item waits on: an open item whose `who`
        differs (a risk escalated to another role) is closed too, and the step opens a new item
        for the new role in this pass."""
        db = self.db
        items = select(InboxItem).where(InboxItem.status == "open").order_by(InboxItem.id)
        actions = (
            select(OrchestratorAction)
            .where(OrchestratorAction.status == "pending")
            .order_by(OrchestratorAction.id)
        )
        for row in [*db.scalars(items), *db.scalars(actions)]:
            is_item = isinstance(row, InboxItem)
            if row.open_key not in live:
                why = NO_LONGER_NEEDED
            elif is_item and live[row.open_key] != row.who:
                why = WHO_CHANGED
            else:
                continue
            row.status = "done" if is_item else "cancelled"
            row.open_key = None
            row.closed_at = self.now
            row.closed_reason = why
            stale = NextStep(row.step, row.subject_type, int(row.subject_id), why, row.who, PERSON)
            what = "close_inbox" if is_item else "cancel_action"
            self.audit(stale, "ok", {"action": what, "id": row.id, "reason": why})
            db.commit()
            self.outcome(stale, "closed" if is_item else "cancelled", why)

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
        if step.step in RECURRING_STEPS:
            stmt = stmt.where(OrchestratorAction.reason == step.full_reason)
        return self.db.scalar(stmt.limit(1)) is not None

    def open_item(self, step: NextStep) -> InboxItem | None:
        stmt = select(InboxItem).where(InboxItem.open_key == step.key)
        return self.db.scalar(stmt.with_for_update())

    def cancel_handoff(self, step: NextStep, item: InboxItem) -> None:
        """The step's agent can act again, so the item opened only because it couldn't is
        cancelled and the step goes ahead as normal."""
        item.status = "cancelled"
        item.open_key = None
        item.closed_at = self.now
        item.closed_reason = AGENT_BACK
        self.audit(
            step, "ok", {"action": "cancel_inbox", "inbox_item_id": item.id, "reason": AGENT_BACK}
        )
        self.db.commit()
        self.outcome(step, "cancelled", f"inbox item {item.id}: {AGENT_BACK}")

    def relock(self, action_id: int) -> OrchestratorAction:
        """The action as it is now, locked. A refresh, since a person may have changed it (and
        its version) while the lock was released."""
        action = self.db.get(OrchestratorAction, action_id)
        self.db.refresh(action, with_for_update=True)
        return action

    def keep_lease(self) -> bool:
        """Renew the pass's lease (locked until the caller commits), or note that it is lost."""
        if _renew_lease(self.db, self.holder, max(self.now, utcnow())):
            return True
        self.lost_lease = True
        return False

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
        item = self.open_item(step)
        if item is not None:
            # Only an item opened because the agent couldn't act gives way, once it can. One
            # opened for a person step, a hold or used-up retries stays open.
            if not item.handoff or step.mode == PERSON:
                self.db.rollback()
                return
            self.cancel_handoff(step, item)
        action = self.pending(step)
        if step.mode == PERSON or step.step in PERSON_ONLY:
            if action is not None:
                # The policy or a switch moved the step to a person while it waited.
                self.give_up(step, action, step.full_reason, handoff=step.unavailable)
            else:
                self.to_person(step, step.full_reason, handoff=step.unavailable)
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
        self.carry_tries(step, action)
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

    def carry_tries(self, step: NextStep, action: OrchestratorAction) -> None:
        """`draft_proposal`'s first try creates the proposal, so its retries are the proposal's
        step (item 6.8): the tries the project's action used, and its next try time, carry over
        to the proposal's first action, so the agent gets the usual number of tries in all."""
        if step.step != "draft_proposal" or step.subject_type != PROPOSAL:
            return
        proposal = self.db.get(ProjectProposal, step.subject_id)
        if proposal is None:
            return
        stmt = (
            select(OrchestratorAction)
            .where(
                OrchestratorAction.step == step.step,
                OrchestratorAction.subject_type == PROJECT,
                OrchestratorAction.subject_id == str(proposal.project_id),
                OrchestratorAction.closed_reason == NO_LONGER_NEEDED,
            )
            .order_by(OrchestratorAction.id.desc())
            .limit(1)
        )
        earlier = self.db.scalar(stmt)
        if earlier is None or not earlier.attempts:
            return
        before = select(OrchestratorAction.id).where(
            OrchestratorAction.step == step.step,
            OrchestratorAction.subject_type == PROPOSAL,
            OrchestratorAction.subject_id == str(step.subject_id),
        )
        if self.db.scalar(before.limit(1)) is not None:
            return  # only the proposal's first action carries them
        action.attempts = earlier.attempts
        action.last_error = earlier.last_error
        action.next_try_at = earlier.next_try_at

    def run(self, step: NextStep, action: OrchestratorAction) -> None:
        step_action = ACTIONS.get(step.step)
        if step_action is None:  # the registry changed since the step was found
            self.give_up(step, action, NO_AGENT, handoff=True)
            return
        if step_action.uses_claude:
            wait = self.cap_reason()
            if wait is not None:
                # One row when the step first waits for this cap; later passes add none.
                if action.wait_reason != wait:
                    action.wait_reason = wait
                    self.audit(
                        step,
                        "skipped",
                        {"action": "wait", "action_id": action.id, "reason": wait},
                    )
                    self.db.commit()
                else:
                    self.db.rollback()
                self.outcome(step, "waiting", wait)
                return
        # Renew the lease before the call, so a long pass never runs out of it; if another pass
        # has taken it over, that pass does the step and this one stops.
        action_id = action.id
        planned_reason = action.reason
        if not self.keep_lease():
            self.db.rollback()
            self.audit(
                step,
                "skipped",
                {"action": "run", "action_id": action_id, "reason": LEASE_LOST_BEFORE},
            )
            self.db.commit()
            self.outcome(step, "skipped", LEASE_LOST_BEFORE)
            return
        if step_action.uses_claude:
            self.calls += 1
            self.report.agent_calls += 1
        # Release the action's row lock before the agent call (up to 120 seconds), so a person
        # can hold the action meanwhile without waiting for it. The row is re-read afterwards.
        self.db.commit()
        context = Context(
            self.db,
            self.settings,
            self.client,
            self.policy,
            planned_reason,
            step.subject_type,
            self.now,
        )
        try:
            agent_audit_id = step_action.run(context, step.subject_id)
        except risk_agent.AgentError as exc:
            # The agent's own error row is flushed and kept.
            self.failed(step, action_id, str(exc), exc.audit_id)
            return
        except Exception as exc:
            logger.exception("orchestrator step %s failed for %s", step.step, step.key)
            self.db.rollback()
            self.failed(step, action_id, f"{type(exc).__name__}: {exc}", None)
            return
        action = self.relock(action_id)
        if action.status != "pending":
            self.not_applied(step, action, agent_audit_id)
            return
        if not self.keep_lease():
            # Another pass now runs the steps; the action stays pending for it.
            self.not_applied(step, action, agent_audit_id, LEASE_LOST)
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

    def not_applied(
        self,
        step: NextStep,
        action: OrchestratorAction,
        agent_audit_id: int | None,
        reason: str = NOT_APPLIED,
    ) -> None:
        """A person held the action while its agent ran (or another pass took over the lease).
        The hold stands: the agent's changes are rolled back, and its run is recorded again as
        `skipped`, not applied, keeping its model, tokens and cost (so the daily cap still
        counts it)."""
        status, action_id = action.status, action.id
        kept = None
        run = self.db.get(AuditDecision, agent_audit_id) if agent_audit_id is not None else None
        if run is not None:
            kept = {
                name: getattr(run, name)
                for name in (
                    "agent",
                    "trigger",
                    "version",
                    "tier",
                    "signals",
                    "output",
                    "model",
                    "prompt_version",
                    "input_tokens",
                    "output_tokens",
                    "cost_usd",
                    "actor_user_id",
                )
            }
            kept["subject"] = (run.subject_type, run.subject_id)
            kept["action_taken"] = {
                **(run.action_taken or {}),
                "applied": False,
                "not_applied_reason": reason,
            }
        self.db.rollback()
        run_id = None
        if kept is not None:
            run_id = _record(self.db, status="skipped", **kept)
        self.audit(
            step,
            "skipped",
            {
                "action": "run",
                "action_id": action_id,
                "agent_audit_id": run_id,
                "applied": False,
                "action_status": status,
                "reason": reason,
            },
        )
        self.db.commit()
        self.outcome(step, "held" if reason == NOT_APPLIED else "skipped", reason)

    def failed(
        self, step: NextStep, action_id: int, error: str, agent_audit_id: int | None
    ) -> None:
        action = self.relock(action_id)
        if action.status != "pending":
            # A person held it during the call: the hold stands and nothing is retried.
            self.audit(
                step,
                "error",
                {
                    "action": "run",
                    "action_id": action.id,
                    "agent_audit_id": agent_audit_id,
                    "applied": False,
                    "action_status": action.status,
                },
                error=error[:MAX_ERROR],
            )
            self.db.commit()
            self.outcome(step, "held", f"the {action.agent} failed while the action was held")
            return
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
        reason = f"{step.full_reason}; the {action.agent} failed after {MAX_RETRIES} retries"
        self.give_up(step, action, reason)

    def give_up(
        self, step: NextStep, action: OrchestratorAction, reason: str, *, handoff: bool = False
    ) -> None:
        action.status = "cancelled"
        action.open_key = None
        action.closed_at = self.now
        action.closed_reason = reason[:REASON_MAX]
        self.db.flush()
        self.to_person(step, reason, action_id=action.id, handoff=handoff)


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
        steps = next_steps(db, rules, agent_mode=settings.agent_mode, now=now)
        report.ran = True
        report.steps = len(steps)
        if mode == "shadow":
            _shadow(db, steps, rules.digest, report)
            return report
        work = _Pass(db, settings, client, rules, now, report, holder)
        work.close_stale({step.key: person_who(rules, step) for step in steps})
        for step in steps:
            if work.lost_lease:
                break
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
        if args.command == "once":
            one_pass()
            return 0
        while True:
            try:
                one_pass()
            except Exception:
                # A bad pass (a policy error, the database away) waits for the next interval.
                logger.exception(
                    "orchestrator pass failed; trying again in %ss",
                    settings.orchestrator_interval_seconds,
                )
            time.sleep(settings.orchestrator_interval_seconds)
    except KeyboardInterrupt:
        print("orchestrator: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
