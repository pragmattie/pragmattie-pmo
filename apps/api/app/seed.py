"""Demo data: `python -m app.seed [--reset]` and `python -m app.seed users [--password-file PATH]`.

The demo portfolio (item 6.9) starts busy, so the board, the inbox and the orchestrator have work
from the first minute. Three invented client companies (all simulated: their contacts are on
`.example`) and "PragMattie Sync", an internal project with no client, hold a card in every board
column, each in a state the orchestrator has a next step for:

- a draft request with no value case (the intake agent drafts one);
- a submitted request whose value case and readiness check are confirmed (its approval gate);
- a project in planning with an empty plan (the planner agent drafts it);
- one with a complete plan and no proposal (the orchestrator drafts the proposal);
- one with a sent proposal (the simulated client answers it);
- one whose client accepted its proposal, with every go-ahead check met (its go-ahead gate);
- one in delivery with baseline 1 and a small pending change request, raised by the second
  project manager, which the project's own project manager decides;
- and one project in closure and one closed, which wait on nobody.

Each original project keeps a few scored risks, AI and ordinary, so every priority band appears;
each risk's score and band come from the risk policy, as for any risk. Every person, company,
email address and website is made up and uses the reserved `.example` domain. The people the
data names (who raised a request, owns a milestone, decided a gate) are the demo users below.

Seeding is deterministic and idempotent: each seeded client, project, request and risk has a fixed
`seed_key`, and running the seed again sets those rows back to the values below without adding
duplicates. A seeded project or request gets its plan, proposal, gate decision, baseline, change
request, value case and readiness check when it is first made; one that already exists keeps
what it has. Each seeded risk gets one audit row when it is created or set back, none when it is
unchanged.

`--reset` restores exactly the state above. It first deletes the seeded projects and requests with
everything hung on them, whoever added it (risks, plans, proposals, gate decisions, baselines,
change requests, and a project made from a seeded request), then every orchestrator action and
inbox item, then the seeded clients nothing else uses; rows on anything people made are never
touched. The audit table stays append-only: nothing in it is deleted, and every run adds one row
saying what it seeded and, on a reset, what it cleared. The demo users the data needs are made if
missing (with a random password nobody is shown) and set active with their roles; their
passwords are left alone.

`users` creates or restores the demo users, one per role plus a second project manager, on
`.example` addresses. Every run gives each of them a new random password, sets the role, display
name and active flag back to their seed values and clears any lockout. The passwords exist only in
memory: the database holds argon2id hashes, the audit row holds the roles and a count, and nothing
is logged. After the commit they are printed once to the terminal, or with `--password-file`
written to a git-ignored file only. Because public CI logs would show them, printing is refused
when `CI` is set. Every refusal happens before the database is touched, so a refused run rotates
nothing.
"""

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.audit import record_decision
from app.authz import Role
from app.baselines import freeze
from app.changes import compute_effect
from app.gates import PROJECT_CHECKS
from app.models import (
    ChangeRequest,
    Client,
    GateDecision,
    InboxItem,
    OrchestratorAction,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectAssumption,
    ProjectBaseline,
    ProjectBrief,
    ProjectPlan,
    ProjectProposal,
    ProjectRequest,
    ProjectStakeholder,
    ProposalEvent,
    ProposalVersion,
    ReadinessAction,
    ReadinessCheck,
    Risk,
    User,
    ValueCase,
    ValueCaseBenefit,
)
from app.models.proposal import SIMULATED
from app.plans import milestones_of, new_plan
from app.policy import (
    ChangesPolicy,
    GatesPolicy,
    PolicyError,
    RiskPolicy,
    get_changes,
    get_policies,
)
from app.projects import decision_rights
from app.proposals import contributors, new_proposal, values_of
from app.risks import apply_score, record_risk_change, risk_policy, snapshot
from app.schemas.change import ChangeSet
from app.security import hash_password
from app.simulated_client import LABEL as SIMULATED_LABEL

SEEDED_AT = datetime(2026, 10, 1, 9, 0, 0)

# The demo users. Each role has one, keyed by the role's name; the second project manager raises
# the seeded change request, which the project's own project manager then decides.
DEMO_DOMAIN = "pragmattie.example"
PM = "project_manager"
SECOND_PM = "project_manager_2"
SPONSOR = "sponsor"

CLIENTS: tuple[dict[str, Any], ...] = (
    {
        "seed_key": "client-quillmere",
        "name": "Quillmere Freight Cooperative",
        "industry": "Logistics",
        "website": "https://quillmere-freight.example",
        "contact_name": "Odessa Brightwater",
        "contact_email": "odessa.brightwater@quillmere-freight.example",
    },
    {
        "seed_key": "client-brackenfold",
        "name": "Brackenfold Community Health",
        "industry": "Healthcare",
        "website": "https://brackenfold-health.example",
        "contact_name": "Ignatius Fernhollow",
        "contact_email": "ignatius.fernhollow@brackenfold-health.example",
    },
    {
        "seed_key": "client-larchwick",
        "name": "Larchwick Mutual Savings",
        "industry": "Financial services",
        "website": "https://larchwick-mutual.example",
        "contact_name": "Perpetua Hollingmoor",
        "contact_email": "perpetua.hollingmoor@larchwick-mutual.example",
    },
)

# `client` names a client's seed_key (None marks the internal project), and `pm` a demo user's
# key: the project's project manager is that user.
PROJECTS: tuple[dict[str, Any], ...] = (
    {
        # In delivery, with baseline 1 and a pending change request.
        "seed_key": "project-quillmere-routing",
        "client": "client-quillmere",
        "name": "Depot Routing Modernisation",
        "stage": "delivery",
        "sponsor": "Odessa Brightwater",
        "sponsor_email": "odessa.brightwater@quillmere-freight.example",
        "pm": PM,
        "status": "at_risk",
    },
    {
        # In planning: the plan is complete and there is no proposal yet.
        "seed_key": "project-brackenfold-intake",
        "client": "client-brackenfold",
        "name": "Patient Intake Redesign",
        "stage": "planning",
        "sponsor": "Ignatius Fernhollow",
        "sponsor_email": "ignatius.fernhollow@brackenfold-health.example",
        "pm": SECOND_PM,
        "status": "on_track",
    },
    {
        # In planning: the client accepted the proposal and every go-ahead check is met.
        "seed_key": "project-larchwick-onboarding",
        "client": "client-larchwick",
        "name": "Member Onboarding Portal",
        "stage": "planning",
        "sponsor": "Perpetua Hollingmoor",
        "sponsor_email": "perpetua.hollingmoor@larchwick-mutual.example",
        "pm": SECOND_PM,
        "status": "on_track",
    },
    {
        # In planning with an empty plan (its scope comes from the brief).
        "seed_key": "project-pragmattie-sync",
        "client": None,
        "name": "PragMattie Sync",
        "stage": "planning",
        "sponsor": "Rosalind Ashcombe",
        "sponsor_email": "rosalind.ashcombe@pragmattie.example",
        "pm": PM,
        "status": "on_track",
    },
    {
        # In planning: the proposal was sent to the (simulated) client and has no answer.
        "seed_key": "project-brackenfold-rota",
        "client": "client-brackenfold",
        "name": "Clinic Rota Assistant",
        "stage": "planning",
        "sponsor": "Ignatius Fernhollow",
        "sponsor_email": "ignatius.fernhollow@brackenfold-health.example",
        "pm": PM,
        "status": "on_track",
    },
    {
        "seed_key": "project-quillmere-yard",
        "client": "client-quillmere",
        "name": "Yard Camera Pilot",
        "stage": "closure",
        "sponsor": "Odessa Brightwater",
        "sponsor_email": "odessa.brightwater@quillmere-freight.example",
        "pm": SECOND_PM,
        "status": "on_track",
    },
    {
        "seed_key": "project-larchwick-statements",
        "client": "client-larchwick",
        "name": "Statement Archive Move",
        "stage": "closed",
        "sponsor": "Perpetua Hollingmoor",
        "sponsor_email": "perpetua.hollingmoor@larchwick-mutual.example",
        "pm": PM,
        "status": "complete",
    },
)

# The brief of each project in planning or delivery, by project seed_key. Money is a decimal.
BRIEFS: dict[str, dict[str, Any]] = {
    "project-quillmere-routing": {
        "purpose": "Plan depot routes from the day's orders instead of a fixed weekly plan.",
        "objectives": "Cut empty running by a tenth and plan a depot's day in under ten minutes.",
        "scope_in": "Route planning for the four northern depots and the drivers' route view.",
        "scope_out": "Vehicle tracking hardware and payroll.",
        "wanted_by": date(2027, 6, 30),
        "budget_low": Decimal("100000.00"),
        "budget_high": Decimal("120000.00"),
    },
    "project-brackenfold-intake": {
        "purpose": "Make the clinic's intake form quicker for patients and safer for nurses.",
        "objectives": "Halve the time to complete intake and keep a nurse's review of every case.",
        "scope_in": "The online intake form, intake suggestions for nurses and the nurse's queue.",
        "scope_out": "Appointment booking and clinical records.",
        "wanted_by": date(2027, 5, 31),
        "budget_low": Decimal("70000.00"),
        "budget_high": Decimal("90000.00"),
    },
    "project-larchwick-onboarding": {
        "purpose": "Let new members open a savings account online without visiting a branch.",
        "objectives": "Open nine in ten new accounts online within a day of applying.",
        "scope_in": "The online application, the identity check and an onboarding helper.",
        "scope_out": "Loans, mortgages and changes to existing accounts.",
        "wanted_by": date(2027, 4, 30),
        "budget_low": Decimal("80000.00"),
        "budget_high": Decimal("95000.00"),
    },
    "project-pragmattie-sync": {
        "purpose": "Bring the next round of delivery work under the same agent rules as the last.",
        "objectives": "Ship each agreed item with a person's approval and a full audit trail.",
        "scope_in": "The next milestone's items, their reviews and the audit report.",
        "scope_out": "New client work.",
        "wanted_by": date(2027, 3, 31),
        "budget_low": Decimal("30000.00"),
        "budget_high": Decimal("45000.00"),
    },
    "project-brackenfold-rota": {
        "purpose": "Draft the clinic's weekly rota from leave and skills, for a manager to check.",
        "objectives": "Publish each rota two weeks ahead with every shift covered.",
        "scope_in": "Rota drafts, the manager's review screen and the published rota.",
        "scope_out": "Pay and time recording.",
        "wanted_by": date(2027, 4, 30),
        "budget_low": Decimal("40000.00"),
        "budget_high": Decimal("55000.00"),
    },
}

# The complete plan of each project that has one, by project seed_key. A milestone is (name,
# target date, owner's demo user key, description); a deliverable is (name, its milestone's
# position); an estimate line is (description, category, amount). Scope comes from the brief.
PLANS: dict[str, dict[str, Any]] = {
    "project-quillmere-routing": {
        "contingency_pct": 10,
        "report_period": "weekly",
        "report_due_day": 5,
        "milestones": (
            (
                "Route data ready",
                date(2026, 12, 11),
                PM,
                "Depot and order data loaded and checked.",
            ),
            ("Pilot depot live", date(2027, 2, 12), PM, "One depot plans its routes daily."),
            ("All four depots live", date(2027, 5, 28), PM, "Every northern depot uses it."),
        ),
        "deliverables": (
            ("Depot data feed", 0),
            ("Route planner", 1),
            ("Drivers' route view", 2),
        ),
        "lines": (
            ("Delivery team", "people", Decimal("62000.00")),
            ("Routing service licence", "software", Decimal("18000.00")),
            ("Depot training", "services", Decimal("20000.00")),
        ),
    },
    "project-brackenfold-intake": {
        "contingency_pct": 10,
        "report_period": "fortnightly",
        "report_due_day": 3,
        "milestones": (
            ("Intake rules agreed", date(2027, 1, 15), SECOND_PM, "Nurses sign off the rules."),
            ("Form pilot", date(2027, 3, 12), SECOND_PM, "One clinic uses the new form."),
            ("Suggestions live", date(2027, 5, 14), SECOND_PM, "Nurses see suggestions."),
        ),
        "deliverables": (
            ("Intake rule book", 0),
            ("Online intake form", 1),
            ("Nurse's queue with suggestions", 2),
        ),
        "lines": (
            ("Design and build team", "people", Decimal("48000.00")),
            ("Form hosting", "software", Decimal("12000.00")),
            ("Fairness review", "services", Decimal("6000.00")),
        ),
    },
    "project-larchwick-onboarding": {
        "contingency_pct": 10,
        "report_period": "weekly",
        "report_due_day": 4,
        "milestones": (
            ("Product sheets ready", date(2027, 1, 22), SECOND_PM, "Helper answers from these."),
            (
                "Identity check linked",
                date(2027, 3, 5),
                SECOND_PM,
                "The branch check works online.",
            ),
            ("Portal open", date(2027, 4, 23), SECOND_PM, "Members apply online."),
        ),
        "deliverables": (
            ("Checked product sheets", 0),
            ("Online identity check", 1),
            ("Application portal and helper", 2),
        ),
        "lines": (
            ("Build team", "people", Decimal("52000.00")),
            ("Portal hosting", "software", Decimal("15000.00")),
            ("Compliance review", "services", Decimal("9000.00")),
        ),
    },
    "project-brackenfold-rota": {
        "contingency_pct": 10,
        "report_period": "monthly",
        "report_due_day": 10,
        "milestones": (
            ("Rota rules captured", date(2027, 1, 29), PM, "Leave, skills and cover rules."),
            ("First drafted rota", date(2027, 3, 19), PM, "A manager checks a real draft."),
        ),
        "deliverables": (
            ("Rota rule set", 0),
            ("Rota drafts and review screen", 1),
        ),
        "lines": (
            ("Build team", "people", Decimal("30000.00")),
            ("Scheduling service", "software", Decimal("8000.00")),
            ("Manager training", "services", Decimal("4000.00")),
        ),
    },
}

# The proposal of each project that has one, by project seed_key. `status` is how far it got:
# `sent` (waiting for the client) or `accepted` (by the simulated client). `day` places its history.
PROPOSALS: dict[str, dict[str, Any]] = {
    "project-quillmere-routing": {
        "status": "accepted",
        "day": 0,
        "summary": "We will plan Quillmere's northern depot routes each day from live orders.",
        "assumptions": "Quillmere provides the depot data feed by the first milestone.",
        "terms": "Invoiced monthly against milestones; prices exclude tax.",
    },
    "project-larchwick-onboarding": {
        "status": "accepted",
        "day": 10,
        "summary": "We will build Larchwick's online onboarding, with a helper that answers only "
        "from the product sheets.",
        "assumptions": "Larchwick's compliance committee names one member to approve changes.",
        "terms": "Invoiced monthly against milestones; prices exclude tax.",
    },
    "project-brackenfold-rota": {
        "status": "sent",
        "day": 20,
        "summary": "We will draft Brackenfold's weekly rotas for a manager to check and publish.",
        "assumptions": "Brackenfold shares leave and skills records before the first milestone.",
        "terms": "Invoiced monthly against milestones; prices exclude tax.",
    },
}

# The project that passed its go-ahead gate, and the change request pending on it: raised by
# the second project manager, small enough for the project's own project manager to decide.
GO_AHEAD_PROJECT = "project-quillmere-routing"
GO_AHEAD_REASON = "The plan is complete, inside the budget, and the client accepted the proposal."
GO_AHEAD_NOTE = "Agreed with the delivery team and the client"
CHANGE: dict[str, Any] = {
    "project": GO_AHEAD_PROJECT,
    "raised_by": SECOND_PM,
    "title": "Move the pilot three days for the depot's stock count",
    "reason": "The pilot depot runs its yearly stock count in the pilot week, so drivers can't be "
    "trained then; the trainer's extra days cost a little more.",
    "move": (1, date(2027, 2, 17)),  # the pilot milestone, Friday to the next Wednesday
    "add_lines": (("Extra trainer days", "services", Decimal("2500.00")),),
}

# The seeded requests. `requested_by` is a demo user's key. A `value_case` and `readiness` are
# written and confirmed by that person.
REQUESTS: tuple[dict[str, Any], ...] = (
    {
        # A draft with no value case yet.
        "seed_key": "request-brackenfold-referrals",
        "client": "client-brackenfold",
        "title": "Referral Letter Triage",
        "asked_for": "Sort incoming referral letters by urgency for the duty nurse.",
        "problem": "Referral letters wait in one pile, so urgent ones can sit for a day.",
        "outcome": "Urgent referrals reach a nurse within the hour.",
        "budget_low": Decimal("25000.00"),
        "budget_high": Decimal("35000.00"),
        "wanted_by": date(2027, 6, 30),
        "uses_ai": True,
        "known_risks": "Letters hold medical details.",
        "requested_by": SECOND_PM,
        "status": "draft",
    },
    {
        # Submitted, with its value case and readiness check confirmed: ready for its gate.
        "seed_key": "request-larchwick-queues",
        "client": "client-larchwick",
        "title": "Branch Queue Insights",
        "asked_for": "Forecast branch queues by hour so staff rotas follow demand.",
        "problem": "Branches are short-staffed at lunchtime and idle mid-morning.",
        "outcome": "Members wait under ten minutes at the busiest hour.",
        "budget_low": Decimal("30000.00"),
        "budget_high": Decimal("40000.00"),
        "wanted_by": date(2027, 7, 30),
        "uses_ai": True,
        "known_risks": "Footfall data is patchy for two branches.",
        "requested_by": PM,
        "status": "submitted",
        "value_case": {
            "cost_low": Decimal("30000.00"),
            "cost_high": Decimal("38000.00"),
            "cost_method": "Six weeks of a two-person team plus forecasting service fees.",
            "strategic_fit": "Supports the society's aim to cut branch waiting times.",
            "cost_of_not_doing": "Members keep queuing at lunchtime and complaints grow.",
            "benefit": {
                "description": "Shorter waits at the busiest hour",
                "measure": "Median wait at 12:00 to 13:00, from the queue system",
                "current_value": "18 minutes",
                "target": "Under 10 minutes",
                "target_date": date(2027, 10, 29),
            },
        },
        "readiness": {
            "problem": "Lunchtime queues are measured and members complain about them.",
            "approach": "Forecasting from past footfall suits the pattern; a person sets rotas.",
            "data": "Two years of hourly footfall for every branch but two; those are excluded.",
        },
    },
)


# A few scored risks per project, AI and ordinary, so every priority band appears. `project`
# names a project's seed_key. Bands follow from the risk policy; the comments show today's.
RISKS: tuple[dict[str, Any], ...] = (
    {
        "seed_key": "risk-quillmere-carrier-feed",
        "project": "project-quillmere-routing",
        "kind": "threat",
        "category": "project",
        "title": "The carrier's depot data feed arrives late",
        "cause": "The carrier is rebuilding its export and has not dated the new feed.",
        "effect": "Route testing waits and the depot go-live slips.",
        "objectives": ["schedule", "cost"],
        "probability": 4,
        "impact": 4,  # 16, High
        "response": "share",
        "response_plan": "Agree a dated feed in the carrier contract, with a fee for lateness.",
    },
    {
        "seed_key": "risk-quillmere-route-drift",
        "project": "project-quillmere-routing",
        "kind": "threat",
        "category": "ai",
        "title": "Suggested routes get worse as traffic patterns change",
        "cause": "The routing model was checked against last year's traffic.",
        "effect": "Drivers get longer routes and stop trusting the suggestions.",
        "objectives": ["quality"],
        "probability": 3,
        "impact": 3,  # 9, Medium
        "response": "reduce",
        "response_plan": "Compare suggested and driven routes monthly; retrain when they part.",
    },
    {
        "seed_key": "risk-quillmere-model-spend",
        "project": "project-quillmere-routing",
        "kind": "threat",
        "category": "ai",
        "title": "Route planning calls cost more than budgeted",
        "cause": "Each re-plan sends the whole depot's stops to the model.",
        "effect": "Monthly running cost goes over the agreed figure.",
        "objectives": ["cost"],
        "probability": 2,
        "impact": 2,  # 4, Low
        "response": "reduce",
        "response_plan": "Cap calls per depot per day in code and send only changed stops.",
    },
    {
        "seed_key": "risk-brackenfold-unfair-triage",
        "project": "project-brackenfold-intake",
        "kind": "threat",
        "category": "ai",
        "title": "Intake suggestions rank some patients unfairly",
        "cause": "Past intake notes under-record needs for some groups of patients.",
        "effect": "Some patients wait longer and the clinic's trust suffers.",
        "objectives": ["quality"],
        "probability": 3,
        "impact": 5,  # 15, High
        "response": "reduce",
        "response_plan": "Check suggestions by patient group before launch; keep a nurse's review.",
    },
    {
        "seed_key": "risk-brackenfold-patient-data",
        "project": "project-brackenfold-intake",
        "kind": "threat",
        "category": "ai",
        "title": "Patient details reach the model's logs",
        "cause": "Free-text intake answers can hold names and medical details.",
        "effect": "Private health information is exposed outside the clinic.",
        "objectives": ["quality", "cost"],
        "probability": 2,
        "impact": 5,  # 10, Medium
        "response": "avoid",
        "response_plan": "Strip personal details before any text reaches the model; log no text.",
    },
    {
        "seed_key": "risk-brackenfold-clinical-lead",
        "project": "project-brackenfold-intake",
        "kind": "threat",
        "category": "project",
        "title": "The clinical lead is called back to the wards",
        "cause": "Only one clinician knows the current intake rules in detail.",
        "effect": "Design questions wait until a cover clinician is found.",
        "objectives": ["schedule"],
        "probability": 2,
        "impact": 3,  # 6, Low
        "response": "reduce",
        "response_plan": "Name a second clinician now and record intake rules as they are agreed.",
    },
    {
        "seed_key": "risk-larchwick-wrong-answers",
        "project": "project-larchwick-onboarding",
        "kind": "threat",
        "category": "ai",
        "title": "The onboarding helper gives members wrong account terms",
        "cause": "The helper answers from general text when the product sheet says nothing.",
        "effect": "Members open accounts on terms the society does not offer.",
        "objectives": ["quality"],
        "probability": 3,
        "impact": 4,  # 12, Medium
        "response": "reduce",
        "response_plan": "Answer only from the product sheets and hand other questions to staff.",
    },
    {
        "seed_key": "risk-larchwick-slow-sign-off",
        "project": "project-larchwick-onboarding",
        "kind": "threat",
        "category": "project",
        "title": "Compliance sign-off takes weeks",
        "cause": "The compliance committee meets once a month.",
        "effect": "Each design change waits for the next meeting.",
        "objectives": ["schedule"],
        "probability": 4,
        "impact": 2,  # 8, Medium
        "response": "reduce",
        "response_plan": "Agree with the committee which changes one named member can approve.",
    },
    {
        "seed_key": "risk-larchwick-reuse-id-check",
        "project": "project-larchwick-onboarding",
        "kind": "opportunity",
        "category": "project",
        "title": "Reuse the branch identity check online",
        "cause": "The branches already use an identity check that works remotely.",
        "effect": "Onboarding needs less new work and finishes sooner.",
        "objectives": ["schedule", "cost"],
        "probability": 3,
        "impact": 2,  # 6, Low
        "response": "pursue",
        "response_plan": "Ask the branch systems team for the check's interface in the first week.",
    },
    {
        "seed_key": "risk-sync-hidden-instructions",
        "project": "project-pragmattie-sync",
        "kind": "threat",
        "category": "ai",
        "title": "Issue text steers the build agent",
        "cause": "The build agent reads issue and comment text that anyone can write.",
        "effect": "The agent acts on instructions planted in that text.",
        "objectives": ["quality"],
        "probability": 3,
        "impact": 5,  # 15, High
        "response": "reduce",
        "response_plan": "Treat issue text as data, limit the agent's tools, review every change.",
    },
    {
        "seed_key": "risk-sync-review-fatigue",
        "project": "project-pragmattie-sync",
        "kind": "threat",
        "category": "ai",
        "title": "Reviewers approve agent changes without reading them",
        "cause": "Most agent changes are small and pass, so reviews get quicker.",
        "effect": "A faulty change is merged with a person's approval on it.",
        "objectives": ["quality"],
        "probability": 3,
        "impact": 3,  # 9, Medium
        "response": "reduce",
        "response_plan": "Keep a checklist on each review and spot-check merged changes monthly.",
    },
    {
        "seed_key": "risk-sync-extra-requests",
        "project": "project-pragmattie-sync",
        "kind": "threat",
        "category": "project",
        "title": "New requests keep joining the current milestone",
        "cause": "Ideas raised in demos are added to the milestone as they come up.",
        "effect": "The milestone keeps moving and planned items wait.",
        "objectives": ["schedule"],
        "probability": 2,
        "impact": 3,  # 6, Low
        "response": "reduce",
        "response_plan": "Log new ideas for the next milestone unless the sponsor agrees a change.",
    },
)


# --- The demo users the data names


def demo_users() -> tuple[dict[str, str], ...]:
    """One demo user per role, keyed by the role's name, plus the second project manager.
    Derived from the roles, so a new role gets a user too."""
    users = [
        {
            "key": role.value,
            "role": role.value,
            "email": f"demo.{role.value.replace('_', '.')}@{DEMO_DOMAIN}",
            "display_name": f"Demo {role.value.replace('_', ' ').title()}",
        }
        for role in Role
    ]
    users.append(
        {
            "key": SECOND_PM,
            "role": Role.PROJECT_MANAGER.value,
            "email": f"demo.project.manager.2@{DEMO_DOMAIN}",
            "display_name": "Demo Project Manager 2",
        }
    )
    return tuple(users)


def demo_people(db: Session) -> dict[str, User]:
    """The demo users by key, each active with its seed role and name. One that is missing is
    made with a random password nobody is shown (`seed users` hands out ones to sign in with);
    an existing one keeps its password."""
    people = {}
    for values in demo_users():
        user = db.scalar(select(User).where(User.email == values["email"]))
        if user is None:
            password_hash = hash_password(secrets.token_urlsafe(PASSWORD_BYTES))
            user = User(email=values["email"], password_hash=password_hash)
            db.add(user)
        user.display_name = values["display_name"]
        user.role = values["role"]
        user.is_active = True
        people[values["key"]] = user
    db.flush()
    return people


# --- Clearing


@dataclass
class Cleared:
    clients: int = 0
    projects: int = 0
    requests: int = 0
    orchestrator_actions: int = 0
    inbox_items: int = 0


def _ids(db: Session, stmt) -> list[int]:
    # Read the ids first: MySQL can't delete from a table that its own subquery reads.
    return list(db.scalars(stmt))


def _clear_projects(db: Session, ids: list[int]) -> None:
    """Delete everything hung on the projects, children before the rows they point at."""
    proposals = _ids(db, select(ProjectProposal.id).where(ProjectProposal.project_id.in_(ids)))
    if proposals:
        db.execute(delete(ProposalEvent).where(ProposalEvent.proposal_id.in_(proposals)))
        db.execute(delete(ProposalVersion).where(ProposalVersion.proposal_id.in_(proposals)))
        db.execute(delete(ProjectProposal).where(ProjectProposal.id.in_(proposals)))
    # A baseline points at the gate decision or change request that made it.
    db.execute(delete(ProjectBaseline).where(ProjectBaseline.project_id.in_(ids)))
    db.execute(delete(ChangeRequest).where(ChangeRequest.project_id.in_(ids)))
    db.execute(
        delete(GateDecision).where(
            GateDecision.subject_type == "project", GateDecision.subject_id.in_(ids)
        )
    )
    db.execute(delete(Risk).where(Risk.project_id.in_(ids)))
    plans = _ids(db, select(ProjectPlan.id).where(ProjectPlan.project_id.in_(ids)))
    if plans:
        # A deliverable points at its milestone.
        for model in (PlanDeliverable, PlanEstimateLine, PlanMilestone):
            db.execute(delete(model).where(model.plan_id.in_(plans)))
        db.execute(delete(ProjectPlan).where(ProjectPlan.id.in_(plans)))
    for model in (ProjectBrief, ProjectStakeholder, ProjectAssumption):
        db.execute(delete(model).where(model.project_id.in_(ids)))


def _clear_requests(db: Session, ids: list[int]) -> None:
    value_cases = _ids(db, select(ValueCase.id).where(ValueCase.request_id.in_(ids)))
    if value_cases:
        db.execute(delete(ValueCaseBenefit).where(ValueCaseBenefit.value_case_id.in_(value_cases)))
        db.execute(delete(ValueCase).where(ValueCase.id.in_(value_cases)))
    checks = _ids(db, select(ReadinessCheck.id).where(ReadinessCheck.request_id.in_(ids)))
    if checks:
        db.execute(delete(ReadinessAction).where(ReadinessAction.check_id.in_(checks)))
        db.execute(delete(ReadinessCheck).where(ReadinessCheck.id.in_(checks)))
    db.execute(
        delete(GateDecision).where(
            GateDecision.subject_type == "request", GateDecision.subject_id.in_(ids)
        )
    )
    db.execute(delete(ProjectRequest).where(ProjectRequest.id.in_(ids)))


def clear_seeded(db: Session) -> Cleared:
    """Delete the seeded requests and projects (and a project made from a seeded request) with
    everything hung on them, whoever added it; every orchestrator action and inbox item; and the
    seeded clients that no remaining project or request uses. Rows on anything people made are
    kept, and so are the audit rows. The caller commits."""
    cleared = Cleared()
    cleared.inbox_items = db.execute(delete(InboxItem)).rowcount
    cleared.orchestrator_actions = db.execute(delete(OrchestratorAction)).rowcount
    requests = _ids(db, select(ProjectRequest.id).where(ProjectRequest.seed_key.is_not(None)))
    made_from = select(ProjectRequest.project_id).where(ProjectRequest.id.in_(requests))
    projects = _ids(
        db, select(Project.id).where(or_(Project.seed_key.is_not(None), Project.id.in_(made_from)))
    )
    if projects:
        _clear_projects(db, projects)
    if requests:
        _clear_requests(db, requests)
    if projects:
        db.execute(delete(Project).where(Project.id.in_(projects)))
    in_use = exists().where(Project.client_id == Client.id) | exists().where(
        ProjectRequest.client_id == Client.id
    )
    cleared.clients = db.execute(
        delete(Client).where(Client.seed_key.is_not(None), ~in_use)
    ).rowcount
    cleared.projects, cleared.requests = len(projects), len(requests)
    return cleared


# --- Seeding


@dataclass
class SeedResult:
    clients: int
    projects: int
    requests: int
    risks: int
    cleared: Cleared = field(default_factory=Cleared)


def _upsert[T: (Client, Project, ProjectRequest)](
    db: Session, model: type[T], values: dict[str, Any]
) -> tuple[T, bool]:
    """The seeded row, set to `values`, and whether it is new."""
    row = db.scalar(select(model).where(model.seed_key == values["seed_key"]))
    new = row is None
    if new:
        row = model()
        db.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    row.created_at = SEEDED_AT
    return row, new


def _seed_risk(db: Session, policy: RiskPolicy, values: dict[str, Any], project_id: int) -> None:
    """Create or restore one seeded risk, scored from the policy, with one audit row when it is
    new or was changed (no actor: the seed command acts for whoever runs it)."""
    risk = db.scalar(select(Risk).where(Risk.seed_key == values["seed_key"]))
    before = None if risk is None else snapshot(risk)
    if risk is None:
        risk = Risk()
        db.add(risk)
    for key, value in values.items():
        setattr(risk, key, value)
    risk.project_id = project_id
    risk.status = "open"
    risk.source = "person"
    risk.needs_judgement = False
    risk.show_in_proposal = False
    risk.owner_user_id = None
    risk.created_at = SEEDED_AT
    apply_score(risk, policy)
    db.flush()
    if before is None:
        record_risk_change(db, risk, action="create", before=None, actor_user_id=None)
    elif snapshot(risk) != before:
        record_risk_change(db, risk, action="seed", before=before, actor_user_id=None)


def _ensure_plan(db: Session, project: Project) -> ProjectPlan:
    """The project's plan. A project without one gets an empty plan, scope prefilled from its
    brief if it has one, as for any new project (item 5.1)."""
    plan = db.scalar(select(ProjectPlan).where(ProjectPlan.project_id == project.id))
    if plan is None:
        brief = db.scalar(select(ProjectBrief).where(ProjectBrief.project_id == project.id))
        plan = new_plan(project.id, brief)
        db.add(plan)
        db.flush()
    return plan


def _fill_plan(db: Session, plan: ProjectPlan, values: dict[str, Any], people: dict) -> None:
    """Write a complete plan: milestones, deliverables, estimate lines and reporting."""
    plan.contingency_pct = values["contingency_pct"]
    plan.report_period = values["report_period"]
    plan.report_due_day = values["report_due_day"]
    milestones = []
    for name, target, owner, description in values["milestones"]:
        milestone = PlanMilestone(
            plan_id=plan.id,
            name=name,
            target_date=target,
            owner_user_id=people[owner].id,
            description=description,
        )
        milestones.append(milestone)
    db.add_all(milestones)
    db.flush()
    for name, position in values["deliverables"]:
        db.add(PlanDeliverable(plan_id=plan.id, name=name, milestone_id=milestones[position].id))
    for description, category, amount in values["lines"]:
        db.add(
            PlanEstimateLine(
                plan_id=plan.id, description=description, category=category, amount=amount
            )
        )
    db.flush()


def _seed_proposal(
    db: Session, project: Project, plan: ProjectPlan, values: dict[str, Any], people: dict
) -> None:
    """The proposal and its history: written by the project manager, submitted, approved by the
    sponsor, sent by the project manager and, when accepted, answered by the simulated client."""
    pm, sponsor = db.get(User, project.pm_user_id), people[SPONSOR]
    client = db.get(Client, project.client_id)
    at = SEEDED_AT + timedelta(days=values["day"])
    proposal = new_proposal(db, plan, pm.id)
    for name in ("summary", "assumptions", "terms"):
        setattr(proposal, name, values[name])
    proposal.created_at = at
    db.flush()
    copy = ProposalVersion(
        proposal_id=proposal.id,
        number=1,
        content={"project_name": project.name, "client_name": client.name, **values_of(proposal)},
        submitted_by=pm.id,
        # The project manager wrote and submitted it; the sponsor who approves it did not
        # contribute (item 6.10).
        contributors=contributors(proposal, pm.id),
        submitted_at=at + timedelta(hours=2),
    )
    db.add(copy)
    db.flush()
    sent_on = (at + timedelta(days=2)).date()
    events = [
        ProposalEvent(kind="approve", actor_user_id=sponsor.id, created_at=at + timedelta(days=1)),
        ProposalEvent(
            kind="sent",
            actor_user_id=pm.id,
            sent_to=f"{client.contact_name} <{client.contact_email}>",
            sent_on=sent_on,
            means="email",
            created_at=at + timedelta(days=2),
        ),
    ]
    if values["status"] == "accepted":
        answered = at + timedelta(days=3)
        events.append(
            ProposalEvent(
                kind="answer",
                source=SIMULATED,
                answer="accepted",
                answered_on=answered.date(),
                note=f"{SIMULATED_LABEL}: answered automatically in the demo",
                created_at=answered,
            )
        )
    for event in events:
        event.proposal_id, event.version_id = proposal.id, copy.id
    db.add_all(events)
    proposal.status = values["status"]
    proposal.updated_at = events[-1].created_at
    db.flush()


def _seed_go_ahead(
    db: Session, project: Project, plan: ProjectPlan, gates: GatesPolicy, people: dict
) -> ProjectBaseline:
    """The sponsor's pass at the go-ahead gate, each check as it stands, and baseline 1."""
    gate = gates.gates["go_ahead"]
    criteria = []
    for criterion in gate.criteria:
        result = PROJECT_CHECKS[criterion.check](db, project) if criterion.check else None
        criteria.append(
            {
                "id": criterion.id,
                "text": criterion.text,
                "kind": "check" if criterion.check else "person",
                "check": criterion.check,
                "met": result.met if result else True,
                "detail": result.detail if result else None,
                "note": None if result else GO_AHEAD_NOTE,
            }
        )
    at = SEEDED_AT + timedelta(days=5)
    decision = GateDecision(
        gate_id=gate.id,
        subject_type="project",
        subject_id=project.id,
        outcome="pass",
        decided_by=people[SPONSOR].id,
        reason=GO_AHEAD_REASON,
        criteria=criteria,
        policy_version=gates.digest,
        created_at=at,
    )
    db.add(decision)
    db.flush()
    baseline = freeze(db, plan, decision.id)
    baseline.created_at = at
    return baseline


def _seed_change(
    db: Session,
    project: Project,
    plan: ProjectPlan,
    baseline: ProjectBaseline,
    rules: ChangesPolicy,
    people: dict,
) -> None:
    """The submitted change request, measured against the baseline as the change routes do."""
    position, target = CHANGE["move"]
    milestone = milestones_of(db, plan.id)[position]
    changes = ChangeSet(
        move_milestones=[{"milestone_id": milestone.id, "target_date": target}],
        add_lines=[
            {"description": d, "category": c, "amount": f"{a:.2f}"}
            for d, c, a in CHANGE["add_lines"]
        ],
    ).model_dump(mode="json")
    raiser = people[CHANGE["raised_by"]]
    effect, decider = compute_effect(
        baseline.plan,
        changes,
        rules,
        baseline_number=baseline.number,
        has_project_manager=project.pm_user_id is not None,
        raised_by_project_manager=raiser.id == project.pm_user_id,
    )
    at = SEEDED_AT + timedelta(days=30)
    db.add(
        ChangeRequest(
            project_id=project.id,
            title=CHANGE["title"],
            reason=CHANGE["reason"],
            changes=changes,
            effect=effect,
            decider=decider,
            measured_baseline_number=baseline.number,
            status="submitted",
            raised_by=raiser.id,
            submitted_at=at,
            created_at=at,
            updated_at=at,
        )
    )
    db.flush()


def _build_project(
    db: Session, project: Project, people: dict, gates: GatesPolicy, rules: ChangesPolicy
) -> None:
    """A new seeded project's brief, plan, proposal, go-ahead decision and change request."""
    key = project.seed_key
    if key in BRIEFS:
        db.add(
            ProjectBrief(project_id=project.id, decision_rights=decision_rights(), **BRIEFS[key])
        )
        db.flush()
    plan = _ensure_plan(db, project)
    if key in PLANS:
        _fill_plan(db, plan, PLANS[key], people)
    if key in PROPOSALS:
        _seed_proposal(db, project, plan, PROPOSALS[key], people)
    if key == GO_AHEAD_PROJECT:
        baseline = _seed_go_ahead(db, project, plan, gates, people)
        if CHANGE["project"] == key:
            _seed_change(db, project, plan, baseline, rules, people)


def _build_request(db: Session, request: ProjectRequest, values: dict[str, Any]) -> None:
    """A new seeded request's value case and readiness check, confirmed by its raiser."""
    at = SEEDED_AT + timedelta(days=12)
    if "value_case" in values:
        case = dict(values["value_case"])
        benefit = case.pop("benefit")
        db.add(
            ValueCase(
                request_id=request.id,
                **case,
                source="person",
                confirmed_by=request.requested_by,
                confirmed_at=at,
                created_at=at,
                updated_at=at,
                benefits=[ValueCaseBenefit(position=1, **benefit)],
            )
        )
    if "readiness" in values:
        notes = values["readiness"]
        db.add(
            ReadinessCheck(
                request_id=request.id,
                **{f"{area}_rating": "green" for area in notes},
                **{f"{area}_note": note for area, note in notes.items()},
                source="person",
                confirmed_by=request.requested_by,
                confirmed_at=at,
                created_at=at,
                updated_at=at,
            )
        )
    if values["status"] == "submitted":
        request.submitted_at = at + timedelta(hours=1)
    db.flush()


AUDIT_AGENT = "seed"
PORTFOLIO_SUBJECT = "demo_portfolio"


def seed(db: Session, *, reset: bool = False, policy: RiskPolicy | None = None) -> SeedResult:
    """Create or restore the demo portfolio; with `reset`, clear it first. Risks are scored from
    `policy`, by default the risk policy. The policies are loaded before anything is written, so
    a `PolicyError` changes nothing. Adds one audit row for the run. The caller commits."""
    policy = policy or risk_policy()
    gates, rules = get_policies().gates, get_changes()
    cleared = clear_seeded(db) if reset else Cleared()
    people = demo_people(db)
    clients = {}
    for values in CLIENTS:
        clients[values["seed_key"]], _ = _upsert(db, Client, values)
    db.flush()
    projects, new_projects = {}, []
    for values in PROJECTS:
        project = dict(values)
        client_key, pm = project.pop("client"), people[project.pop("pm")]
        project["client_id"] = clients[client_key].id if client_key else None
        project.update(pm=pm.display_name, pm_email=pm.email, pm_user_id=pm.id)
        row, new = _upsert(db, Project, project)
        projects[row.seed_key] = row
        if new:
            new_projects.append(row)
    db.flush()
    for values in RISKS:
        risk = dict(values)
        _seed_risk(db, policy, risk, projects[risk.pop("project")].id)
    for project in projects.values():
        if project in new_projects:
            _build_project(db, project, people, gates, rules)
        else:
            _ensure_plan(db, project)
    for values in REQUESTS:
        fields = {k: v for k, v in values.items() if k not in ("value_case", "readiness")}
        fields["client_id"] = clients[fields.pop("client")].id
        fields["requested_by"] = people[fields["requested_by"]].id
        request, new = _upsert(db, ProjectRequest, fields)
        db.flush()
        if new:
            _build_request(db, request, values)
    result = SeedResult(len(CLIENTS), len(PROJECTS), len(REQUESTS), len(RISKS), cleared)
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=PORTFOLIO_SUBJECT,
        subject_id=uuid.uuid4().hex,
        status="ok",
        signals={"actor": "system", "command": f"python -m app.seed{' --reset' if reset else ''}"},
        action_taken={
            "action": "reset_portfolio" if reset else "seed_portfolio",
            "seeded": {
                "clients": result.clients,
                "projects": result.projects,
                "requests": result.requests,
                "risks": result.risks,
            },
            "cleared": asdict(cleared) if reset else None,
        },
    )
    return result


# --- `seed users`

AUDIT_SUBJECT = "demo_users"
PASSWORD_BYTES = 18  # token_urlsafe(18) gives 24 characters, about 144 bits.


@dataclass(frozen=True, repr=False)
class DemoLogin:
    role: str
    email: str
    password: str

    def __repr__(self) -> str:
        # Never show the password, even by accident in a traceback or a debugger.
        return f"DemoLogin(role={self.role!r}, email={self.email!r})"


def seed_users(db: Session) -> list[DemoLogin]:
    """Create or restore the demo users with new random passwords, and add one audit row.

    The passwords are returned and nowhere else. The caller commits, then hands them over."""
    logins = []
    for values in demo_users():
        user = db.scalar(select(User).where(User.email == values["email"]))
        if user is None:
            user = User(email=values["email"])
            db.add(user)
        password = secrets.token_urlsafe(PASSWORD_BYTES)
        user.display_name = values["display_name"]
        user.role = values["role"]
        user.is_active = True
        user.password_hash = hash_password(password)
        user.failed_logins = 0
        user.failed_window_started_at = None
        user.locked_until = None
        logins.append(DemoLogin(values["role"], values["email"], password))
    db.flush()
    record_decision(
        db,
        agent=AUDIT_AGENT,
        trigger="human",
        subject_type=AUDIT_SUBJECT,
        subject_id=uuid.uuid4().hex,
        status="ok",
        signals={"actor": "system", "command": "python -m app.seed users"},
        action_taken={
            "action": "seed_demo_users",
            "roles": [login.role for login in logins],
            "users": len(logins),
        },
    )
    return logins


class PasswordFileRefused(Exception):
    """The password file is not a safe place for the passwords. The message says why."""


def _git(path: Path, *args: str) -> int:
    """Run git in `path`'s folder and return its exit code. Raises PasswordFileRefused when git
    is missing or can't run, so the caller fails closed."""
    git = shutil.which("git")
    if git is None:
        raise PasswordFileRefused(
            "git is not installed here, so the git-ignore rule can't be checked"
        )
    try:
        result = subprocess.run(
            [git, *args, "--", str(path)],
            cwd=path.parent,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PasswordFileRefused(f"git could not run ({type(exc).__name__})") from None
    return result.returncode


def check_password_file(path: Path) -> Path:
    """The absolute path, if git confirms it is ignored and untracked. Otherwise refuse."""
    path = Path(os.path.abspath(path))
    if not path.parent.is_dir():
        raise PasswordFileRefused(f"the folder {path.parent} does not exist")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise PasswordFileRefused(f"{path} is not a plain file")
    if _git(path, "ls-files", "--error-unmatch") == 0:
        raise PasswordFileRefused(f"{path} is tracked by git; it must never be overwritten")
    ignored = _git(path, "check-ignore", "-q")
    if ignored == 1:
        raise PasswordFileRefused(f"{path} is not git-ignored (try a name like demo-passwords.txt)")
    if ignored != 0:
        raise PasswordFileRefused(
            f"git can't say whether {path} is ignored (is it in a repository?)"
        )
    return path


def open_password_file(path: Path) -> tuple[int, bool]:
    """Open (creating if needed) the file with owner-only access, without truncating it, so a run
    that fails later leaves an earlier file intact. Returns the descriptor and whether it is new.
    Windows ignores the mode; there the git-ignore rule and the user's profile protect it."""
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, flags, 0o600)
        created = False
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)  # tighten a file that already existed, before anything is written
    return fd, created


def format_logins(logins: list[DemoLogin]) -> str:
    rows = [("role", "email", "password"), *((x.role, x.email, x.password) for x in logins)]
    widths = [max(len(row[i]) for row in rows) for i in range(2)]
    lines = [f"{r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  {r[2]}" for r in rows]
    return "\n".join(lines) + "\n"


def _run_users(args: argparse.Namespace) -> int:
    # Every refusal comes before the database is touched, so a refused run rotates nothing.
    fd, created, path = None, False, None
    if args.password_file is not None:
        try:
            path = check_password_file(args.password_file)
            fd, created = open_password_file(path)
        except PasswordFileRefused as exc:
            print(f"Refused: {exc}. Nothing was changed.", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"Refused: can't open the password file ({exc.strerror}).", file=sys.stderr)
            return 2
    elif os.environ.get("CI"):
        print(
            "Refused: CI is set and CI logs may be public, so passwords are never printed there. "
            "Use --password-file with a git-ignored path. Nothing was changed.",
            file=sys.stderr,
        )
        return 2

    from app.db import SessionLocal

    try:
        try:
            with SessionLocal() as db:
                logins = seed_users(db)
                db.commit()
        except SQLAlchemyError as exc:
            name = type(exc).__name__
            print(f"Seed failed: {name}. Has `alembic upgrade head` run?", file=sys.stderr)
            if fd is not None:
                os.close(fd)
                fd = None
                if created:
                    path.unlink(missing_ok=True)
            return 1

        text = format_logins(logins)
        if fd is None:
            print(text, end="")
            print(f"Seeded {len(logins)} demo users. Shown once only; re-run to rotate.")
            return 0
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None  # the handle closes it now
            handle.write(text)
        print(f"Seeded {len(logins)} demo users; their passwords are in {path}.")
        return 0
    finally:
        if fd is not None:
            os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reset",
        action="store_true",
        help="restore the demo portfolio exactly, clearing the orchestrator's work too",
    )
    commands = parser.add_subparsers(dest="command")
    users = commands.add_parser(
        "users", help="create or restore the demo users, with new random passwords"
    )
    users.add_argument(
        "--password-file",
        type=Path,
        metavar="PATH",
        help="write the passwords to this git-ignored file (mode 0600) instead of the terminal",
    )
    args = parser.parse_args(argv)
    if args.command == "users":
        if args.reset:
            parser.error("--reset applies to the demo portfolio, not to users")
        return _run_users(args)

    # Imported here: app.db builds its engine from the settings on import.
    from app.db import SessionLocal

    try:
        with SessionLocal() as db:
            result = seed(db, reset=args.reset)
            db.commit()
    except SQLAlchemyError as exc:
        name = type(exc).__name__
        print(f"Seed failed: {name}. Has `alembic upgrade head` run?", file=sys.stderr)
        return 1
    except PolicyError as exc:
        print(f"Seed failed: a policy could not be loaded: {exc}", file=sys.stderr)
        return 1
    if args.reset:
        c = result.cleared
        print(
            f"Cleared {c.clients} clients, {c.projects} projects, {c.requests} requests, "
            f"{c.orchestrator_actions} orchestrator actions and {c.inbox_items} inbox items."
        )
    print(
        f"Seeded {result.clients} clients, {result.projects} projects, {result.requests} requests "
        f"and {result.risks} risks."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
