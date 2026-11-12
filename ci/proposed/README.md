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

- **`policies/reporting.yaml`** (item 7.3), drafted at `ci/proposed/policies/reporting.yaml`: the
  limits that rate a status report green, amber or red. Schedule: amber when a milestone is more
  than 0 working days late against its baseline target, red above 10. Cost: spending ahead of
  time (spent % of the baseline less the share of delivery time passed), amber above 5 points,
  red above 15. Scope: the baseline's move from baseline 1, either way, amber above 5%, red
  above 10%. Risk: amber at 1 open Medium or High risk, red at 2 open High risks. Until it is
  applied, creating or refreshing a status report answers 503 (reading still works). Tests use
  a fixture copy in `apps/api/tests/fixtures/policies/reporting/`, so applying it changes no test.

- **`policies/autonomy.yaml`** (item 7.4), drafted at `ci/proposed/policies/autonomy.yaml`: the
  applied file plus two steps and one agent switch. `draft_status_report` is `agent`
  (`status_report_agent`, who `project_manager`): once a report is due and its period has no
  report, the orchestrator creates the period's draft and the agent drafts its text, once per
  period. `publish_status_report` is `person` (`project_manager`, who must hold
  `report:publish`): the reviewed draft goes to the project manager's inbox. Publishing is now a
  floor step, enforced in code. `agents.status_report_agent.enabled: true`. The loader requires
  every step exactly once, so **after this item merges the live file is refused until this one
  is applied**: the orchestrator stops and `GET /api/v1/autonomy` answers 503. Apply it in the
  same sitting as the merge, as with earlier policy files, so the gap is minutes and nothing
  hosted runs in between. Tests read this draft while it is pending.

## Applied

- **`policies/autonomy.yaml`** (item 6.8): one line added, `answer: accepted` under
  `agents.simulated_client`: what the simulated client answers a sent proposal (`accepted`, or
  `declined` for a demo). The loader refuses any other value and used `accepted` while the key
  was missing, so behaviour is unchanged; the choice is now visible in the policy.
- **`policies/autonomy.yaml`** (item 6.1): for each of the 15 steps in a project's life, whether
  an agent does it (`agent`), an agent does it after a 15-minute window the project manager may
  hold (`window`), or a person does it (`person`), plus an off switch per agent. Starting values:
  drafting steps and the simulated client's answer `agent`; `score_risk`, `open_low_risk` and
  `submit_proposal` `window`; every other step `person`. The five person-only steps are enforced
  in code, and each step's `who` must hold the permission its decision needs. `accept_risk` names
  `project_manager`, but a risk's band still decides who may accept it, so a Medium or High risk
  goes to the sponsor. No agent may decide a change (`decide_change_pm`) until a later item adds
  one. Applied with its header corrected (the loader is in `app/policy.py`); `GET /api/v1/autonomy`
  now answers instead of returning 503.
- **`policies/changes.yaml`** (item 5.5): who decides a change request, by its size against the
  current baseline. The project manager decides a change that adds at most 5% to the baseline
  total, moves no milestone later by more than 5 working days and changes no scope; everything
  else goes to `sponsor`. Applied with one comment corrected (the percentage is compared
  unrounded, as the code does). The change request routes now answer instead of returning 503.

- **`policies/gates.yaml`** (item 5.4): adds the go-ahead gate (planning to delivery, for
  projects, decided by `sponsor`) with its six automatic checks and two person criteria; the
  approval gate is unchanged. `GET` and `POST /api/v1/projects/{id}/gate` now answer instead of
  returning 503, and the tests load the applied file.
- **`policies/gates.yaml`** (item 4.4): the approval gate (intake to planning, for project
  requests, decided by `sponsor`) with its four automatic checks and two person criteria, moved
  over the live placeholder unchanged. `GET` and `POST /api/v1/requests/{id}/gate` now answer
  instead of returning 503, and the tests load the applied file.
- **`policies/risk-library.yaml`** (item 3.2): the starter risk library, moved from
  `ci/proposed/policies/risk-library.yaml` unchanged. The library routes now answer instead of
  returning 503.
- **`ci.yml`** (item 2.4): applied over `.github/workflows/ci.yml` and the copy here deleted. It
  adds `API (security tests)`, a required check in the `main` ruleset. `API (migrations on MySQL)`
  keeps its name and now also runs `pytest -m mysql --require-mysql` (the last-admin and
  parallel-login-failure races, on its throwaway MySQL service, via `TEST_MYSQL_URL`); a skipped
  MySQL test fails that step. `Dependency audit` (`pip-audit`, `npm audit --omit=dev`) is
  report-only, so it is **not** a required check yet. The workflow still needs no secrets.
- **`CLAUDE.md` standards note** (item 1.6): the standards map bullet was added to `CLAUDE.md`.
- **`policies/`** (item 1.4): the first real `tiers.yaml`, `risk.yaml`, `gates.yaml` and
  `approvers.yaml`, moved from `ci/proposed/policies/`. `apps/api/app/policy.py` validates them
  strictly and has no defaults in code, so a bad or missing file raises `PolicyError`. To change a
  policy, an agent writes the new version here and a person applies it over the existing file.
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
