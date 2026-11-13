"""The portfolio board (item 6.4): every request and project as a card in a column by stage, with
what it is waiting on, computed fresh on each read from the existing tables. Nothing is stored.

- **Columns**, left to right: Requests (draft and submitted requests), Approval gate (a submitted
  request with every approval check met), Planning, Go-ahead gate (a planning project with every
  go-ahead check met), Delivery, Closure, Closed. An approved request is shown as the project its
  approval created. A project still at `intake` sits with the requests.
- **Stopped items** (declined or withdrawn requests, cancelled projects) are hidden unless asked
  for; then they sit in their stage's column, marked stopped, and count in no total.
- **What a card waits on** is the orchestrator's next steps (`orchestrator.next_steps`) for the
  card's request, or for its project and the project's risks, proposals and change requests: the
  same function the inbox reads, so the two never disagree. An agent step is "Agent drafting the
  plan"; a window step "Agent will submit the proposal at 14:20 UTC unless held"; a person step
  "Waiting on the sponsor: go-ahead gate". An agent step a person held, or that failed after its
  retries, has an open inbox item and waits on a person. A step the simulated client takes is
  labelled simulated, and so is every card whose client is simulated (item 6.8: its contact email
  is in the demo domain), so the board always says when an answer came from the simulation.
- **The latest report** (item 7.5): a project card shows its latest published status report's
  overall rating and how many days ago it was published.
- **Money** is computed here: a plan total is its estimate lines plus contingency
  (`plans.compute_total`); the summary adds them up. Nothing is taken from the client.

The board decides nothing and changes nothing (R1): each card links to the page where a person
acts.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import delivery, orchestrator
from app.autonomy import PERSON, WINDOW, AutonomyPolicy
from app.gates import REQUEST_CHECKS
from app.models import (
    ChangeRequest,
    Client,
    InboxItem,
    OrchestratorAction,
    PlanDeliverable,
    PlanEstimateLine,
    Project,
    ProjectBrief,
    ProjectPlan,
    ProjectProposal,
    ProjectRequest,
    Risk,
    StatusReport,
    User,
)
from app.models.portfolio import CANCELLED
from app.models.report import PUBLISHED
from app.orchestrator import (
    CHANGE_REQUEST,
    DELIVERABLE,
    PROJECT,
    PROPOSAL,
    REQUEST,
    RISK,
    STATUS_REPORT,
    NextStep,
)
from app.plans import compute_total
from app.policy import PolicyError, RiskPolicy, get_policies
from app.roles import Role

REQUESTS = "requests"
APPROVAL_GATE = "approval_gate"
PLANNING = "planning"
GO_AHEAD_GATE = "go_ahead_gate"
DELIVERY = "delivery"
CLOSURE = "closure"
CLOSED = "closed"
COLUMNS: dict[str, str] = {
    REQUESTS: "Requests",
    APPROVAL_GATE: "Approval gate",
    PLANNING: "Planning",
    GO_AHEAD_GATE: "Go-ahead gate",
    DELIVERY: "Delivery",
    CLOSURE: "Closure",
    CLOSED: "Closed",
}
STAGE_COLUMNS = {
    "intake": REQUESTS,
    "planning": PLANNING,
    "delivery": DELIVERY,
    "closure": CLOSURE,
    "closed": CLOSED,
}
# Request statuses a card shows for: an approved request is shown as its project.
LIVE_REQUESTS = ("draft", "submitted")
STOPPED_REQUESTS = ("declined", "withdrawn")
# The proposal statuses a card shows; a withdrawn proposal is passed over.
PROPOSAL_SHOWN = ("draft", "submitted", "approved", "sent", "accepted", "declined")

AGENT_HOLDER = "agent"
PERSON_HOLDER = "person"
SIMULATED_AGENT = "simulated_client"

# Each step in words: what an agent is doing, what it will do after a window, and what a person
# is asked for. `pass_gate` is named by its gate (`GATE_NAMES`).
STEP_WORDS: dict[str, tuple[str, str, str]] = {
    "draft_value_case": ("drafting the value case", "draft the value case", "value case"),
    "draft_readiness": (
        "drafting the readiness check",
        "draft the readiness check",
        "readiness check",
    ),
    "draft_plan": ("drafting the plan", "draft the plan", "plan"),
    "propose_risks": ("looking for risks", "look for risks", "risks to add"),
    "score_risk": ("scoring a risk", "score a risk", "risk to score"),
    "open_low_risk": ("opening a risk", "open a risk", "risk to open"),
    "draft_proposal": ("drafting the proposal", "draft the proposal", "proposal to write"),
    "submit_proposal": ("submitting the proposal", "submit the proposal", "proposal to submit"),
    "approve_proposal": ("approving the proposal", "approve the proposal", "proposal approval"),
    "send_proposal": ("sending the proposal", "send the proposal", "proposal to send"),
    "client_answer": ("answering the proposal", "answer the proposal", "client's answer"),
    "client_acceptance": (
        "answering for a deliverable",
        "answer for a deliverable",
        "deliverable acceptance",
    ),
    "accept_risk": ("deciding a risk", "decide a risk", "risk decision"),
    "pass_gate": ("deciding the gate", "decide the gate", "gate"),
    "decide_change_pm": ("deciding a change", "decide a change", "change request decision"),
    "decide_change_sponsor": ("deciding a change", "decide a change", "change request decision"),
    "draft_status_report": (
        "drafting the status report",
        "draft the status report",
        "status report to write",
    ),
    "publish_status_report": (
        "publishing the status report",
        "publish the status report",
        "status report to publish",
    ),
    "report_overdue_pm": ("", "", "status report overdue"),
    "report_overdue_sponsor": ("", "", "status report overdue"),
    "report_unread": ("", "", "status report to read"),
}
GATE_NAMES = {REQUEST: "approval gate", PROJECT: "go-ahead gate"}
# By the gate a `pass_gate` step names (item 7.6 adds the acceptance gate).
GATE_NAMES_BY_ID = {
    "approval": "approval gate",
    "go_ahead": "go-ahead gate",
    "acceptance": "acceptance gate",
}

WAITING_PERSON = "person"
WAITING_AGENT = "agent"


@dataclass(frozen=True)
class Waiting:
    """One thing a card is waiting on: an orchestrator next step, in words."""

    step: str
    subject_type: str
    subject_id: int
    holder: str  # AGENT_HOLDER or PERSON_HOLDER
    who: str  # the role for a person, the agent's name for an agent
    text: str
    due_at: datetime | None = None
    simulated: bool = False


@dataclass
class Card:
    kind: str  # REQUEST or PROJECT
    id: int
    column: str
    name: str
    client_id: int | None
    client_name: str | None
    project_manager: str | None
    health: str | None = None  # the project's status
    proposal_status: str | None = None
    top_risk_band: str | None = None
    open_risks: int = 0
    open_medium_high_risks: int = 0
    plan_total: Decimal | None = None
    top_budget: Decimal | None = None
    stopped: bool = False
    client_simulated: bool = False
    # The latest published status report (item 7.5): its overall rating, when it was published
    # and how many days old it is.
    report_rating: str | None = None
    report_published_at: datetime | None = None
    report_age_days: int | None = None
    waiting: list[Waiting] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.kind}-{self.id}"

    @property
    def held_by(self) -> str | None:
        """Who has the card: a person when any step waits on one, else an agent, else nobody."""
        holders = {w.holder for w in self.waiting}
        if PERSON_HOLDER in holders:
            return PERSON_HOLDER
        return AGENT_HOLDER if AGENT_HOLDER in holders else None

    @property
    def simulated(self) -> bool:
        return self.client_simulated or any(w.simulated for w in self.waiting)


@dataclass(frozen=True)
class Filters:
    client_id: int | None = None
    project_manager: str | None = None
    waiting: str | None = None  # WAITING_PERSON or WAITING_AGENT
    show_stopped: bool = False


@dataclass
class Summary:
    counts: dict[str, int]
    planning_value: Decimal
    delivery_value: Decimal
    open_medium_high_risks: int
    decisions_waiting: int
    stopped: int


@dataclass
class Board:
    columns: dict[str, list[Card]]
    summary: Summary
    clients: list[tuple[int, str]]
    project_managers: list[str]


# --- Words for a step


def _role(role: str) -> str:
    return role.replace("_", " ")


def _person_label(step: NextStep) -> str:
    if step.step == "pass_gate":
        return GATE_NAMES_BY_ID.get(step.gate or "", GATE_NAMES[step.subject_type])
    return STEP_WORDS[step.step][2]


def waiting_for(
    policy: AutonomyPolicy,
    step: NextStep,
    item: InboxItem | None,
    action: OrchestratorAction | None,
) -> Waiting:
    """`step` in words. `item` is its open inbox item and `action` its pending action, if any."""
    base = {"step": step.step, "subject_type": step.subject_type, "subject_id": step.subject_id}
    # A person step; or an agent step a person held, or that failed after its retries (its item
    # stays open). A hand-off item (the agent couldn't act) gives way once the agent can.
    if step.mode == PERSON or (item is not None and not item.handoff):
        who = orchestrator.person_who(policy, step)
        text = f"Waiting on the {_role(who)}: {_person_label(step)}"
        return Waiting(**base, holder=PERSON_HOLDER, who=who, text=text)
    agent = step.waits_on
    simulated = agent == SIMULATED_AGENT
    actor = "Simulated client" if simulated else "Agent"
    doing, will_do, _ = STEP_WORDS[step.step]
    if step.mode == WINDOW:
        due = action.due_at if action is not None else None
        when = f" at {due:%H:%M} UTC" if due is not None else ""
        text = f"{actor} will {will_do}{when} unless held"
        return Waiting(
            **base, holder=AGENT_HOLDER, who=agent, text=text, due_at=due, simulated=simulated
        )
    text = f"{actor} {doing}"
    return Waiting(**base, holder=AGENT_HOLDER, who=agent, text=text, simulated=simulated)


# --- The cards


def _approval_checks_met(db: Session, request: ProjectRequest) -> bool:
    """Every automatic check the approval gate names, or every request check while the gates
    file isn't applied (as the inbox does)."""
    names = list(REQUEST_CHECKS)
    try:
        gate = get_policies().gates.gates.get("approval")
    except PolicyError:
        gate = None
    if gate is not None:
        names = [c.check for c in gate.criteria if c.check in REQUEST_CHECKS]
    return all(REQUEST_CHECKS[name](db, request).met for name in names)


def _band_ranks(risk_rules: RiskPolicy) -> dict[str, int]:
    return {band.name: rank for rank, band in enumerate(risk_rules.priority_bands)}


def _risk_fields(db: Session, card: Card, ranks: dict[str, int]) -> None:
    bands = db.scalars(select(Risk.band).where(Risk.project_id == card.id, Risk.status == "open"))
    ranked = [(ranks.get(band, -1), band) for band in bands]
    card.open_risks = len(ranked)
    # Medium and High: every band above the lowest one the risk policy sets.
    card.open_medium_high_risks = sum(1 for rank, _ in ranked if rank > 0)
    scored = [(rank, band) for rank, band in ranked if band is not None]
    card.top_risk_band = max(scored)[1] if scored else None


def _plan_total(db: Session, project_id: int) -> Decimal | None:
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project_id))
    if plan is None:
        return None
    amounts = db.scalars(select(PlanEstimateLine.amount).where(PlanEstimateLine.plan_id == plan.id))
    return compute_total(amounts, plan.contingency_pct)


def _project_card(
    db: Session, project: Project, clients: dict[int, str], ranks: dict[str, int], gate: bool
) -> Card:
    column = STAGE_COLUMNS.get(project.stage, REQUESTS)
    if column == PLANNING and gate:
        column = GO_AHEAD_GATE
    card = Card(
        kind=PROJECT,
        id=project.id,
        column=column,
        name=project.name,
        client_id=project.client_id,
        client_name=clients.get(project.client_id) if project.client_id else None,
        project_manager=project.pm,
        health=project.status,
        stopped=project.status == CANCELLED,
    )
    card.proposal_status = db.scalar(
        select(ProjectProposal.status)
        .where(ProjectProposal.project_id == project.id, ProjectProposal.status.in_(PROPOSAL_SHOWN))
        .order_by(ProjectProposal.id.desc())
        .limit(1)
    )
    _risk_fields(db, card, ranks)
    card.plan_total = _plan_total(db, project.id)
    card.top_budget = db.scalar(
        select(ProjectBrief.budget_high).where(ProjectBrief.project_id == project.id)
    )
    _report_fields(db, card)
    return card


def _report_fields(db: Session, card: Card) -> None:
    """The latest published report: the newest period's, the newest of its reports."""
    report = db.scalar(
        select(StatusReport)
        .where(StatusReport.project_id == card.id, StatusReport.status == PUBLISHED)
        .order_by(StatusReport.period_end.desc(), StatusReport.id.desc())
        .limit(1)
    )
    if report is None or report.published_at is None:
        return
    card.report_rating = report.ratings["overall"]["rating"]
    card.report_published_at = report.published_at
    card.report_age_days = (delivery.today() - report.published_at.date()).days


def _request_card(
    db: Session, request: ProjectRequest, clients: dict[int, str], raisers: dict[int, str]
) -> Card:
    column = REQUESTS
    if request.status == "submitted" and _approval_checks_met(db, request):
        column = APPROVAL_GATE
    return Card(
        kind=REQUEST,
        id=request.id,
        column=column,
        name=request.title,
        client_id=request.client_id,
        client_name=clients.get(request.client_id) if request.client_id else None,
        # A request has no project yet: the project manager who raised it will run it.
        project_manager=raisers.get(request.requested_by),
        top_budget=request.budget_high,
        stopped=request.status in STOPPED_REQUESTS,
    )


def _owner_projects(db: Session, steps: list[NextStep]) -> dict[tuple[str, int], int]:
    """The project each risk, proposal, status report, change request and deliverable step
    belongs to."""
    owners: dict[tuple[str, int], int] = {}
    for subject_type, model in (
        (RISK, Risk),
        (PROPOSAL, ProjectProposal),
        (STATUS_REPORT, StatusReport),
    ):
        ids = {s.subject_id for s in steps if s.subject_type == subject_type}
        if ids:
            rows = db.execute(select(model.id, model.project_id).where(model.id.in_(ids)))
            owners.update({(subject_type, row.id): row.project_id for row in rows})
    ids = {s.subject_id for s in steps if s.subject_type == CHANGE_REQUEST}
    if ids:
        rows = db.execute(
            select(ChangeRequest.id, ChangeRequest.project_id).where(ChangeRequest.id.in_(ids))
        )
        owners.update({(CHANGE_REQUEST, row.id): row.project_id for row in rows})
    ids = {s.subject_id for s in steps if s.subject_type == DELIVERABLE}
    if ids:
        rows = db.execute(
            select(PlanDeliverable.id, ProjectPlan.project_id)
            .join(ProjectPlan, ProjectPlan.id == PlanDeliverable.plan_id)
            .where(PlanDeliverable.id.in_(ids))
        )
        owners.update({(DELIVERABLE, row.id): row.project_id for row in rows})
    return owners


def _card_key(step: NextStep, owners: dict[tuple[str, int], int]) -> str | None:
    if step.subject_type in (REQUEST, PROJECT):
        return f"{step.subject_type}-{step.subject_id}"
    project_id = owners.get((step.subject_type, step.subject_id))
    return f"{PROJECT}-{project_id}" if project_id is not None else None


def cards(
    db: Session,
    policy: AutonomyPolicy,
    *,
    agent_mode: str,
    risk_rules: RiskPolicy,
    show_stopped: bool = False,
) -> list[Card]:
    """Every card, requests then projects, each by id. Raises `PolicyError` when the autonomy or
    risk policy can't be loaded."""
    steps = orchestrator.next_steps(db, policy, agent_mode=agent_mode, risk_rules=risk_rules)
    items = {
        i.open_key: i
        for i in db.scalars(select(InboxItem).where(InboxItem.status == "open"))
        if i.open_key
    }
    actions = {
        a.open_key: a
        for a in db.scalars(
            select(OrchestratorAction).where(OrchestratorAction.status == "pending")
        )
        if a.open_key
    }
    owners = _owner_projects(db, steps)
    waiting: dict[str, list[Waiting]] = {}
    for step in steps:
        key = _card_key(step, owners)
        if key is not None:
            entry = waiting_for(policy, step, items.get(step.key), actions.get(step.key))
            waiting.setdefault(key, []).append(entry)

    clients = {row.id: row.name for row in db.execute(select(Client.id, Client.name))}
    ranks = _band_ranks(risk_rules)
    request_statuses = LIVE_REQUESTS + (STOPPED_REQUESTS if show_stopped else ())
    requests = list(
        db.scalars(
            select(ProjectRequest)
            .where(ProjectRequest.status.in_(request_statuses))
            .order_by(ProjectRequest.id)
        )
    )
    raiser_ids = {r.requested_by for r in requests}
    raisers = {
        row.id: row.display_name
        for row in db.execute(
            select(User.id, User.display_name).where(
                User.id.in_(raiser_ids), User.role == Role.PROJECT_MANAGER
            )
        )
    }
    projects = select(Project).order_by(Project.id)
    if not show_stopped:
        projects = projects.where(Project.status != CANCELLED)
    found = [_request_card(db, r, clients, raisers) for r in requests]
    for project in db.scalars(projects):
        # The go-ahead column is the orchestrator's own rule: a planning project whose next steps
        # include its gate has every go-ahead check met. (A project in delivery at the acceptance
        # gate stays in the Delivery column.)
        at_gate = any(
            w.step == "pass_gate" and w.subject_type == PROJECT
            for w in waiting.get(f"{PROJECT}-{project.id}", [])
        )
        found.append(_project_card(db, project, clients, ranks, at_gate))
    simulated = set(db.scalars(select(Client.id).where(Client.simulated.is_(True))))
    for card in found:
        card.waiting = waiting.get(card.key, [])
        card.client_simulated = card.client_id in simulated
    return found


# --- The board


def _matches(card: Card, filters: Filters) -> bool:
    if filters.client_id is not None and card.client_id != filters.client_id:
        return False
    if filters.project_manager is not None and card.project_manager != filters.project_manager:
        return False
    # The waiting filter follows `held_by`: a card waiting on a person and an agent is person-held.
    if filters.waiting == WAITING_PERSON and card.held_by != PERSON_HOLDER:
        return False
    if filters.waiting == WAITING_AGENT and card.held_by != AGENT_HOLDER:
        return False
    return True


def _sum(values: list[Decimal | None]) -> Decimal:
    return sum((v for v in values if v is not None), Decimal("0.00"))


def summarise(shown: list[Card]) -> Summary:
    """Counts per column over every shown card; the values and risks over live cards only."""
    live = [c for c in shown if not c.stopped]
    return Summary(
        counts={column: sum(1 for c in shown if c.column == column) for column in COLUMNS},
        planning_value=_sum([c.plan_total for c in live if c.column in (PLANNING, GO_AHEAD_GATE)]),
        delivery_value=_sum([c.plan_total for c in live if c.column == DELIVERY]),
        open_medium_high_risks=sum(c.open_medium_high_risks for c in live),
        decisions_waiting=sum(1 for c in live for w in c.waiting if w.holder == PERSON_HOLDER),
        stopped=len(shown) - len(live),
    )


def board(
    db: Session,
    policy: AutonomyPolicy,
    filters: Filters,
    *,
    agent_mode: str,
    risk_rules: RiskPolicy,
) -> Board:
    """The board under `filters`. The client and project manager choices come from every card,
    so a filter never hides its own options."""
    every = cards(
        db, policy, agent_mode=agent_mode, risk_rules=risk_rules, show_stopped=filters.show_stopped
    )
    shown = [c for c in every if _matches(c, filters)]
    columns: dict[str, list[Card]] = {column: [] for column in COLUMNS}
    for card in shown:
        columns[card.column].append(card)
    clients = sorted(
        {(c.client_id, c.client_name) for c in every if c.client_id is not None and c.client_name},
        key=lambda pair: (pair[1], pair[0]),
    )
    managers = sorted({c.project_manager for c in every if c.project_manager})
    return Board(columns, summarise(shown), clients, managers)
