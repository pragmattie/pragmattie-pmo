import dataclasses
import re
import shutil
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from app.config import Settings, get_settings
from app.policy import PolicyError, get_policies, load_policies

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1]
VALID = Path(__file__).parent / "fixtures" / "policies" / "valid"


def policy_dir(tmp_path: Path, name: str | None = None, change: Callable | None = None) -> Path:
    """A copy of the valid fixture set, with `change(data)` applied to the file `name`."""
    target = tmp_path / "policies"
    shutil.copytree(VALID, target)
    if name:
        path = target / name
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    return target


def assert_rejected(tmp_path, name, change, match):
    with pytest.raises(PolicyError, match=match):
        load_policies(policy_dir(tmp_path, name, change))


# --- The valid set


def test_valid_set_loads_into_frozen_objects():
    policies = load_policies(VALID)
    tiers = policies.tiers
    assert list(tiers.tiers) == ["T0", "T1", "T2", "T3"]
    assert tiers.fallback_tier == "T2"
    assert tiers.auto_approved == frozenset()
    assert tiers.tiers["T3"].approvals_required == 2

    risk = policies.risk
    bands = [(b.name, b.min_score, b.max_score, b.escalation_owner) for b in risk.priority_bands]
    assert bands == [
        ("Low", 1, 6, "project_manager"),
        ("Medium", 7, 12, "sponsor"),
        ("High", 13, 25, "sponsor"),
    ]
    assert dict(risk.minimum_band_by_impact) == {5: "Medium"}
    assert list(risk.probability) == list(risk.impact) == [1, 2, 3, 4, 5]
    assert risk.thresholds["schedule"].allowed_variation_pct == Decimal("10")
    assert risk.thresholds["cost"].allowed_variation_pct == Decimal("10")
    assert risk.thresholds["quality"].allowed_variation_pct is None
    assert policies.gates.gates == ()
    assert all(a.contact.endswith(".example") for a in policies.approvers.roles.values())

    with pytest.raises(dataclasses.FrozenInstanceError):
        tiers.fallback_tier = "T0"
    with pytest.raises(dataclasses.FrozenInstanceError):
        risk.priority_bands[0].max_score = 25
    with pytest.raises(TypeError):
        tiers.tiers["T4"] = tiers.tiers["T3"]


def test_drafted_policy_files_load():
    """The real files if a person has applied them, otherwise the drafts awaiting review."""
    applied = REPO_ROOT / "policies"
    directory = applied if any(applied.glob("*.yaml")) else REPO_ROOT / "ci/proposed/policies"
    policies = load_policies(directory)
    assert policies.tiers.fallback_tier == "T2"


# --- Missing and malformed files


@pytest.mark.parametrize("name", ["tiers.yaml", "risk.yaml", "gates.yaml", "approvers.yaml"])
def test_missing_file_raises_with_its_name(tmp_path, name):
    directory = policy_dir(tmp_path)
    (directory / name).unlink()
    with pytest.raises(PolicyError, match=rf"{name}: policy file not found"):
        load_policies(directory)


def test_bad_yaml_raises(tmp_path):
    directory = policy_dir(tmp_path)
    (directory / "risk.yaml").write_text("version: 1\nprobability: [unclosed\n")
    with pytest.raises(PolicyError, match="risk.yaml: not valid YAML"):
        load_policies(directory)


def test_non_utf8_file_raises(tmp_path):
    directory = policy_dir(tmp_path)
    (directory / "tiers.yaml").write_bytes(b"version: 1\nfallback_tier: \xff\xfe\n")
    with pytest.raises(PolicyError, match="tiers.yaml: not valid UTF-8"):
        load_policies(directory)


def test_non_mapping_file_raises(tmp_path):
    directory = policy_dir(tmp_path)
    (directory / "gates.yaml").write_text("- just\n- a list\n")
    with pytest.raises(PolicyError, match="gates.yaml: expected a mapping"):
        load_policies(directory)


def test_safe_load_refuses_python_objects(tmp_path):
    directory = policy_dir(tmp_path)
    (directory / "gates.yaml").write_text("version: 1\ngates: !!python/object/apply:os.getcwd []\n")
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policies(directory)


def test_wrong_version_raises(tmp_path):
    assert_rejected(tmp_path, "gates.yaml", lambda d: d.update(version=2), "version must be 1")


def test_unknown_key_raises(tmp_path):
    assert_rejected(
        tmp_path, "tiers.yaml", lambda d: d.update(autoapprove=["T0"]), "unknown keys: autoapprove"
    )


def test_unknown_nested_key_raises(tmp_path):
    def change(data):
        data["priority_bands"][0]["colour"] = "green"

    assert_rejected(tmp_path, "risk.yaml", change, r"priority_bands\[0\]: unknown keys: colour")


# --- Tiers


def test_unknown_tier_raises(tmp_path):
    def change(data):
        data["tiers"]["T4"] = {"description": "Too high", "approvals_required": 3}

    assert_rejected(tmp_path, "tiers.yaml", change, "unknown tier 'T4'")


def test_missing_tier_raises(tmp_path):
    assert_rejected(tmp_path, "tiers.yaml", lambda d: d["tiers"].pop("T1"), "missing tiers: T1")


def test_missing_fallback_tier_raises(tmp_path):
    assert_rejected(
        tmp_path, "tiers.yaml", lambda d: d.pop("fallback_tier"), "missing keys: fallback_tier"
    )


@pytest.mark.parametrize("value", ["T9", None, ""])
def test_unknown_fallback_tier_raises(tmp_path, value):
    assert_rejected(
        tmp_path,
        "tiers.yaml",
        lambda d: d.update(fallback_tier=value),
        "fallback_tier: unknown tier",
    )


def test_higher_tier_asking_for_fewer_approvals_raises(tmp_path):
    def change(data):
        data["tiers"]["T3"]["approvals_required"] = 0

    assert_rejected(tmp_path, "tiers.yaml", change, "T3 asks for fewer approvals than T2")


@pytest.mark.parametrize("tier", ["T2", "T3"])
def test_high_tier_listed_as_auto_approved_raises(tmp_path, tier):
    def change(data):
        data["auto_approved"] = [tier]
        for name in ("T0", "T1", "T2", "T3"):
            data["tiers"][name]["approvals_required"] = 0

    assert_rejected(tmp_path, "tiers.yaml", change, f"{tier} can never be auto-approved")


def test_auto_approved_tier_needs_zero_approvals(tmp_path):
    assert_rejected(
        tmp_path,
        "tiers.yaml",
        lambda d: d.update(auto_approved=["T0"]),
        "auto-approved exactly when it needs 0 approvals",
    )


def test_low_tier_may_be_auto_approved(tmp_path):
    def change(data):
        data["auto_approved"] = ["T0"]
        data["tiers"]["T0"]["approvals_required"] = 0

    policies = load_policies(policy_dir(tmp_path, "tiers.yaml", change))
    assert policies.tiers.auto_approved == frozenset({"T0"})


def test_boolean_is_not_an_approval_count(tmp_path):
    def change(data):
        data["tiers"]["T0"]["approvals_required"] = True

    assert_rejected(tmp_path, "tiers.yaml", change, "expected a whole number")


# --- Risk bands, scales and thresholds


def test_out_of_order_bands_raise(tmp_path):
    assert_rejected(
        tmp_path, "risk.yaml", lambda d: d["priority_bands"].reverse(), "bands are out of order"
    )


def test_overlapping_bands_raise(tmp_path):
    def change(data):
        data["priority_bands"][1]["min_score"] = 6

    assert_rejected(tmp_path, "risk.yaml", change, "bands Low and Medium overlap")


def test_gap_between_bands_raises(tmp_path):
    def change(data):
        data["priority_bands"][1]["min_score"] = 8

    assert_rejected(tmp_path, "risk.yaml", change, "gap between bands Low and Medium")


def test_bands_must_cover_every_score(tmp_path):
    def change(data):
        data["priority_bands"][2]["max_score"] = 20

    assert_rejected(tmp_path, "risk.yaml", change, "cover every score from 1 to 25")


def test_band_with_min_above_max_raises(tmp_path):
    def change(data):
        data["priority_bands"][0]["min_score"] = 10
        data["priority_bands"][0]["max_score"] = 8

    assert_rejected(tmp_path, "risk.yaml", change, "min_score is above max_score")


def test_escalation_owner_must_be_a_role(tmp_path):
    def change(data):
        data["priority_bands"][2]["escalation_owner"] = "chief_executive"

    assert_rejected(tmp_path, "risk.yaml", change, "'chief_executive' is not a role")


def test_scale_must_run_1_to_5(tmp_path):
    assert_rejected(tmp_path, "risk.yaml", lambda d: d["probability"].pop(3), "missing levels: 3")


def test_scale_rejects_extra_level(tmp_path):
    def change(data):
        data["impact"][6] = dict(data["impact"][5])

    assert_rejected(tmp_path, "risk.yaml", change, "unknown level 6")


def test_impact_level_needs_every_objective(tmp_path):
    assert_rejected(
        tmp_path, "risk.yaml", lambda d: d["impact"][2].pop("cost"), r"impact.2: missing keys: cost"
    )


def test_cost_threshold_needs_a_percentage(tmp_path):
    assert_rejected(
        tmp_path,
        "risk.yaml",
        lambda d: d["thresholds"]["cost"].pop("allowed_variation_pct"),
        "missing keys: allowed_variation_pct",
    )


def test_threshold_percentage_is_bounded(tmp_path):
    def change(data):
        data["thresholds"]["schedule"]["allowed_variation_pct"] = 150

    assert_rejected(tmp_path, "risk.yaml", change, "expected a number from 0 to 100")


def test_impact_floor_must_name_a_band(tmp_path):
    assert_rejected(
        tmp_path,
        "risk.yaml",
        lambda d: d["minimum_band_by_impact"].update({5: "Severe"}),
        "'Severe' is not a priority band",
    )


@pytest.mark.parametrize("value", [["Medium"], {"band": "Medium"}, 3, None])
def test_impact_floor_must_be_text(tmp_path, value):
    assert_rejected(
        tmp_path,
        "risk.yaml",
        lambda d: d["minimum_band_by_impact"].update({5: value}),
        r"minimum_band_by_impact\.5: expected non-empty text",
    )


def test_impact_floor_must_name_a_level(tmp_path):
    assert_rejected(
        tmp_path,
        "risk.yaml",
        lambda d: d["minimum_band_by_impact"].update({7: "High"}),
        "unknown impact level 7",
    )


def test_impact_floor_never_drops_as_impact_rises(tmp_path):
    def change(data):
        data["minimum_band_by_impact"] = {4: "High", 5: "Medium"}

    assert_rejected(
        tmp_path, "risk.yaml", change, "impact 5 has a lower minimum band than impact 4"
    )


# --- Gates and approvers


def test_gates_stay_empty_until_p5(tmp_path):
    assert_rejected(
        tmp_path,
        "gates.yaml",
        lambda d: d.update(gates=["G1"]),
        "empty list until stage gates arrive in P5",
    )


def test_approver_contact_must_be_example_domain(tmp_path):
    def change(data):
        data["roles"]["sponsor"]["contact"] = "sponsor@pragmattie.com"

    assert_rejected(tmp_path, "approvers.yaml", change, "must be an address in the .example")


# --- Loading on first use


def test_get_policies_loads_on_first_use_and_does_not_cache_failures(tmp_path, monkeypatch):
    directory = tmp_path / "empty"
    directory.mkdir()
    monkeypatch.setenv("POLICIES_DIR", str(directory))
    get_settings.cache_clear()
    get_policies.cache_clear()
    try:
        with pytest.raises(PolicyError, match="policy file not found"):
            get_policies()
        for path in VALID.glob("*.yaml"):
            shutil.copy(path, directory)
        assert get_policies().tiers.fallback_tier == "T2"
        assert get_policies() is get_policies()
    finally:
        get_settings.cache_clear()
        get_policies.cache_clear()


def test_app_starts_without_policy_files(client):
    """Policies are not read at startup, so the API runs before the real files are applied."""
    get_policies.cache_clear()
    with client:  # runs the app's startup and shutdown
        assert client.get("/api/v1/health").status_code == 200
    assert get_policies.cache_info().currsize == 0


def test_policies_dir_defaults_to_repository_root():
    assert Settings(_env_file=None, app_env="test").policies_dir == REPO_ROOT / "policies"


# --- The loader is the only code reading policy files


def test_only_the_loader_reads_policy_files():
    offenders = []
    for path in sorted((API_DIR / "app").rglob("*.py")):
        name = path.relative_to(API_DIR).as_posix()
        text = path.read_text()
        if name != "app/policy.py" and re.search(r"yaml", text, re.IGNORECASE):
            offenders.append(f"{name} mentions yaml")
        if name not in ("app/policy.py", "app/config.py") and "policies_dir" in text:
            offenders.append(f"{name} uses policies_dir")
    assert offenders == []
