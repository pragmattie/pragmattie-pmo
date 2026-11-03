"""From approval to project: what passing the approval gate creates (item 4.6).

`create_from_request` builds, from an approved request:

- the project, in stage `planning` with status `on_track`, named after the request, with the
  request's client, the deciding sponsor and the project manager the sponsor named;
- its brief, prefilled from the request and its value case, with who decides what taken from the
  approvers file's roles and what each role may decide here;
- its stakeholder list: the sponsor, the project manager and the client's contact if it has one;
- its first risks: one `proposed`, unscored AI risk for each readiness area rated yellow or red,
  and one `proposed` project risk for the request's own risks text, if any.

The request has no assumptions, so the assumptions list starts empty.

The risks come from the sponsor's decision and the readiness check a person confirmed, not from an
agent, so their `source` is `person`. Nothing is scored and nothing escalates: a person scores and
opens each one on the risk register. A readiness note that names an ethical, legal or reputational
concern marks its risk `needs_judgement` (R9).

Everything is only flushed, never committed: the gate route commits it with the decision in one
transaction, so a failure anywhere leaves nothing behind.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.api.intake import readiness_of, value_case_of
from app.authz import PERMISSIONS, Action
from app.models import (
    Client,
    Project,
    ProjectBrief,
    ProjectRequest,
    ProjectStakeholder,
    Risk,
    User,
    ValueCaseBenefit,
)
from app.models.intake import LONG_TEXT_MAX, READINESS_AREAS
from app.policy import get_policies
from app.roles import Role

STAGE = "planning"
STATUS = "on_track"

# What each decision right lets a role decide on a project, in plain words.
DECIDES = {
    Action.PROJECT_WRITE: "the project's brief, stakeholders, assumptions and risks",
    Action.INTAKE_DECIDE: "new requests",
    Action.GATE_DECIDE: "stage gates",
    Action.RISK_ACCEPT: "accepting risks in the bands the risk policy gives the role",
    Action.REPORT_PUBLISH: "publishing status reports",
    Action.PROPOSAL_APPROVE: "client proposals",
}

AREA_NAMES = {
    "problem": "the problem is worth solving",
    "approach": "AI is the right tool",
    "data": "the data is good enough",
}
READINESS_EFFECT = (
    "Until this readiness area is resolved, the project may not deliver the outcome that was "
    "approved."
)
REQUEST_RISK_TITLE = "Risks named in the request"
REQUEST_RISK_EFFECT = "To be described when this risk is scored."
RESPONSE_PLAN = "To be decided when this risk is scored."
# A readiness note naming one of these needs a person's judgement, not a number (R9).
JUDGEMENT = re.compile(r"ethic|legal|reputation", re.IGNORECASE)


@dataclass
class Created:
    """Everything passing the gate made, by id once flushed."""

    project: Project
    brief: ProjectBrief
    stakeholders: list[ProjectStakeholder] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)

    def ids(self) -> dict[str, Any]:
        return {
            "project_id": self.project.id,
            "brief_id": self.brief.id,
            "stakeholder_ids": [s.id for s in self.stakeholders],
            "assumption_ids": [],
            "risk_ids": [r.id for r in self.risks],
        }


def _fit(text: str) -> str:
    """Text cut to fit a long-text column."""
    return text if len(text) <= LONG_TEXT_MAX else text[: LONG_TEXT_MAX - 1] + "…"


def decision_rights() -> list[dict[str, str]]:
    """Who decides what: each role in the approvers file that holds decision rights here."""
    rights = []
    for role, approver in get_policies().approvers.roles.items():
        try:
            actions = PERMISSIONS[Role(role)]
        except ValueError:
            continue  # a role outside the product, such as the repository owner
        decides = [words for action, words in DECIDES.items() if action in actions]
        if decides:
            rights.append({"role": role, "title": approver.title, "decides": "; ".join(decides)})
    return rights


def _benefit_line(benefit: ValueCaseBenefit) -> str:
    details = [
        f"measure: {benefit.measure}" if benefit.measure else None,
        f"target: {benefit.target}" if benefit.target else None,
        f"by {benefit.target_date.isoformat()}" if benefit.target_date else None,
    ]
    shown = ", ".join(d for d in details if d)
    return f"- {benefit.description}" + (f" ({shown})" if shown else "")


def _brief(db: Session, request: ProjectRequest, project: Project) -> ProjectBrief:
    value_case = value_case_of(db, request.id)
    purpose, objectives = request.problem, request.outcome
    budget_low, budget_high = request.budget_low, request.budget_high
    if value_case is not None:
        if value_case.strategic_fit:
            purpose = f"{purpose}\n\nStrategic fit: {value_case.strategic_fit}"
        benefits = [_benefit_line(b) for b in value_case.benefits]
        if benefits:
            objectives = objectives + "\n\nBenefits:\n" + "\n".join(benefits)
        if value_case.cost_low is not None or value_case.cost_high is not None:
            budget_low, budget_high = value_case.cost_low, value_case.cost_high
    return ProjectBrief(
        project_id=project.id,
        purpose=_fit(purpose),
        objectives=_fit(objectives),
        scope_in=request.asked_for,
        scope_out=None,
        wanted_by=request.wanted_by,
        budget_low=budget_low,
        budget_high=budget_high,
        decision_rights=decision_rights(),
    )


def _stakeholders(
    db: Session, project: Project, sponsor: User, pm: User
) -> list[ProjectStakeholder]:
    people = [
        ProjectStakeholder(
            project_id=project.id,
            name=sponsor.display_name,
            user_id=sponsor.id,
            role_or_interest="Sponsor",
        ),
        ProjectStakeholder(
            project_id=project.id,
            name=pm.display_name,
            user_id=pm.id,
            role_or_interest="Project manager",
        ),
    ]
    client = db.get(Client, project.client_id) if project.client_id else None
    if client is not None and client.contact_name:
        people.append(
            ProjectStakeholder(
                project_id=project.id,
                name=client.contact_name,
                user_id=None,
                role_or_interest=f"Client contact, {client.name}",
            )
        )
    return people


def _risk(project: Project, **values: Any) -> Risk:
    """A `proposed`, unscored threat from a person's decision, for the team to score."""
    return Risk(
        project_id=project.id,
        kind="threat",
        objectives=[],
        probability=None,
        impact=None,
        score=None,
        band=None,
        response="reduce",
        response_plan=RESPONSE_PLAN,
        owner_user_id=None,
        status="proposed",
        source="person",
        **values,
    )


def _first_risks(db: Session, request: ProjectRequest, project: Project) -> list[Risk]:
    risks = []
    check = readiness_of(db, request.id) if request.uses_ai else None
    if check is not None:
        for area in READINESS_AREAS:
            rating = getattr(check, f"{area}_rating")
            if rating not in ("yellow", "red"):
                continue
            note = getattr(check, f"{area}_note") or ""
            risks.append(
                _risk(
                    project,
                    title=f"Readiness: {area} rated {rating} ({AREA_NAMES[area]})",
                    cause=note or f"The readiness check rated the {area} area {rating}.",
                    effect=READINESS_EFFECT,
                    category="ai",
                    needs_judgement=bool(JUDGEMENT.search(note)),
                )
            )
    if request.known_risks:
        risks.append(
            _risk(
                project,
                title=REQUEST_RISK_TITLE,
                cause=request.known_risks,
                effect=REQUEST_RISK_EFFECT,
                category="project",
                needs_judgement=False,
            )
        )
    return risks


def create_from_request(db: Session, request: ProjectRequest, sponsor: User, pm: User) -> Created:
    """The project and the records planning starts from, flushed but not committed."""
    project = Project(
        client_id=request.client_id,
        name=request.title,
        stage=STAGE,
        status=STATUS,
        sponsor=sponsor.display_name,
        sponsor_email=sponsor.email,
        sponsor_user_id=sponsor.id,
        pm=pm.display_name,
        pm_email=pm.email,
        pm_user_id=pm.id,
    )
    db.add(project)
    db.flush()
    created = Created(
        project=project,
        brief=_brief(db, request, project),
        stakeholders=_stakeholders(db, project, sponsor, pm),
        risks=_first_risks(db, request, project),
    )
    db.add_all([created.brief, *created.stakeholders, *created.risks])
    db.flush()
    return created
