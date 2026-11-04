"""Demo data: `python -m app.seed [--reset]` and `python -m app.seed users [--password-file PATH]`.

Creates three invented client companies, a project for each, and "PragMattie Sync" as an internal
project (no client), and gives each project a few scored risks, AI and ordinary, so every priority
band appears. Every person, company, email address and website is made up and uses the
reserved `.example` domain.

Seeding is deterministic and idempotent: each seeded row has a fixed `seed_key`, and running the
seed again sets those rows back to the values below without adding duplicates. `--reset` first
clears the seeded rows (and only those), then seeds afresh. Rows people created are never touched.
Each seeded risk gets one audit row when it is created or set back, none when it is unchanged; its
score and band come from the risk policy, as for any risk.

`users` creates or restores one demo user per role, on `.example` addresses. Every run gives each
of them a new random password, sets the role, display name and active flag back to their seed
values and clears any lockout. The passwords exist only in memory: the database holds argon2id
hashes, the audit row holds the roles and a count, and nothing is logged. After the commit they
are printed once to the terminal, or with `--password-file` written to a git-ignored file only.
Because public CI logs would show them, printing is refused when `CI` is set. Every refusal
happens before the database is touched, so a refused run rotates nothing.
"""

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, exists, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.audit import record_decision
from app.authz import Role
from app.models import (
    Client,
    PlanDeliverable,
    PlanEstimateLine,
    PlanMilestone,
    Project,
    ProjectPlan,
    Risk,
    User,
)
from app.policy import PolicyError, RiskPolicy
from app.risks import apply_score, record_risk_change, risk_policy, snapshot
from app.security import hash_password

SEEDED_AT = datetime(2026, 10, 1, 9, 0, 0)

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

# `client` names a client's seed_key; None marks an internal project.
PROJECTS: tuple[dict[str, Any], ...] = (
    {
        "seed_key": "project-quillmere-routing",
        "client": "client-quillmere",
        "name": "Depot Routing Modernisation",
        "stage": "delivery",
        "sponsor": "Odessa Brightwater",
        "sponsor_email": "odessa.brightwater@quillmere-freight.example",
        "pm": "Casimir Duncombe",
        "pm_email": "casimir.duncombe@pragmattie.example",
        "status": "at_risk",
    },
    {
        "seed_key": "project-brackenfold-intake",
        "client": "client-brackenfold",
        "name": "Patient Intake Redesign",
        "stage": "planning",
        "sponsor": "Ignatius Fernhollow",
        "sponsor_email": "ignatius.fernhollow@brackenfold-health.example",
        "pm": "Marisol Thackeray",
        "pm_email": "marisol.thackeray@pragmattie.example",
        "status": "on_track",
    },
    {
        "seed_key": "project-larchwick-onboarding",
        "client": "client-larchwick",
        "name": "Member Onboarding Portal",
        "stage": "intake",
        "sponsor": "Perpetua Hollingmoor",
        "sponsor_email": "perpetua.hollingmoor@larchwick-mutual.example",
        "pm": "Casimir Duncombe",
        "pm_email": "casimir.duncombe@pragmattie.example",
        "status": "on_track",
    },
    {
        "seed_key": "project-pragmattie-sync",
        "client": None,
        "name": "PragMattie Sync",
        "stage": "delivery",
        "sponsor": "Rosalind Ashcombe",
        "sponsor_email": "rosalind.ashcombe@pragmattie.example",
        "pm": "Marisol Thackeray",
        "pm_email": "marisol.thackeray@pragmattie.example",
        "status": "on_track",
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


@dataclass
class SeedResult:
    clients: int
    projects: int
    risks: int
    cleared_clients: int = 0
    cleared_projects: int = 0


def _upsert[T: (Client, Project)](db: Session, model: type[T], values: dict[str, Any]) -> T:
    row = db.scalar(select(model).where(model.seed_key == values["seed_key"]))
    if row is None:
        row = model()
        db.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    row.created_at = SEEDED_AT
    return row


def clear_seeded(db: Session) -> tuple[int, int]:
    """Delete seeded risks, then seeded projects that no person's risk is on, then seeded clients
    that no remaining project uses. A seeded project or client that a person's row refers to is
    kept (it is reset to its seed values on the next seed). A deleted project's plan (which the
    plans migration gave every project) goes with it. Their audit rows stay. Returns
    (clients, projects) deleted. The caller commits."""
    db.execute(delete(Risk).where(Risk.seed_key.is_not(None)))
    has_risk = exists().where(Risk.project_id == Project.id)
    doomed = select(Project.id).where(Project.seed_key.is_not(None), ~has_risk)
    # Read the ids first: MySQL can't delete from a table that its own subquery reads.
    plans = list(db.scalars(select(ProjectPlan.id).where(ProjectPlan.project_id.in_(doomed))))
    if plans:
        for model in (PlanDeliverable, PlanEstimateLine, PlanMilestone):
            db.execute(delete(model).where(model.plan_id.in_(plans)))
        db.execute(delete(ProjectPlan).where(ProjectPlan.id.in_(plans)))
    projects = db.execute(delete(Project).where(Project.seed_key.is_not(None), ~has_risk)).rowcount
    in_use = exists().where(Project.client_id == Client.id)
    clients = db.execute(delete(Client).where(Client.seed_key.is_not(None), ~in_use)).rowcount
    return clients, projects


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
    risk.owner_user_id = None
    risk.created_at = SEEDED_AT
    apply_score(risk, policy)
    db.flush()
    if before is None:
        record_risk_change(db, risk, action="create", before=None, actor_user_id=None)
    elif snapshot(risk) != before:
        record_risk_change(db, risk, action="seed", before=before, actor_user_id=None)


def seed(db: Session, *, reset: bool = False, policy: RiskPolicy | None = None) -> SeedResult:
    """Create or restore the demo rows. Risks are scored from `policy`, by default the risk
    policy (a `PolicyError` if it can't be loaded, before anything is written). The caller
    commits."""
    policy = policy or risk_policy()
    cleared = clear_seeded(db) if reset else (0, 0)
    clients = {values["seed_key"]: _upsert(db, Client, values) for values in CLIENTS}
    db.flush()
    projects = {}
    for values in PROJECTS:
        project = dict(values)
        client_key = project.pop("client")
        project["client_id"] = clients[client_key].id if client_key else None
        projects[project["seed_key"]] = _upsert(db, Project, project)
    db.flush()
    for values in RISKS:
        risk = dict(values)
        _seed_risk(db, policy, risk, projects[risk.pop("project")].id)
    return SeedResult(len(CLIENTS), len(PROJECTS), len(RISKS), *cleared)


# One demo user per role. `demo_users()` derives the rest, so a new role gets a user too.
DEMO_DOMAIN = "pragmattie.example"
AUDIT_AGENT = "seed"
AUDIT_SUBJECT = "demo_users"
PASSWORD_BYTES = 18  # token_urlsafe(18) gives 24 characters, about 144 bits.


def demo_users() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "role": role.value,
            "email": f"demo.{role.value.replace('_', '.')}@{DEMO_DOMAIN}",
            "display_name": f"Demo {role.value.replace('_', ' ').title()}",
        }
        for role in Role
    )


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
        "--reset", action="store_true", help="clear seeded rows (only those) before seeding"
    )
    commands = parser.add_subparsers(dest="command")
    users = commands.add_parser(
        "users", help="create or restore one demo user per role, with new random passwords"
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
        print(f"Seed failed: the risk policy could not be loaded: {exc}", file=sys.stderr)
        return 1
    if args.reset:
        print(f"Cleared {result.cleared_clients} clients and {result.cleared_projects} projects.")
    print(f"Seeded {result.clients} clients, {result.projects} projects and {result.risks} risks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
