"""Demo data: `python -m app.seed [--reset]`.

Creates three invented client companies, a project for each, and "PragMattie Sync" as an internal
project (no client). Every person, company, email address and website is made up and uses the
reserved `.example` domain.

Seeding is deterministic and idempotent: each seeded row has a fixed `seed_key`, and running the
seed again sets those rows back to the values below without adding duplicates. `--reset` first
clears the seeded rows (and only those), then seeds afresh. Rows people created are never touched.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, exists, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Client, Project

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
        "stage": "executing",
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
        "stage": "initiating",
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
        "stage": "executing",
        "sponsor": "Rosalind Ashcombe",
        "sponsor_email": "rosalind.ashcombe@pragmattie.example",
        "pm": "Marisol Thackeray",
        "pm_email": "marisol.thackeray@pragmattie.example",
        "status": "on_track",
    },
)


@dataclass
class SeedResult:
    clients: int
    projects: int
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
    """Delete seeded projects, then seeded clients that no remaining project uses. A seeded client
    that a person's project refers to is kept (it is reset to its seed values on the next seed).
    Returns (clients, projects) deleted. The caller commits."""
    projects = db.execute(delete(Project).where(Project.seed_key.is_not(None))).rowcount
    in_use = exists().where(Project.client_id == Client.id)
    clients = db.execute(delete(Client).where(Client.seed_key.is_not(None), ~in_use)).rowcount
    return clients, projects


def seed(db: Session, *, reset: bool = False) -> SeedResult:
    """Create or restore the demo rows. The caller commits."""
    cleared = clear_seeded(db) if reset else (0, 0)
    clients = {values["seed_key"]: _upsert(db, Client, values) for values in CLIENTS}
    db.flush()
    for values in PROJECTS:
        project = dict(values)
        client_key = project.pop("client")
        project["client_id"] = clients[client_key].id if client_key else None
        _upsert(db, Project, project)
    db.flush()
    return SeedResult(len(CLIENTS), len(PROJECTS), *cleared)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reset", action="store_true", help="clear seeded rows (only those) before seeding"
    )
    args = parser.parse_args(argv)

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
    if args.reset:
        print(f"Cleared {result.cleared_clients} clients and {result.cleared_projects} projects.")
    print(f"Seeded {result.clients} clients and {result.projects} projects.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
