# ci/proposed

Agents cannot edit `.github/workflows/` or `policies/`. When a task needs a new or changed
workflow or policy file, the agent writes it here instead and says so in its summary; a person
reviews it and applies it by hand.

Nothing in this folder runs automatically.

## How to apply a proposal

1. Read the file and the summary that came with it.
2. Copy it to its real place (`.github/workflows/ci.yml`, `policies/tiers.yaml`, ...) on a branch
   named `non-phase-work-<topic>`, and delete the copy from this folder in the same change.
3. Open a pull request and merge it with a merge commit.
4. If a CI job was added or renamed, update the `main` ruleset's required checks in the same
   sitting: they are matched by job name, and a pull request would otherwise wait on a check that
   never reports.

## Pending

- **`policies/`** (arrives with item 1.4): the first real `tiers.yaml`, `risk.yaml`,
  `gates.yaml` and `approvers.yaml`. `ci/proposed/policies/` holds them until a person moves them
  to `policies/` at the repository root (the folder exists, with a `.gitkeep`).

## Applied

- **`ci.yml`** (item 1.1): applied as `.github/workflows/ci.yml` and the copy here deleted. Its
  three jobs are `API (lint + tests)`, `API (migrations on MySQL)` and `Web (tests + build)`, and
  are required checks in the `main` ruleset. The MySQL job uses a throwaway `mysql:8.4` service
  container with an empty root password, so the workflow needs no secrets. To change CI, an agent
  writes the new version here and a person applies it over the existing file.
- **`ci.yml`** (item 1.2): applied over `.github/workflows/ci.yml` as a commit on the item's own
  branch, so its pull request was tested against the CI it was built for (a separate pull request
  would have failed: `main` had no `app.dbcheck` yet). Settings lost their `PMO_` prefix; the
  MySQL job sets `APP_ENV=test` (no JWT secret needed) and adds `python -m app.dbcheck` and
  `alembic check`. Job names are unchanged, so the required checks needed no update.
- Bootstrap (by hand, with Claude): `implement.yml`, `review.yml`, the issue template and
  `CLAUDE.md`, ported from the earlier agentic rebuild with its hardcoded values turned into
  repository variables. They replace the workflow part of item 1.7.

## Repository variables the workflows read

All optional; defaults in brackets. Change them in Settings, Secrets and variables, Actions,
Variables.

| Variable | Meaning |
| --- | --- |
| `DAILY_CAP_USD` [10] | Runs and reviews stop for the day when recorded cost reaches this |
| `IMPLEMENT_MODEL` [claude-opus-5-5] | Model for builds |
| `SONNET_MODEL` [claude-sonnet-5] | Model when an issue has the `model:sonnet` label |
| `REVIEW_MODEL` [claude-opus-5-5] | Model for reviews |
| `PLAN_TIERS` [T2 T3] | Forecast tiers that get a plan first (`/approve-plan`) |
| `TURNS_T0` .. `TURNS_T3` [20 40 60 80] | Turn limits by tier |
| `TURNS_PER_POINT` [15], `TURNS_MAX` [150], `PLAN_TURNS` [25] | Turn rules |

## First run (dry-run checklist)

1. Make sure the labels and the `P1 Foundation` milestone exist, and that the app is installed
   on this repository with its secrets set.
2. Create a throwaway issue titled `0.1 Dry run` with a one-line spec ("add a line to the
   README"), label it `forecast:T1`, `points:1` and `agent-ready`.
3. Watch Actions: the implementer runs, opens a pull request, and the reviewer comments.
4. Close the pull request and the issue unmerged.
