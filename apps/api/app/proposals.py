"""What a proposal takes from the plan, and the checker that compares the two (item 5.2).

- `plan_facts`: what the plan says now: its total, estimate lines, milestones and deliverables,
  and the risks ticked to show in the proposal (title and response only, never a score or band).
- `refresh_from_plan`: copies those facts onto the proposal. The price, dates and deliverables
  are only ever written here, by code; a client can't type them.
- `check`: rules in code, no model. A pure function of the proposal's values and the plan's facts
  that returns one `Flag` for each problem, naming the rule, the section and the text:

  - `stray_money`: a money figure in a text section that is neither the plan's current total nor one
    of its current estimate lines (the plan's, not the proposal's copy, so a figure the plan has
    since dropped is flagged). A money figure is an amount with a currency sign (`$`, `£`, `€`), a
    thousands separator or exactly 2 decimal places. It matches when its value is exactly equal at 2
    decimal places, with or without the sign, separators or a trailing `.00`, so `$12,500`,
    `12,500.00` and `$12500` all match `12500.00`. A shorthand figure (`$40k`, `40k`, `£1.2m`, `$2
    million`) is never an exact amount, so it is always flagged. A bare whole number with none of
    these marks (`3 workshops`, `12500`) is not read as money, nor is a percentage.
  - `stray_date`: a date in a text section that isn't a milestone date. Only three forms are read:
    ISO `2027-01-15`, `15 January 2027` and `January 15, 2027` (month names may be short, as
    `Jan`, and a day may have an ordinal, as `15th`). Other forms are not read, so they are never
    flagged: numeric dates such as `15/01/2027` or `01/15/2027`, a month without a day
    (`January 2027`), a day without a year, and words such as `mid-January` or `Q1 2027`.
  - `missing_deliverable` and `missing_milestone`: one in the plan that the proposal lacks (a
    milestone counts as missing when its date moved). `removed_deliverable` and
    `removed_milestone`: one in the proposal that the plan no longer has.
    `changed_deliverable` and `changed_milestone`: one in both whose description the plan has
    since changed.
  - `stale_price`: the proposal's price is not the plan's total.
  - `changed_estimate_line`, `missing_estimate_line` and `removed_estimate_line`: the proposal's
    copied estimate lines compared with the plan's line by line, in order (description, category
    and amount). A line that differs, one the plan has added, or one the plan no longer has is
    flagged, even when the total is unchanged.
  - `closed_risk`: a risk shown that has since been closed or rejected. `risk_changed`: a risk
    shown that is no longer ticked, one ticked that the proposal doesn't show yet, or one shown
    whose title or response (the response or its plan) has changed in the risk register.

Refreshing from the plan clears every flag but the stray money and dates, which the project manager
fixes in the text. It never changes who wrote a section.

Who wrote each text section (item 5.3) is in `proposal.section_sources`. `unreviewed_sections`
names the agent's drafts no person has edited or confirmed yet (they block submit), and
`may_replace` says whether the proposal agent may write a section: only when it is empty or still
an unreviewed agent draft.
"""

import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import zip_longest
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    PlanDeliverable,
    PlanEstimateLine,
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
# What a proposal shows of a risk; a change to any of them flags it until refreshed.
RISK_SHOWN_FIELDS = ("title", "response", "response_plan")

_MONEY = re.compile(
    r"(?<![\w.,])"
    r"(?:(?P<sign>[$£€])\s?)?"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"(?P<scale>k|m|bn|\s(?:thousand|million|billion))?"
    r"(?![\w.,]\d|\w|\s?%)",
    re.I,
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


def _amounts(text: str) -> list[tuple[str, Decimal | None]]:
    """Each money figure in `text`, as written and as a value at 2 decimal places (None for a
    shorthand figure such as `$40k`, which is never an exact amount)."""
    found: list[tuple[str, Decimal | None]] = []
    for match in _MONEY.finditer(text):
        number, sign, scale = match.group("number"), match.group("sign"), match.group("scale")
        written = match.group(0).strip()
        if scale:
            # `$40k`, `40k` or `$2 million`; a bare `5m` or `3 million` may not be money.
            if sign or scale.lower() == "k":
                found.append((written, None))
            continue
        if not (sign or "," in number or re.search(r"\.\d{2}$", number)):
            continue
        try:
            found.append((written, Decimal(number.replace(",", "")).quantize(CENT)))
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


def _line_text(line: dict[str, Any]) -> str:
    return f"{line['description']} ({line['category']}) {_money(Decimal(line['amount']))}"


def _same_line(shown: dict[str, Any], planned: dict[str, Any]) -> bool:
    return (
        shown["description"] == planned["description"]
        and shown["category"] == planned["category"]
        and Decimal(shown["amount"]) == Decimal(planned["amount"])
    )


def check(values: dict[str, Any], facts: PlanFacts) -> list[Flag]:
    """The checker's flags for a proposal's `values` (its text sections and what it copied from
    the plan) against the plan's `facts`, in a fixed order."""
    flags: list[Flag] = []
    price = Decimal(values["price"]).quantize(CENT)
    # The figures the text may quote are the plan's current ones, never the proposal's copy.
    allowed_money = {Decimal(facts.total).quantize(CENT)} | {
        Decimal(line["amount"]).quantize(CENT) for line in facts.estimate_lines
    }
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

    for number, (shown, planned) in enumerate(
        zip_longest(values["estimate_lines"], facts.estimate_lines), start=1
    ):
        if shown is None:
            flags.append(
                Flag(
                    "missing_estimate_line",
                    SECTION_PRICE,
                    f"Estimate line {number}: {_line_text(planned)} is in the plan, "
                    "not the proposal",
                )
            )
        elif planned is None:
            flags.append(
                Flag(
                    "removed_estimate_line",
                    SECTION_PRICE,
                    f"Estimate line {number}: {_line_text(shown)} is no longer in the plan",
                )
            )
        elif not _same_line(shown, planned):
            flags.append(
                Flag(
                    "changed_estimate_line",
                    SECTION_PRICE,
                    f"Estimate line {number}: {_line_text(shown)} is now "
                    f"{_line_text(planned)} in the plan",
                )
            )

    shown_deliverables = {d["name"]: d for d in values["deliverables"]}
    planned_deliverables = {d["name"]: d for d in facts.deliverables}
    for d in facts.deliverables:
        shown = shown_deliverables.get(d["name"])
        if shown is None:
            flags.append(Flag("missing_deliverable", SECTION_DELIVERABLES, d["name"]))
        elif shown.get("description") != d["description"]:
            flags.append(
                Flag(
                    "changed_deliverable",
                    SECTION_DELIVERABLES,
                    f"{d['name']} (description changed in the plan)",
                )
            )
    for d in values["deliverables"]:
        if d["name"] not in planned_deliverables:
            flags.append(Flag("removed_deliverable", SECTION_DELIVERABLES, d["name"]))

    def key(m: dict[str, Any]) -> tuple[str, str]:
        return (m["name"], m["target_date"])

    shown_milestones = {key(m): m for m in values["milestones"]}
    planned_milestones = {key(m) for m in facts.milestones}
    for m in facts.milestones:
        shown = shown_milestones.get(key(m))
        if shown is None:
            flags.append(
                Flag("missing_milestone", SECTION_MILESTONES, f"{m['name']} ({m['target_date']})")
            )
        elif shown.get("description") != m["description"]:
            flags.append(
                Flag(
                    "changed_milestone",
                    SECTION_MILESTONES,
                    f"{m['name']} ({m['target_date']}): description changed in the plan",
                )
            )
    for m in values["milestones"]:
        if key(m) not in planned_milestones:
            flags.append(
                Flag("removed_milestone", SECTION_MILESTONES, f"{m['name']} ({m['target_date']})")
            )

    shown_risks = {r["risk_id"] for r in values["risks"]}
    current_risks = {r["risk_id"]: r for r in facts.shown_risks}
    for r in values["risks"]:
        status, ticked = facts.risk_states.get(r["risk_id"], ("closed", False))
        current = current_risks.get(r["risk_id"])
        if status in GONE_STATUSES:
            flags.append(Flag("closed_risk", SECTION_RISKS, f"{r['title']} ({status})"))
        elif not ticked:
            flags.append(
                Flag("risk_changed", SECTION_RISKS, f"{r['title']} (no longer ticked to show)")
            )
        elif current is not None and any(
            r.get(field) != current[field] for field in RISK_SHOWN_FIELDS
        ):
            flags.append(
                Flag(
                    "risk_changed",
                    SECTION_RISKS,
                    f"{r['title']} (title or response changed in the risk register)",
                )
            )
    for r in facts.shown_risks:
        if r["risk_id"] not in shown_risks:
            flags.append(
                Flag("risk_changed", SECTION_RISKS, f"{r['title']} (ticked to show, not yet shown)")
            )
    return flags


def _source(proposal: ProjectProposal, name: str) -> dict[str, Any]:
    return (proposal.section_sources or {}).get(name) or {}


def is_unreviewed(proposal: ProjectProposal, name: str) -> bool:
    """Whether the section is an agent's draft that no person has edited or confirmed."""
    entry = _source(proposal, name)
    return entry.get("source") == "agent" and entry.get("reviewed_by") is None


def unreviewed_sections(proposal: ProjectProposal) -> list[str]:
    return [
        name
        for name in TEXT_SECTIONS
        if (getattr(proposal, name) or "").strip() and is_unreviewed(proposal, name)
    ]


def may_replace(proposal: ProjectProposal, name: str) -> bool:
    """Whether the proposal agent may write the section: it is empty or an unreviewed draft."""
    return not (getattr(proposal, name) or "").strip() or is_unreviewed(proposal, name)


def mark_section(
    proposal: ProjectProposal, name: str, *, source: str, reviewed_by: int, reviewed_at: str
) -> None:
    """Record who wrote, or who reviewed, a section. The dict is replaced, so it is saved."""
    sources = dict(proposal.section_sources or {})
    sources[name] = {"source": source, "reviewed_by": reviewed_by, "reviewed_at": reviewed_at}
    proposal.section_sources = sources


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
