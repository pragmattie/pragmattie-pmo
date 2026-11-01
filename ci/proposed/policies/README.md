# Proposed policies (item 1.4)

First drafts of the governance files that `apps/api/app/policy.py` loads. Agents may not edit
`policies/`, so they wait here until a person reviews and applies them. Everything in them is
drafted in our own words; nothing is copied from general practice.

| File | Holds |
| --- | --- |
| `tiers.yaml` | Tiers T0 to T3 with approvals per tier, the tiers that may be auto-approved (none yet; T2 and T3 never), and `fallback_tier: T2`, used when a risk assessment fails |
| `risk.yaml` | Probability and impact scales 1 to 5 with a definition per level (impact against schedule, cost and quality, for threats and opportunities); allowed variation per objective (schedule and cost +/-10%, quality no loss of key functions); priority bands Low 1-6 (project manager), Medium 7-12 (sponsor), High 13-25 (sponsor); and the rule that impact 5 is at least Medium |
| `gates.yaml` | Placeholder (an empty `gates` list) until stage gates arrive in P5 |
| `approvers.yaml` | Roles that approve and receive escalations: role names and `.example` mailboxes, no people |

## Decisions for the reviewer

- Probability percentages and impact percentages per level are proposals; check that the
  impact-4 schedule and cost definitions line up with the 10% allowed variation.
- `tiers.yaml` asks for 1 approval for T0 to T2 and 2 for T3, and auto-approves nothing, which
  matches today's rule that the repository owner approves every pull request.

## How the loader treats them

- Validation is strict: unknown keys, unknown tiers, missing levels, bands out of order,
  overlapping or with gaps, an escalation owner that is not a role in `approvers.yaml`, a higher
  tier asking for fewer approvals, T2/T3 listed as auto-approved, or a missing or unknown
  `fallback_tier` all raise `PolicyError` with the file and key named.
- There are no defaults in code. Until these files are applied, any feature that asks for
  policies gets a `PolicyError`; the API itself still starts, because policies load on first use.
- Under Docker Compose the API reads `./policies` mounted read-only at `/policies`
  (`POLICIES_DIR`); outside Docker it reads `policies/` at the repository root.

## Apply

1. Review the four files (and edit the values if needed).
2. On a branch named `non-phase-work-policies`, move them to `policies/` at the repository root,
   delete `policies/.gitkeep` and this folder's copies (keep this README until the Pending entry
   in `ci/proposed/README.md` is moved to Applied).
3. Run `cd apps/api && pytest -q tests/test_policy.py`: `test_drafted_policy_files_load` now reads
   `policies/` instead of this folder and must pass.
4. Open a pull request and merge it with a merge commit.
