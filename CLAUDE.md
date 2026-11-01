# CLAUDE.md — PragMattie PMO

Instructions for every agent that works in this repository. Read all of it before you start.
If this file and an issue disagree, stop and say so in your summary rather than guessing.

## What this repository is

- **PragMattie PMO** is a *fictional* portfolio-orchestration product for a consulting practice:
  project and work-item intake, a risk register, stage gates, status reports, a client proposal
  workflow, a portfolio dashboard and an agent registry. The company, its clients, people and all
  seed data are invented.
- It is built by AI agents under the same rules as the earlier PragMattie Sync projects: a person
  approves every spec, every agent run is audited, and a person decides what merges. It is the
  broader-project companion to the delivery work shown there.
- It is a client demonstration for **PragMattie Growth Partners, LLC**. The code is public but
  **all rights reserved**: there is no licence granting reuse, and you must not add one.
- Content follows three general practice (general project practice, AI governance practice, and AI project practice).
  Their licences are personal-use only. **Paraphrase, and cite section numbers; never quote more
  than a few words, and never paste passages, tables or figures from them.**
- **Use the published terms and rules wherever possible.** Check a term against its source before using
  it. If we depart from a source, say so and why. published sets no numeric cut-offs, so label ours as
  our own choice.
- **You cannot read general practice.** Use only the names and section numbers that an issue gives you
  or that are already in this repository. Never cite one from memory; if you need one you don't
  have, say so in your summary.
- **Follow `policies/project-rules.md`.** It holds our own rules, informed by those sources. Follow
  every rule that applies to your change. If you depart from one, say which and why in your
  summary; the reviewer checks changes against it. the published names may appear only to identify its
  publications and concepts: never suggest that published endorses this product.
- **Standards map.** `docs/standards-map.yaml` tags every feature to general practice and is
  served at `GET /api/v1/standards`. The no-quoting rule applies there in full: paraphrase, cite a
  section number, and never paste wording, tables or figures. Each entry's prose (title, notes and
  AI-standard name) is capped at 200 characters, and a test in
  `apps/api/tests/test_standards.py` rejects longer entries and unknown values. When you build a
  feature, add or update its entry under its milestone (`P<n>-<slug>`).

## How you work

1. **Build exactly what the issue asks.** Its spec and acceptance criteria are the scope. Anything
   else you notice goes in your summary as a follow-up, never into the change.
2. **Clean room.** Work from the issue's spec and this file. Do not fetch or copy code from any
   other PragMattie repository; the behaviour you need is described in the issue.
3. **Tests are part of the change.** Add or update tests for what you build, and run the relevant
   suite before you finish. Report honestly whether it passes.
4. **Commit as you go** on the branch you were given, with Conventional Commits messages. Don't push
   and don't open the pull request: the workflow does that.
5. **Never edit** `.github/workflows/`, `policies/`, `CLAUDE.md` or anything that holds secrets. If a
   task needs a workflow or a policy file, write it to `ci/proposed/` and say so: a person applies it.
6. **Never add** credentials, tokens, keys, passwords or `.pem` files to any file, never print
   environment variables, and never log a secret.
7. **Keep changes small and readable.** Match the surrounding code; no speculative abstractions.

## Stack (settled; do not swap)

| Layer | Choice |
| --- | --- |
| API | Python 3.12, FastAPI, SQLAlchemy 2, Alembic, Pydantic v2 (`apps/api`, port 8000) |
| Database | **MySQL 8.4, not Postgres.** Docker service `db` locally (host port **3307**); the hosted database is Aiven for MySQL, reached over TLS. Tests use a throwaway SQLite file. |
| Web | **Vue 3, not React**: Vite, **Vuetify** (MIT), Pinia, Vue Router, Chart.js via vue-chartjs (`apps/web`, port 5173) |
| Tests and lint | pytest + ruff (line length 100, rules `E,F,I,B,UP`, target py312); Vitest for web |
| Local run | Docker Compose |
| Agents | Python + the Claude API; every call uses a strict JSON schema, and limits are enforced in code, never trusted from the model |

## Conventions

- **Names:** database `pragmattie_pmo`. Display name "PragMattie PMO". Vuetify theme
  `pragmattiePmo`: navy `#2E3F55`, teal `#1B8A94`, gold `#E8B35A`. The footer credits
  PragMattie Growth Partners, LLC.
- **Migrations:** `NNNN_short_name.py` with a matching `revision` id. On MySQL, downgrades drop
  tables rather than individual indexes (foreign-key indexes can't be dropped first).
- **Audit:** every agent and human decision is one append-only row in `pmo_audit`. Never update or
  delete one: a correction is a new row that supersedes it. `trigger` is a MySQL reserved word:
  quote it in hand-written SQL.
- **Money** is a decimal, and a JSON string with 2 decimal places.
- **Seed data** uses the reserved `.example` domain, and never real people or companies. Demo
  passwords are generated at seed time, printed once and never stored or committed.
- **Settings** live in one place (`app/config.py`); secrets are `Field(repr=False)`.
- **Lists** are paged with a stable order.
- **Commits:** Conventional Commits, `type(scope): description`, e.g.
  `feat(api): add project intake endpoint`. No attribution lines in commits or pull requests.

## Security rules (login and roles arrive in milestone P2)

- Every API route is guarded by an explicit permission, except `health`, `auth/login` and
  `auth/logout`, which stays open so signing out is safe to repeat. FastAPI's docs routes are also
  open, and are served only when `APP_ENV` is dev or test. A test fails the build for an unguarded
  route.
- Passwords are hashed with argon2id; plaintext never appears in logs, responses or audit rows.
- Sessions are an httpOnly cookie plus a CSRF header on unsafe methods. Error messages for login are
  generic.
- No role can approve its own submission. The API enforces permissions; the web only hides things.
- AI output is a proposal: an agent never sends a document outside the system, approves a gate,
  accepts a risk, commits a price, or authorises a project. A person decides, and that is recorded.

## Governance you are working under

- Until a governance layer sits over this repository, **every pull request needs the repository
  owner's approval**, whatever its size. Each issue's `forecast:Tn` label says how careful to be:
  T2 and T3 issues begin with a plan that a person approves before you build.
- Every run you make is recorded with its model, turns, tokens and cost, and stops for the day when
  the daily cap is reached.
- A reviewer agent reads every agent pull request against its issue's spec and this file and posts
  one advisory comment ("would approve" or "would request changes"). It never merges or approves.
- When a person merges your pull request, the next open item in the same milestone starts by
  itself if its spec is approved (no `spec-draft` label). The next milestone never starts without a
  person.
- A miss by an agent is a prompt fix, never a lower bar.

## Human-written files

This file, `.github/` and the `policies/` files are written or applied by people (the project's
PM with Claude) before any agent runs on them. Everything else is agent-built.
