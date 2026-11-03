"""Policy directories for tests that need the gates file.

The gates file is drafted in ci/proposed/policies/ and applied to policies/ by a person. Tests load
the real file from the repository (never a copy of its contents written into a test): the draft
while it is pending, the applied file once the draft is gone.
"""

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIED_POLICIES = REPO_ROOT / "policies"
DRAFT_POLICIES = REPO_ROOT / "ci" / "proposed" / "policies"


def gates_file() -> Path:
    draft = DRAFT_POLICIES / "gates.yaml"
    return draft if draft.exists() else APPLIED_POLICIES / "gates.yaml"


def gates_policy_dir(tmp_path: Path) -> Path:
    """A policies directory: the applied files, with the gates file from `gates_file()`."""
    target = tmp_path / "gates-policies"
    target.mkdir()
    for path in APPLIED_POLICIES.glob("*.yaml"):
        shutil.copy(path, target)
    shutil.copy(gates_file(), target / "gates.yaml")
    return target
