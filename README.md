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

Each seeded project gets a few scored risks, AI and ordinary, covering every priority band. The
seed scores them from `policies/risk.yaml` and records one audit row for each risk it creates or
sets back. `--reset` keeps a seeded project that holds a risk a person added.

## The starter risk library

`policies/risk-library.yaml` holds starter risks, AI and ordinary, so a new project's register
never starts empty. `GET /api/v1/risk-library` lists them, and
`POST /api/v1/projects/{id}/risks/from-library` with `{"template_ids": [...]}` adds the chosen
ones to a project as `proposed` risks with no score, for the project's people to score. A
template has no probability or impact, because those depend on the project.

## The risk register screen

**Risks** in the menu (any role with `project:read`) opens a project's risk register: a paged
table with filters for status, band, category and kind, a 5 x 5 heat map of open risks coloured
by band (click a cell to filter the table to it; threats and opportunities can be shown on their
own), open totals per band, and a drawer with every field and the risk's history. Only a role
with `project:write` (the project manager) sees the buttons to add, edit and close a risk. If
someone else saved the risk first, the screen says so and shows the latest version.

It reads three routes, each needing only `project:read`:
`GET /api/v1/projects/{id}/risks/heatmap` (optionally `?kind=threat` or `?kind=opportunity`),
`GET /api/v1/projects/{id}/risks/{risk_id}/history` (the risk's own audit rows, newest first)
and `GET /api/v1/users` (active people's ids and names, to show and pick an owner). The risk
list also takes `probability` and `impact` filters, for a heat map cell. Band colours are
`band-low`, `band-medium` and `band-high` in the `pragmattiePmo` theme.

## Escalation and acceptance

Each priority band in `policies/risk.yaml` names its escalation owner (Low: project manager;
Medium and High: sponsor). When an open risk enters a band whose owner differs from before, it is
escalated to that role, with one audit row naming the band, the role and the risk. It stays
escalated until it leaves `open` or a re-score moves it to another owner. While it is escalated,
only a role at or above the owner may close it or re-score it into a lower band (losing its score
counts as lower); anyone else gets a 403, recorded as a refused decision, and nothing changes.
Raising the score and other edits are still allowed. In practice the owner accepting ends it.
`GET /api/v1/risks/escalations` lists the open escalations for the caller's role, across projects.

- `POST /api/v1/projects/{id}/risks/{risk_id}/accept` (`risk:accept`) accepts an open, scored
  risk. The caller's role must be at or above the band's owner (a sponsor accepts any band, a
  project manager only Low), with a reason of at least 20 characters and a review date. A refusal
  is a 403 and is audited too. Re-scoring an accepted risk into a higher band reopens it.
- `POST .../{risk_id}/open` and `POST .../{risk_id}/reject` (`project:write`) decide on a
  proposed risk; rejecting needs a reason.
- The general `PATCH` only closes an open or accepted risk, or reopens a closed one. A rejected
  risk is final: any `PATCH` to it is a 409.

These routes need a signed-in person, and each decision is an audit row naming them. On the Risks
page, Accept, Open and Reject show only to roles that may use them, and a panel lists what is
escalated to the signed-in role. Close risk is hidden from a person who can't clear a risk's
escalation, who sees who can instead, and a rejected risk has no Edit button.

## The risk agent

`POST /api/v1/projects/{id}/risks/suggest` (`project:write`) asks Claude for up to five new risks
for the project; the **Suggest risks** button on the Risks page runs it. `AGENT_MODE` gates it:
`off` refuses, `shadow` returns the proposals and saves nothing, `enforce` saves them as
`proposed`, unscored risks marked **Agent proposal**, for a person to open or reject.
`?dry_run=true` returns the exact request and a cost ceiling without calling anything. Each run is
one audit row. Its limits, model and caps are in [docs/agents.md](docs/agents.md).

## Project requests and the Intake queue

**Intake** in the menu (any role with `project:read`) shows the queue of project requests: a paged
table (title, client, status, AI, wanted by, submitted) with filters for status, client and AI,
sorted by submitted date or wanted-by date (requests without that date come last either way).
A request with no client is internal work. Only a role with `project:write` sees **New request**,
and on a request's page, Edit and Submit (while it is a draft) and Withdraw (draft or submitted).

- `GET /api/v1/requests`, `GET /api/v1/requests/{id}` and `GET .../{id}/history` (the request's
  own audit rows, newest first, with the actor's display name) need `project:read`.
- `POST /api/v1/requests`, `PATCH .../{id}`, `POST .../{id}/submit` and `POST .../{id}/withdraw`
  need `project:write`. Any project manager may act on any request; the audit row names who did.
  `requested_by` is the signed-in person and can't be sent. Only a draft can be edited.
- Every change sends the `version` it read; a stale one is a 409. `declined` and `withdrawn` are
  final. No route here approves, declines or returns a request to draft: the approval gate does.
- The title is at most 200 characters; what is asked for, the problem, the outcome and known risks
  at most 2,000 each. Budgets are 2-decimal strings, and the low end can't be above the high end.

### The value case

Each request has one value case: why it is worth doing, how we will know, and what it will cost.
It shows on the request's page; a role with `project:write` can add or edit it while the request
is a draft.

- **Benefits** (1 to 10): a description, how it is measured, the current value, the target and the
  date the target should be met by. Only the description is needed to save one.
- **Estimated cost**: low and high (2-decimal strings, low no more than high) and how it was
  estimated. This is our estimate; the request's **Budget available** is what the client has in
  mind. The two are not linked or checked against each other.
- **Strategic fit** (the client or practice goal it serves) and the **cost of not doing it**
  (optional).
- `GET /api/v1/requests/{id}/value-case` needs `project:read` (404 until there is one);
  `PUT .../{id}/value-case` needs `project:write` and replaces the whole value case. It carries the
  request's `version`, bumps it, and is a 409 if the version is stale or the request isn't a draft.
- Saving a value case as a person confirms it (`confirmed_by`, `confirmed_at`). One an agent
  drafted keeps `source` "agent" and must be saved by a person, changed or not, before the request
  can be submitted. Each change is one audit row (action `value_case`) in the request's history,
  with the values before and after; a save that changes nothing on a value case a person has
  already confirmed writes nothing.
- A request can be submitted only when its value case has at least one benefit with a measure and
  a target, a full cost estimate, and a person's confirmation. Otherwise submitting is a 409 whose
  `detail` is `{message, missing: [...]}`, and the page lists what is missing.

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

## After the risk policy changes

Each risk stores the score and band the server worked out from `policies/risk.yaml` when the risk
was last saved. After that file changes, run the rescore command so every stored score and band
matches the new policy. Each risk whose score or band changes gets one audit row:

```sh
docker compose exec api python -m app.risks rescore
```

## Checks

```sh
cd apps/api && pip install -r requirements-dev.txt && ruff check . && ruff format --check . && pytest -q
cd apps/web && npm ci && npm test && npm run build
```
