# PragMattie PMO

A fictional portfolio-orchestration product for a consulting practice, built by AI agents under
human approval. A client demonstration for PragMattie Growth Partners, LLC. All rights reserved.

## Layout

| Path | What |
| --- | --- |
| `apps/api` | FastAPI API (Python 3.12), port 8000 |
| `apps/web` | Vue 3 + Vuetify web app, port 5173 |
| `docs/adr` | Architecture decision records |
| `ci/proposed` | Workflow and policy proposals for a person to apply |
| `scripts` | Helper scripts |

## Run locally

```sh
cp .env.example .env   # then set your own passwords
docker compose up --build
```

The web app is at http://localhost:5173, the API health check at
http://localhost:8000/api/v1/health and MySQL on host port 3307.

Load the invented demo portfolio (every name and address is fictional, on `.example`):

```sh
docker compose exec api python -m app.seed           # idempotent
docker compose exec api python -m app.seed --reset   # clear seeded rows only, then seed again
```

## Demo users for a rehearsal

`python -m app.seed users` creates one demo user per role, each on a `.example` address, with a
new random password. The passwords exist only in that run: the database keeps argon2id hashes,
the audit trail records that the users were seeded (roles and a count, no passwords), and nothing
is logged. No password is ever written in this repository, including here.

Each run also puts every demo user back as seeded: role, display name, active, and no lockout.
To rotate the passwords, run it again; the old ones stop working at once (sessions already signed
in are not ended, see the known gaps in `docs/security.md`).

**Write them to a git-ignored file** (from `apps/api` on the host, with the API settings from your
`.env` in the environment; the `DB_*` parts point at the compose database on port 3307):

```sh
python -m app.seed users --password-file ../../demo-passwords.txt
```

The terminal shows only how many users were seeded and where the file is. Before touching the
database, the command checks with `git check-ignore` that the path is ignored (any
`*password*.txt` is) and not tracked, and refuses otherwise, or when git is missing or can't say.
The file is created readable by you only (mode 0600) and is replaced on each run. On Windows that
mode is ignored: there the protection is the git-ignore rule and your own user profile. Delete
the file after the rehearsal.

**Or print them once to the terminal** (the API container has no git and no copy of the
repository, so `--password-file` is refused there):

```sh
docker compose exec api python -m app.seed users
```

Copy them somewhere safe; they are not shown again. When the `CI` environment variable is set,
printing is refused, because CI logs in this repository are public.

## Checks

```sh
cd apps/api && pip install -r requirements-dev.txt && ruff check . && ruff format --check . && pytest -q
cd apps/web && npm ci && npm test && npm run build
```
Built by agents under PragMattie governance.
