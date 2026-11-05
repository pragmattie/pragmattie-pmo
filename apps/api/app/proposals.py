"""What a proposal takes from the plan, and the checker that compares the two (item 5.2).

- `plan_facts`: what the plan says now: its total, estimate lines, milestones and deliverables,
  and the risks ticked to show in the proposal (title and response only, never a score or band).
- `refresh_from_plan`: copies those facts onto the proposal. The price, dates and deliverables
  are only ever written here, by code; a client can't type them.
- `check`: rules in code, no model. A pure function of the proposal's values and the plan's facts
  that returns one `Flag` for each problem, naming the rule, the section and the text:

  - `stray_money`: a money figure in a text section that is neither the total nor an estimate
    line. A money figure is an amount with a currency sign (`$`, `£`, `€`), a thousands separator
    or exactly 2 decimal places; it is compared by value, so `$12,500` matches `12500.00`.
  - `stray_date`: a date in a text section that isn't a milestone date. Dates are read as
    `2027-03-01`, `1 March 2027` or `March 1, 2027` (month names may be short).
  - `missing_deliverable` and `missing_milestone`: one in the plan that the proposal lacks (a
    milestone counts as missing when its date moved). `removed_deliverable` and
    `removed_milestone`: one in the proposal that the plan no longer has.
  - `stale_price`: the proposal's price is not the plan's total.
  - `closed_risk`: a risk shown that has since been closed or rejected. `risk_changed`: a risk
    shown that is no longer ticked, or one ticked that the proposal doesn't show yet.

Refreshing from the plan clears every flag but the stray money and dates, which the project manager
fixes in the text.
"""

import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    ProjectPlan,
    ProjectProposal,
    Risk,
)
from app.models.proposal import TEXT_SECTIONS
from app.plans import CENT, compute_total, milestones_of

SECTION_PRICE = "price"
SECTION_DELIVERABLES = "deliverables"
SECTION_MILESTONES = "milestones"
SECTION_RISKS = "risks"
# Risks that may no longer be shown to a client.
GONE_STATUSES = ("closed", "rejected")

_MONEY = re.compile(
    r"(?<![\w.,])"
    r"(?:(?P<sign>[$£€])\s?)?"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"(?![\w.,]\d|\w|\s?%)"
)
_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH})\s+(\d{{4}})\b", re.I)
_MONTH_DAY = re.compile(rf"\b({_MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)


@dataclass(frozen=True)
class Flag:
    rule: str
    section: str
    text: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class PlanFacts:
    """What the plan and the risk register say now, in the form a proposal stores it."""

    version: int
    total: Decimal
    estimate_lines: list[dict[str, Any]]
    milestones: list[dict[str, Any]]
    deliverables: list[dict[str, Any]]
    shown_risks: list[dict[str, Any]]
    # Every risk a proposal might show, by id: its status and whether it is ticked.
    risk_states: dict[int, tuple[str, bool]]


def _money(value: Decimal) -> str:
    return str(Decimal(value).quantize(CENT))


def plan_facts(db: Session, plan: ProjectPlan) -> PlanFacts:
    lines = list(
        db.scalars(
            select(PlanEstimateLine)
            .where(PlanEstimateLine.plan_id == plan.id)
            .order_by(PlanEstimateLine.id)
        )
    )
    milestones = milestones_of(db, plan.id)
    names = {m.id: m.name for m in milestones}
    deliverables = list(
        db.scalars(
            select(PlanDeliverable)
            .where(PlanDeliverable.plan_id == plan.id)
            .order_by(PlanDeliverable.id)
        )
    )
    risks = list(
        db.scalars(select(Risk).where(Risk.project_id == plan.project_id).order_by(Risk.id))
    )
    return PlanFacts(
        version=plan.version,
        total=compute_total([line.amount for line in lines], plan.contingency_pct),
        estimate_lines=[
            {
                "description": line.description,
                "category": line.category,
                "amount": _money(line.amount),
            }
            for line in lines
        ],
        milestones=[
            {"name": m.name, "target_date": m.target_date.isoformat(), "description": m.description}
            for m in milestones
        ],
        deliverables=[
            {
                "name": d.name,
                "description": d.description,
                "milestone": names.get(d.milestone_id) if d.milestone_id else None,
            }
            for d in deliverables
        ],
        shown_risks=[
            {
                "risk_id": r.id,
                "title": r.title,
                "response": r.response,
                "response_plan": r.response_plan,
            }
            for r in risks
            if r.show_in_proposal and r.status not in GONE_STATUSES
        ],
        risk_states={r.id: (r.status, r.show_in_proposal) for r in risks},
    )


def refresh_from_plan(proposal: ProjectProposal, facts: PlanFacts) -> None:
    """Copy the price, estimate, milestones, deliverables and shown risks from the plan."""
    proposal.price = facts.total
    proposal.estimate_lines = facts.estimate_lines
    proposal.milestones = facts.milestones
    proposal.deliverables = facts.deliverables
    proposal.risks = facts.shown_risks
    proposal.plan_version_seen = facts.version


def _amounts(text: str) -> list[tuple[str, Decimal]]:
    """Each money figure in `text`, as written and as a value."""
    found = []
    for match in _MONEY.finditer(text):
        number = match.group("number")
        is_money = match.group("sign") or "," in number or re.search(r"\.\d{2}$", number)
        if not is_money:
            continue
        try:
            found.append((match.group(0).strip(), Decimal(number.replace(",", ""))))
        except InvalidOperation:  # pragma: no cover - the pattern only matches numbers
            continue
    return found


def _dates(text: str) -> list[tuple[str, date | None]]:
    """Each date in `text`, as written and as a date (None when it isn't a real day)."""

    def make(year: str, month: int, day: str) -> date | None:
        try:
            return date(int(year), month, int(day))
        except ValueError:
            return None

    found = [(m.group(0), make(m[1], int(m[2]), m[3])) for m in _ISO_DATE.finditer(text)]
    for m in _DAY_MONTH.finditer(text):
        found.append((m.group(0), make(m[3], _MONTHS[m[2].lower().rstrip(".")], m[1])))
    for m in _MONTH_DAY.finditer(text):
        found.append((m.group(0), make(m[3], _MONTHS[m[1].lower().rstrip(".")], m[2])))
    return found


def _without_dates(text: str) -> str:
    """`text` with its dates blanked, so a date's digits aren't read as money."""
    for pattern in (_ISO_DATE, _DAY_MONTH, _MONTH_DAY):
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


def check(values: dict[str, Any], facts: PlanFacts) -> list[Flag]:
    """The checker's flags for a proposal's `values` (its text sections and what it copied from
    the plan) against the plan's `facts`, in a fixed order."""
    flags: list[Flag] = []
    price = Decimal(values["price"]).quantize(CENT)
    allowed_money = {price} | {Decimal(line["amount"]) for line in values["estimate_lines"]}
    allowed_dates = {date.fromisoformat(m["target_date"]) for m in values["milestones"]}
    for section in TEXT_SECTIONS:
        text = values.get(section) or ""
        for written, amount in _amounts(_without_dates(text)):
            if amount not in allowed_money:
                flags.append(Flag("stray_money", section, written))
        for written, day in _dates(text):
            if day not in allowed_dates:
                flags.append(Flag("stray_date", section, written))

    if price != facts.total:
        flags.append(
            Flag(
                "stale_price",
                SECTION_PRICE,
                f"The proposal's price {_money(price)} is not the plan's total "
                f"{_money(facts.total)}",
            )
        )

    shown_deliverables = {d["name"] for d in values["deliverables"]}
    planned_deliverables = {d["name"] for d in facts.deliverables}
    for d in facts.deliverables:
        if d["name"] not in shown_deliverables:
            flags.append(Flag("missing_deliverable", SECTION_DELIVERABLES, d["name"]))
    for d in values["deliverables"]:
        if d["name"] not in planned_deliverables:
            flags.append(Flag("removed_deliverable", SECTION_DELIVERABLES, d["name"]))

    def key(m: dict[str, Any]) -> tuple[str, str]:
        return (m["name"], m["target_date"])

    shown_milestones = {key(m) for m in values["milestones"]}
    planned_milestones = {key(m) for m in facts.milestones}
    for m in facts.milestones:
        if key(m) not in shown_milestones:
            flags.append(
                Flag("missing_milestone", SECTION_MILESTONES, f"{m['name']} ({m['target_date']})")
            )
    for m in values["milestones"]:
        if key(m) not in planned_milestones:
            flags.append(
                Flag("removed_milestone", SECTION_MILESTONES, f"{m['name']} ({m['target_date']})")
            )

    shown_risks = {r["risk_id"] for r in values["risks"]}
    for r in values["risks"]:
        status, ticked = facts.risk_states.get(r["risk_id"], ("closed", False))
        if status in GONE_STATUSES:
            flags.append(Flag("closed_risk", SECTION_RISKS, f"{r['title']} ({status})"))
        elif not ticked:
            flags.append(
                Flag("risk_changed", SECTION_RISKS, f"{r['title']} (no longer ticked to show)")
            )
    for r in facts.shown_risks:
        if r["risk_id"] not in shown_risks:
            flags.append(
                Flag("risk_changed", SECTION_RISKS, f"{r['title']} (ticked to show, not yet shown)")
            )
    return flags


def values_of(proposal: ProjectProposal) -> dict[str, Any]:
    """What the checker reads, and a submit freezes, from a proposal."""
    return {
        **{name: getattr(proposal, name) for name in TEXT_SECTIONS},
        "price": _money(proposal.price),
        "estimate_lines": list(proposal.estimate_lines),
        "milestones": list(proposal.milestones),
        "deliverables": list(proposal.deliverables),
        "risks": list(proposal.risks),
    }
