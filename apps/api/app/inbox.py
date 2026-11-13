"""The decision inbox (item 6.3): every decision waiting on the signed-in person, with the facts it
rests on and a one-line recommendation.

- **Where items come from.** The orchestrator (6.2) makes them: an open `InboxItem` (a `person`
  step, a held window action, or an agent step that failed after 3 retries), and a pending window
  `OrchestratorAction`, which happens at its due time unless the step's `who` holds it.
- **Who sees what.** An item waits on a role (`who`). Where its project names someone for that
  role (its sponsor or project manager), only that person sees it. A draft request has no project
  yet: it goes to the project manager who raised it, or to any project manager if someone else
  did. The ethics reviewer and admin are never named on a project, so they see their role's items.
- **A decided item leaves at once.** An item whose step the orchestrator's rules
  (`orchestrator.subject_steps`) no longer give for its subject is left out, so a person never
  waits for the next pass to see it go. The next pass closes it, as it always has.
- **An item follows its step's `who`.** An open item counts only for the role the live step now
  waits on: when that changed (a risk escalated to another role), the old role stops seeing it at
  once and the new role sees the step. The next pass closes the old item and opens one for the new
  role.
- **The agent's case.** The facts come from the subject as it is now (a gate's checks, a risk's
  score and band, a proposal's flags, a change's effect, a draft's gaps). The recommendation is
  written by code from those facts (`recommend`), one function per step; no model is involved.

- **Nobody decides a proposal they helped make** (item 6.10). An `approve_proposal` item never
  goes to someone who contributed to the latest copy. When the named sponsor did, it goes to any
  active sponsor who did not, saying so; when every approver did, it goes to the admin, who is
  asked to make a non-contributor the decider (`_proposal_route`). The admin never decides it.

Nothing here decides anything: each item is decided through the existing route for its step.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import acceptance, orchestrator
from app import policy as policies
from app import reports as report_rules
from app.authz import Action, allowed_actions
from app.autonomy import AutonomyPolicy
from app.gates import CHECKS_BY_SUBJECT, GATE_CHECKS
from app.models import (
    ChangeRequest,
    Client,
    InboxItem,
    OrchestratorAction,
    PlanDeliverable,
    Project,
    ProjectPlan,
    ProjectProposal,
    ProjectRequest,
    ReadinessCheck,
    Risk,
    StatusReport,
    User,
    ValueCase,
)
from app.models.proposal import TEXT_SECTIONS
from app.orchestrator import (
    CHANGE_REQUEST,
    DELIVERABLE,
    PROJECT,
    PROPOSAL,
    REQUEST,
    RISK,
    STATUS_REPORT,
)
from app.plans import missing
from app.policy import PolicyError, RiskPolicy, get_policies
from app.proposals import check, latest_version, plan_facts, unreviewed_sections, values_of
from app.roles import Role

ITEM = "item"
WINDOW = "window"
DEFAULT_RECOMMENDATION = "Review and decide."
NOT_RUN = "not run"
CHECKER_NOT_RUN = "Read it closely: the checker hasn't run"

# The gate each subject type's `pass_gate` step is decided at; a project in delivery is at the
# acceptance gate (item 7.6).
GATES = {REQUEST: "approval", PROJECT: "go_ahead"}


def gate_of(subject_type: str, subject: Any) -> str:
    """The gate a `pass_gate` step on the subject asks for."""
    if subject_type == PROJECT and subject.stage == "delivery":
        return orchestrator.ACCEPTANCE_GATE
    return GATES[subject_type]


@dataclass(frozen=True)
class Fact:
    """One fact a decision rests on. `met` is set for a check: true, false, or None for neither."""

    key: str
    label: str
    value: str
    met: bool | None = None


@dataclass
class Entry:
    """One inbox entry: an open item, or a pending window action."""

    kind: str  # ITEM or WINDOW
    id: int
    step: str
    subject_type: str
    subject_id: int
    who: str
    reason: str
    handoff: str | None
    created_at: datetime
    action_id: int | None = None
    due_at: datetime | None = None
    gate_id: str | None = None  # the gate a `pass_gate` step asks for
    subject_title: str = ""
    project_id: int | None = None
    project_name: str | None = None
    named_user_id: int | None = None
    named_user_name: str | None = None
    facts: list[Fact] = field(default_factory=list)
    recommendation: str = DEFAULT_RECOMMENDATION


# --- Scope: the subject, its project, and the person it names


@dataclass(frozen=True)
class Scope:
    title: str
    project: Project | None
    # The person the item goes to when the subject names one for the item's role.
    named_user_id: int | None


def _named(project: Project | None, who: str) -> int | None:
    if project is None:
        return None
    if who == Role.SPONSOR:
        return project.sponsor_user_id
    if who == Role.PROJECT_MANAGER:
        return project.pm_user_id
    return None


def _scope(db: Session, subject_type: str, subject_id: int, who: str) -> Scope | None:
    """The subject's title, project and named person; None when the subject is gone."""
    if subject_type == REQUEST:
        request = db.get(ProjectRequest, subject_id)
        if request is None:
            return None
        project = db.get(Project, request.project_id) if request.project_id else None
        if project is not None:
            return Scope(request.title, project, _named(project, who))
        # A draft request goes to the project manager who raised it.
        named = None
        if who == Role.PROJECT_MANAGER and request.requested_by is not None:
            raiser = db.get(User, request.requested_by)
            if raiser is not None and raiser.role == Role.PROJECT_MANAGER:
                named = raiser.id
        return Scope(request.title, None, named)
    if subject_type == PROJECT:
        project = db.get(Project, subject_id)
        return None if project is None else Scope(project.name, project, _named(project, who))
    if subject_type == DELIVERABLE:
        deliverable = db.get(PlanDeliverable, subject_id)
        if deliverable is None:
            return None
        project = orchestrator.deliverable_project(db, subject_id)
        return Scope(f"Deliverable '{deliverable.name}'", project, _named(project, who))
    model = {
        RISK: Risk,
        PROPOSAL: ProjectProposal,
        CHANGE_REQUEST: ChangeRequest,
        STATUS_REPORT: StatusReport,
    }.get(subject_type)
    row = db.get(model, subject_id) if model is not None else None
    if row is None:
        return None
    project = db.get(Project, row.project_id)
    if isinstance(row, ProjectProposal):
        title = f"Proposal for {project.name}" if project is not None else "Proposal"
    elif isinstance(row, StatusReport):
        title = f"Status report, {row.period_start.isoformat()} to {row.period_end.isoformat()}"
    else:
        title = row.title
    return Scope(title, project, _named(project, who))


# --- A proposal's decision never goes to someone who contributed to the copy (item 6.10)

APPROVE_PROPOSAL = "approve_proposal"
NO_FREE_APPROVER = (
    "every approver contributed to this copy, so none of them may approve or return it: make a "
    "sponsor who did not contribute the decider (reassign the project's sponsor, or give the "
    "approve role to someone who did not contribute). The admin never approves or returns it, "
    "and nothing is decided automatically"
)


@dataclass(frozen=True)
class Route:
    """Where an `approve_proposal` item goes instead of its usual person: the role, the people
    in it who may see it (None for anyone in the role), and why it went there."""

    who: str
    user_ids: frozenset[int] | None
    handoff: str


def _named_contributed(name: str) -> str:
    return (
        f"the named sponsor, {name}, contributed to this copy, so a sponsor who did not "
        "contribute decides it"
    )


def _proposal_route(db: Session, proposal_id: int, named_user_id: int | None) -> Route | None:
    """None when the person the item would go to may decide the latest copy. Otherwise any
    active approver who did not contribute to it, or, when every approver contributed, the
    admin, who is asked to make a non-contributor the decider. Nothing is decided here."""
    latest = latest_version(db, proposal_id)
    if latest is None:
        return None
    contributors = set(latest.contributors or ()) | {latest.submitted_by} - {None}
    if not contributors or (named_user_id is not None and named_user_id not in contributors):
        return None
    roles = [r for r in Role if Action.PROPOSAL_APPROVE in allowed_actions(r)]
    approvers = db.execute(
        select(User.id, User.display_name, User.role).where(
            User.is_active.is_(True), User.role.in_(roles)
        )
    ).all()
    free = [a for a in approvers if a.id not in contributors]
    if not free:
        return Route(Role.ADMIN, None, NO_FREE_APPROVER)
    if named_user_id is None:
        if not contributors & {a.id for a in approvers}:
            return None
        handoff = "a sponsor who contributed to this copy may not decide it"
    else:
        named = db.get(User, named_user_id)
        handoff = _named_contributed(named.display_name if named else f"User {named_user_id}")
    return Route(free[0].role, frozenset(a.id for a in free), handoff)


# --- The facts each step's decision rests on


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _gate_facts(db: Session, subject_type: str, subject: Any) -> list[Fact]:
    """The gate's automatic checks, as they stand now. The checks the code names for the gate when
    the gates file doesn't define it yet."""
    checks = CHECKS_BY_SUBJECT[subject_type]
    gate_id = gate_of(subject_type, subject)
    names: list[tuple[str, str]] = [(name, name.replace("_", " ")) for name in GATE_CHECKS[gate_id]]
    try:
        gate = get_policies().gates.gates.get(gate_id)
    except PolicyError:
        gate = None
    if gate is not None:
        names = [(c.check, c.text) for c in gate.criteria if c.check is not None]
    facts = []
    for name, label in names:
        result = checks[name](db, subject)
        facts.append(Fact(name, label, result.detail, result.met))
    return facts


def _risk_facts(risk: Risk) -> list[Fact]:
    facts = [
        Fact("title", "Risk", risk.title),
        Fact("score", "Score", "not scored" if risk.score is None else str(risk.score)),
        Fact("band", "Band", risk.band or "none"),
        Fact("probability", "Probability", str(risk.probability or "not scored")),
        Fact("impact", "Impact", str(risk.impact or "not scored")),
        Fact("response", "Response", risk.response),
        Fact("needs_judgement", "Needs a person's judgement", _yes(risk.needs_judgement)),
    ]
    if risk.escalated_to:
        facts.append(Fact("escalated_to", "Escalated to", risk.escalated_to.replace("_", " ")))
    suggestion = None
    # Never on a judgement risk: a person scores it, so no agent score is shown beside it (R9).
    if risk.status == "proposed" and risk.score is None and not risk.needs_judgement:
        try:
            suggestion = orchestrator.suggested_score(risk, get_policies().risk)
        except PolicyError:
            suggestion = None
    if suggestion is not None:
        score, band = suggestion
        owner = orchestrator.band_owner(get_policies().risk, band) or ""
        facts += [
            Fact(
                "suggested",
                "Suggested by the risk agent",
                f"probability {risk.suggested_probability}, impact {risk.suggested_impact}",
            ),
            Fact("suggested_score", "Suggested score", str(score)),
            Fact("suggested_band", "Suggested band", band),
            Fact("suggested_owner", "Opening it escalates to", owner.replace("_", " ")),
            Fact(
                "suggested_probability_reason",
                "Why that probability",
                risk.suggested_probability_reason or "",
            ),
            Fact("suggested_impact_reason", "Why that impact", risk.suggested_impact_reason or ""),
            Fact("suggested_model", "Model", risk.suggested_model or ""),
        ]
    return facts


def _proposal_facts(db: Session, proposal: ProjectProposal) -> list[Fact]:
    empty = [name for name in TEXT_SECTIONS if not (getattr(proposal, name) or "").strip()]
    facts = [
        Fact("status", "Status", proposal.status),
        Fact("price", "Price", f"{proposal.price:.2f}"),
        Fact("empty_sections", "Empty sections", ", ".join(empty) or "none"),
        Fact(
            "unreviewed_sections",
            "Agent drafts not reviewed",
            ", ".join(unreviewed_sections(proposal)) or "none",
        ),
    ]
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == proposal.project_id))
    if plan is not None:
        flags = check(values_of(proposal), plan_facts(db, plan))
        facts.append(Fact("flags", "Checker flags", str(len(flags)), not flags))
        facts.extend(
            Fact(f"flag_{n}", f"Flag: {flag.section}", flag.text, False)
            for n, flag in enumerate(flags, start=1)
        )
    else:
        # No plan to check against: say so, so nobody reads a missing check as a clean one.
        facts.append(Fact("checker", "Checker", NOT_RUN))
    return facts


def _change_facts(change: ChangeRequest) -> list[Fact]:
    effect = change.effect or {}
    pct = effect.get("cost_difference_pct")
    cost = str(effect.get("cost_difference", "0.00"))
    escalations = effect.get("escalations") or []
    return [
        Fact("title", "Change", change.title),
        Fact("decider", "Decided by", change.decider.replace("_", " ")),
        Fact("cost_difference", "Cost difference", cost if pct is None else f"{cost} ({pct}%)"),
        Fact("new_total", "New total", str(effect.get("new_total", ""))),
        Fact(
            "later_working_days",
            "Latest milestone moves later by (working days)",
            str(effect.get("max_later_working_days", 0)),
        ),
        Fact("scope_changes", "Scope changes", _yes(bool(effect.get("scope_changes")))),
        Fact(
            "escalations",
            "Beyond the limits",
            ", ".join(e.replace("_", " ") for e in escalations) or "none",
            not escalations,
        ),
    ]


def _report_facts(report: StatusReport) -> list[Fact]:
    unreviewed = report_rules.unreviewed_sections(report)
    empty = report_rules.empty_sections(report)
    return [
        Fact("period", "Period", f"{report.period_start} to {report.period_end}"),
        Fact("due_on", "Due on", report.due_on.isoformat()),
        Fact("overall", "Overall rating", report.ratings["overall"]["rating"]),
        Fact("empty_sections", "Empty sections", ", ".join(empty) or "none", not empty),
        Fact(
            "unreviewed_sections",
            "Agent drafts not reviewed",
            ", ".join(unreviewed) or "none",
            not unreviewed,
        ),
        Fact("flags", "Checker flags", str(len(report.flags or [])), not report.flags),
    ]


# Report flags (item 7.5): each overdue step and the reporting policy limit it uses.
OVERDUE_LIMITS = {"report_overdue_pm": "overdue_pm", "report_overdue_sponsor": "overdue_sponsor"}


def _overdue_facts(db: Session, project: Project, step: str) -> list[Fact]:
    try:
        limit = getattr(policies.get_reporting().flags, OVERDUE_LIMITS[step])
    except PolicyError:
        return []
    late = orchestrator.overdue_periods(db, project, limit)
    return [
        Fact("limit", "Limit", f"{limit} working days after the period's due day"),
        Fact(
            "overdue_periods",
            "Periods with no published report",
            ", ".join(f"{p.start} to {p.end}" for p in late) or "none",
            not late,
        ),
    ]


def _unread_facts(report: StatusReport) -> list[Fact]:
    published = report.published_at.date().isoformat() if report.published_at else "not published"
    return [
        Fact("period", "Period", f"{report.period_start} to {report.period_end}"),
        Fact("published_on", "Published on", published),
        Fact("overall", "Overall rating", report.ratings["overall"]["rating"]),
    ]


def _acceptance_facts(db: Session, deliverable: PlanDeliverable) -> list[Fact]:
    """The deliverable and the offer waiting for its answer (item 7.6)."""
    facts = [
        Fact("deliverable", "Deliverable", deliverable.name),
        Fact("status", "Status", deliverable.status.replace("_", " ")),
    ]
    offer = acceptance.waiting_offer(db, deliverable.id)
    if offer is not None:
        facts += [
            Fact("round", "Round", str(offer.round)),
            Fact("offered_to", "Offered to", offer.offered_to or ""),
            Fact(
                "offered_on", "Offered on", offer.offered_on.isoformat() if offer.offered_on else ""
            ),
            Fact("means", "By", offer.means or ""),
        ]
    project = orchestrator.deliverable_project(db, deliverable.id)
    client = db.get(Client, project.client_id) if project and project.client_id else None
    answerer = "the sponsor (no client)"
    if client is not None:
        answerer = "Simulated client" if client.simulated else f"the client, {client.name}"
    facts.append(Fact("answered_by", "Answered by", answerer))
    return facts


def _request_facts(db: Session, request: ProjectRequest) -> list[Fact]:
    has_value_case = db.scalar(select(ValueCase.id).where(ValueCase.request_id == request.id))
    has_readiness = db.scalar(
        select(ReadinessCheck.id).where(ReadinessCheck.request_id == request.id)
    )
    readiness = "not needed" if not request.uses_ai else ("started" if has_readiness else "none")
    return [
        Fact("value_case", "Value case", "started" if has_value_case else "none"),
        Fact("uses_ai", "Uses AI", _yes(request.uses_ai)),
        Fact("readiness", "Readiness check", readiness),
    ]


def _plan_facts(db: Session, project: Project) -> list[Fact]:
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    gaps = ["the project has no plan"] if plan is None else missing(db, plan)
    return [Fact("plan_gaps", "Missing from the plan", "; ".join(gaps) or "nothing", not gaps)]


def facts_for(db: Session, step: str, subject_type: str, subject_id: int) -> list[Fact]:
    """The facts a person needs for `step` on the subject, read as it is now."""
    if subject_type == REQUEST:
        request = db.get(ProjectRequest, subject_id)
        if request is None:
            return []
        if step == "pass_gate":
            return _gate_facts(db, REQUEST, request)
        return _request_facts(db, request)
    if subject_type == PROJECT:
        project = db.get(Project, subject_id)
        if project is None:
            return []
        if step == "pass_gate":
            return _gate_facts(db, PROJECT, project)
        if step == "draft_plan":
            return _plan_facts(db, project)
        if step == "draft_status_report":
            due = orchestrator.report_due(db, project)
            reason = due[0] if due is not None else "no report is due"
            return [Fact("report_due", "Report due", reason)]
        if step in OVERDUE_LIMITS:
            return _overdue_facts(db, project, step)
        open_risks = db.scalars(
            select(Risk.id).where(Risk.project_id == project.id, Risk.status != "rejected")
        ).all()
        return [Fact("risks", "Risks in the register", str(len(open_risks)))]
    if subject_type == RISK:
        risk = db.get(Risk, subject_id)
        return [] if risk is None else _risk_facts(risk)
    if subject_type == PROPOSAL:
        proposal = db.get(ProjectProposal, subject_id)
        return [] if proposal is None else _proposal_facts(db, proposal)
    if subject_type == CHANGE_REQUEST:
        change = db.get(ChangeRequest, subject_id)
        return [] if change is None else _change_facts(change)
    if subject_type == DELIVERABLE:
        deliverable = db.get(PlanDeliverable, subject_id)
        return [] if deliverable is None else _acceptance_facts(db, deliverable)
    if subject_type == STATUS_REPORT:
        report = db.get(StatusReport, subject_id)
        if report is None:
            return []
        if step == "report_unread":
            return _unread_facts(report)
        return _report_facts(report)
    return []


# --- The recommendation: pure functions from the facts to one line


def _value(facts: list[Fact], key: str, default: str = "") -> str:
    return next((f.value for f in facts if f.key == key), default)


def _gate(facts: list[Fact]) -> str:
    if not facts:
        return "No automatic checks apply: decide on the person criteria."
    unmet = [f.label for f in facts if f.met is False]
    if unmet:
        return f"Return it: {len(unmet)} of {len(facts)} checks are not met ({'; '.join(unmet)})."
    return f"Pass it if the person criteria hold: all {len(facts)} checks are met."


def _accept_risk(facts: list[Fact]) -> str:
    score, band = _value(facts, "score"), _value(facts, "band")
    response = _value(facts, "response")
    return (
        f"Accept it only if the project can carry a {band} risk scored {score}; "
        f"otherwise ask for a stronger response than '{response}'."
    )


def _open_low_risk(facts: list[Fact]) -> str:
    score, band = _value(facts, "score"), _value(facts, "band")
    if _value(facts, "suggested_band"):
        score, band = _value(facts, "suggested_score"), _value(facts, "suggested_band")
        return (
            f"The risk agent suggested {band} (score {score}). Use the agent's score (Use this "
            f"score, then Save) or set your own, then open the risk. A {band} risk stays with a "
            "project manager."
        )
    return f"Open it in the risk register: scored {score}, {band}, which a project manager owns."


def _score_risk(facts: list[Fact]) -> str:
    if _value(facts, "needs_judgement") == "yes":
        return "Score it yourself: it needs a person's judgement, so no agent may score it."
    band = _value(facts, "suggested_band")
    if band:
        score, owner = _value(facts, "suggested_score"), _value(facts, "suggested_owner")
        return (
            f"The risk agent suggested {band} (score {score}). Use the agent's score (Use this "
            "score, then Save) or set your own, then open the risk. Opening it escalates it to "
            f"the {owner}."
        )
    return "Score its probability and impact so it gets a band and an owner."


def _flags(facts: list[Fact]) -> int | None:
    """The checker's open flags; None when it hasn't run (no flags fact, or "checker: not run")."""
    value = _value(facts, "flags")
    if not value or _value(facts, "checker") == NOT_RUN:
        return None
    return int(value)


def _approve_proposal(facts: list[Fact]) -> str:
    flags = _flags(facts)
    if flags is None:
        return f"{CHECKER_NOT_RUN}: check the price ({_value(facts, 'price')}) and terms yourself."
    if flags:
        return f"Return it: the checker has {flags} flag(s) open."
    return f"Approve it if the price ({_value(facts, 'price')}) and terms are right: no flags."


def _send_proposal(facts: list[Fact]) -> str:
    flags = _flags(facts)
    if flags is None:
        return f"{CHECKER_NOT_RUN}, so check it yourself before you send it and mark it sent."
    if flags:
        return f"Clear the {flags} open flag(s) first: it can't be marked sent while they're open."
    return "Send it to the client and mark it sent: the checker has no flags open."


def _submit_proposal(facts: list[Fact]) -> str:
    flags = _flags(facts)
    if flags is None:
        return f"{CHECKER_NOT_RUN}, so check it yourself before you submit it for approval."
    if flags:
        return f"Clear the {flags} checker flag(s), then submit it for approval."
    return "Submit it for approval: every section is written and reviewed, with no flags open."


def _draft_proposal(facts: list[Fact]) -> str:
    empty = _value(facts, "empty_sections")
    if not empty:
        # Item 6.8: a client project whose plan is complete and which has no proposal yet.
        return "Create the proposal from the plan, then write its sections."
    return f"Write the empty sections: {empty}."


def _client_answer(_facts: list[Fact]) -> str:
    return "Record the client's answer when it arrives."


def _client_acceptance(facts: list[Fact]) -> str:
    if _value(facts, "answered_by").startswith("the sponsor"):
        return (
            "Accept it if it meets what was agreed; otherwise record it as not accepted with what "
            "is missing."
        )
    return "Record the client's answer on the deliverable when it arrives."


def _decide_change(facts: list[Fact]) -> str:
    cost = _value(facts, "cost_difference")
    days = _value(facts, "later_working_days", "0")
    beyond = _value(facts, "escalations", "none")
    if beyond != "none":
        return f"Weigh it with care: it goes beyond the limits ({beyond}); cost {cost}."
    return f"Approve it if the reason holds: within the limits (cost {cost}, {days} days later)."


def _draft_value_case(facts: list[Fact]) -> str:
    if _value(facts, "readiness") == "none":
        return "Write the value case and, as the request uses AI, the readiness check."
    return "Write the value case: the request has none."


def _draft_readiness(_facts: list[Fact]) -> str:
    return "Complete the readiness check: the request uses AI and has none."


def _draft_plan(facts: list[Fact]) -> str:
    return f"Complete the plan: {_value(facts, 'plan_gaps')}."


def _propose_risks(facts: list[Fact]) -> str:
    return (
        f"Look for the project's risks and add them to the risk register "
        f"(it holds {_value(facts, 'risks', '0')})."
    )


def _draft_status_report(_facts: list[Fact]) -> str:
    return "Create the period's report and write its text."


def _publish_status_report(facts: list[Fact]) -> str:
    unreviewed = _value(facts, "unreviewed_sections", "none")
    empty = _value(facts, "empty_sections", "none")
    flags = _flags(facts)
    if unreviewed != "none":
        return f"Confirm or edit the agent's sections ({unreviewed}), then publish it."
    if empty != "none":
        return f"Write the empty sections ({empty}), then publish it."
    if flags:
        return f"Clear the {flags} checker flag(s), then publish it."
    return "Publish it: every section is written and reviewed, with no flags open."


def _report_overdue_pm(_facts: list[Fact]) -> str:
    return "Publish the report for each period listed; the item leaves once each one is published."


def _report_overdue_sponsor(_facts: list[Fact]) -> str:
    return (
        "Ask the project manager to publish the report for each period listed; the item leaves "
        "once each one is published."
    )


def _report_unread(_facts: list[Fact]) -> str:
    return "Open the report's one-page view; the item leaves once you have opened it."


RECOMMENDERS: dict[str, Callable[[list[Fact]], str]] = {
    "pass_gate": _gate,
    "accept_risk": _accept_risk,
    "open_low_risk": _open_low_risk,
    "score_risk": _score_risk,
    "approve_proposal": _approve_proposal,
    "send_proposal": _send_proposal,
    "submit_proposal": _submit_proposal,
    "draft_proposal": _draft_proposal,
    "client_answer": _client_answer,
    "client_acceptance": _client_acceptance,
    "decide_change_pm": _decide_change,
    "decide_change_sponsor": _decide_change,
    "draft_value_case": _draft_value_case,
    "draft_readiness": _draft_readiness,
    "draft_plan": _draft_plan,
    "propose_risks": _propose_risks,
    "draft_status_report": _draft_status_report,
    "publish_status_report": _publish_status_report,
    "report_overdue_pm": _report_overdue_pm,
    "report_overdue_sponsor": _report_overdue_sponsor,
    "report_unread": _report_unread,
}


def recommend(step: str, facts: list[Fact]) -> str:
    """The one-line recommendation for `step`, from its facts alone."""
    recommender = RECOMMENDERS.get(step)
    return recommender(facts) if recommender is not None else DEFAULT_RECOMMENDATION


# --- The inbox


def _handoff(
    db: Session, item: InboxItem, live: orchestrator.NextStep, names: dict[int, str]
) -> str | None:
    """Why the item went to a person, in the words its audit row uses; None for a person step."""
    if item.action_id is not None:
        action = db.get(OrchestratorAction, item.action_id)
        if action is not None and action.status == "held":
            who = names.get(action.held_by or 0, "a person")
            return f"held by {who}: {action.hold_reason}"
    prefix = f"{live.reason}; "
    if item.reason.startswith(prefix):
        return item.reason[len(prefix) :]
    if item.handoff or item.action_id is not None:
        return item.reason.rsplit("; ", 1)[-1]
    return None


def _user_names(db: Session, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = db.execute(select(User.id, User.display_name).where(User.id.in_(ids)))
    return {row.id: row.display_name for row in rows}


def entries(
    db: Session,
    user: User,
    policy: AutonomyPolicy,
    *,
    agent_mode: str,
    risk_rules: RiskPolicy | None = None,
) -> list[Entry]:
    """The signed-in person's inbox, oldest first, without facts (see `with_case`). Raises
    `PolicyError` when the autonomy or risk policy can't be loaded."""
    rules = risk_rules or orchestrator.risk_policy()
    # The role's open items, plus every open risk item: only a risk's step can move to another
    # role (re-escalated), and it goes to that role at once, before the next pass reopens it.
    items = db.scalars(
        select(InboxItem)
        .where(
            InboxItem.status == "open",
            # And every open proposal decision: it goes to someone else when the person it
            # would go to contributed to the copy (`_proposal_route`).
            or_(
                InboxItem.who == user.role,
                InboxItem.subject_type == RISK,
                InboxItem.step == APPROVE_PROPOSAL,
            ),
        )
        .order_by(InboxItem.created_at, InboxItem.id)
    ).all()
    windows = db.scalars(
        select(OrchestratorAction)
        .where(
            OrchestratorAction.status == "pending",
            OrchestratorAction.mode == orchestrator.WINDOW,
            OrchestratorAction.who == user.role,
        )
        .order_by(OrchestratorAction.created_at, OrchestratorAction.id)
    ).all()
    held_by = {
        a.held_by
        for a in db.scalars(
            select(OrchestratorAction).where(
                OrchestratorAction.id.in_({i.action_id for i in items if i.action_id})
            )
        )
        if a.held_by is not None
    }
    # Each subject's steps and scope are worked out once per request, however many rows share it.
    steps_cache: dict[tuple[str, int], dict[str, orchestrator.NextStep]] = {}
    scope_cache: dict[tuple[str, int, str], Scope | None] = {}
    route_cache: dict[tuple[int, str], Route | None] = {}

    def live(step: str, subject_type: str, subject_id: int) -> orchestrator.NextStep | None:
        key = (subject_type, subject_id)
        if key not in steps_cache:
            found = orchestrator.subject_steps(
                db, policy, subject_type, subject_id, agent_mode=agent_mode, risk_rules=rules
            )
            steps_cache[key] = {s.step: s for s in found}
        return steps_cache[key].get(step)

    found: list[Entry] = []
    names = _user_names(db, held_by)
    for row in [*items, *windows]:
        subject_id = int(row.subject_id)
        step = live(row.step, row.subject_type, subject_id)
        if step is None:
            continue  # decided since the last pass: it leaves at once
        is_item = isinstance(row, InboxItem)
        # The step's `who` now, which an open item follows: when it has changed, the old role
        # stops seeing the item and the new role sees the step.
        who = orchestrator.person_who(policy, step) if is_item else row.who
        route = None
        if row.step == APPROVE_PROPOSAL and row.subject_type == PROPOSAL:
            route_key = (subject_id, who)
            if route_key not in route_cache:
                scope = _scope(db, row.subject_type, subject_id, who)
                named = scope.named_user_id if scope is not None else None
                route_cache[route_key] = _proposal_route(db, subject_id, named)
            route = route_cache[route_key]
        if (route.who if route else who) != user.role:
            continue
        moved = who != row.who
        scope_key = (row.subject_type, subject_id, who)
        if scope_key not in scope_cache:
            scope_cache[scope_key] = _scope(db, row.subject_type, subject_id, who)
        scope = scope_cache[scope_key]
        if scope is None:
            continue
        if route is not None:
            if route.user_ids is not None and user.id not in route.user_ids:
                continue
        elif scope.named_user_id is not None and scope.named_user_id != user.id:
            continue
        found.append(
            Entry(
                kind=ITEM if is_item else WINDOW,
                id=row.id,
                step=row.step,
                subject_type=row.subject_type,
                subject_id=subject_id,
                who=route.who if route else who,
                reason=step.reason,
                handoff=route.handoff
                if route
                else (None if moved or not is_item else _handoff(db, row, step, names)),
                created_at=row.created_at,
                action_id=row.action_id if is_item else row.id,
                due_at=None if is_item else row.due_at,
                gate_id=step.gate,
                subject_title=scope.title,
                project_id=scope.project.id if scope.project is not None else None,
                project_name=scope.project.name if scope.project is not None else None,
                named_user_id=None if route else scope.named_user_id,
            )
        )
    found.sort(key=lambda e: (e.created_at, e.kind != ITEM, e.id))
    return found


def with_case(db: Session, page: list[Entry]) -> list[Entry]:
    """Add the facts, the recommendation and the named person's name to each entry."""
    names = _user_names(db, {e.named_user_id for e in page if e.named_user_id is not None})
    for entry in page:
        entry.facts = facts_for(db, entry.step, entry.subject_type, entry.subject_id)
        entry.recommendation = recommend(entry.step, entry.facts)
        if entry.named_user_id is not None:
            entry.named_user_name = names.get(entry.named_user_id)
    return page
