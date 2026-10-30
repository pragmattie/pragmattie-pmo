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

## Checks

```sh
cd apps/api && pip install -r requirements-dev.txt && ruff check . && ruff format --check . && pytest -q
cd apps/web && npm ci && npm test && npm run build
```
