# ADR 0001: Stack and repository layout

- **Status:** accepted
- **Date:** 2026-10-30
- **Item:** 1.1 Repository skeleton and CI

## Context

PragMattie PMO needs a runnable monorepo before any domain work starts. The stack is settled in
`CLAUDE.md`; this record writes down the choices and the layout so later items don't revisit them.

## Decision

- **API** in `apps/api`: Python 3.12, FastAPI, SQLAlchemy 2, Alembic and Pydantic v2 (settings
  through pydantic-settings in `app/config.py`). Routes live under `/api/v1`. Migrations are named
  `NNNN_short_name.py` with a matching revision id; `0001_baseline` is empty.
- **Database:** MySQL 8.4, not Postgres. Locally the Docker service `db` on host port 3307; hosted
  on Aiven for MySQL over TLS. Unit tests use a throwaway SQLite file.
- **Web** in `apps/web`: Vue 3, Vite, Vuetify (theme `pragmattiePmo`), Pinia and Vue Router; Vitest
  with jsdom for tests. Chart.js via vue-chartjs arrives with the first chart.
- **Quality gates:** ruff (line length 100, rules `E,F,I,B,UP`, py312), pytest and Vitest. CI has
  three jobs whose names are the required checks: `API (lint + tests)`, `API (migrations on MySQL)`
  and `Web (tests + build)`.
- **Layout:** `apps/`, `docs/adr/`, `scripts/` and `ci/proposed/`. Agents cannot write workflows or
  policies, so they propose them under `ci/proposed/` and a person applies them. `policies/`
  itself is created by a person when the first policy files are applied.
- **Local run:** `docker compose up` with credentials from `.env` (see `.env.example`).

## Consequences

- One Python and one Node toolchain; each app pins its own dependencies.
- CI starts only once a person applies `ci/proposed/ci.yml` and sets the required checks.
