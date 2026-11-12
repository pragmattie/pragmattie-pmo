import dataclasses
import hashlib
import re
import shutil
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from policy_files import gates_file, gates_policy_dir

from app.config import Settings, get_settings
from app.policy import PolicyError, get_policies, load_policies
from app.roles import Role

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
    assert dict(policies.gates.gates) == {}
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


def test_live_gates_placeholder_still_loads():
    gates = load_policies(REPO_ROOT / "policies").gates
    assert isinstance(gates.gates, Mapping)
    assert len(gates.digest) == 64


def test_proposed_gates_file_loads(tmp_path):
    """The real file from the repository, so the file a person applies is the file tested."""
    gates = load_policies(gates_policy_dir(tmp_path)).gates
    approval = gates.gates["approval"]
    assert (approval.from_stage, approval.to_stage) == ("intake", "planning")
    assert approval.applies_to == "request"
    assert approval.deciders == ("sponsor",)
    checks = [c.check for c in approval.criteria if not c.person]
    assert checks == [
        "value_case_confirmed",
        "readiness_confirmed",
        "readiness_not_red",
        "yellow_actions_owned",
    ]
    assert [c.text for c in approval.criteria if c.person] == [
        "The expected value justifies the cost",
        "We have, or can get, the people to do it",
    ]
    assert gates.digest == hashlib.sha256(gates_file().read_bytes()).hexdigest()


def test_proposed_gates_file_adds_the_go_ahead_gate(tmp_path):
    gates = load_policies(gates_policy_dir(tmp_path)).gates
    assert list(gates.gates) == ["approval", "go_ahead"]
    go_ahead = gates.gates["go_ahead"]
    assert (go_ahead.from_stage, go_ahead.to_stage) == ("planning", "delivery")
    assert go_ahead.applies_to == "project"
    assert go_ahead.deciders == ("sponsor",)
    assert [c.check for c in go_ahead.criteria if not c.person] == [
        "plan_complete",
        "estimate_within_budget",
        "proposal_accepted",
        "no_unreviewed_risks",
        "escalations_settled",
        "readiness_not_red",
    ]
    assert [c.text for c in go_ahead.criteria if c.person] == [
        "The team and dates in the plan are realistic",
        "The project's risks are acceptable to start",
    ]


def test_the_approval_gate_is_unchanged_from_the_applied_file(tmp_path):
    """The proposed file keeps the approval gate exactly as the live file has it."""
    applied = load_policies(REPO_ROOT / "policies").gates.gates
    if "approval" not in applied:
        pytest.skip("the live gates file has no approval gate")
    proposed = load_policies(gates_policy_dir(tmp_path)).gates.gates
    assert proposed["approval"] == applied["approval"]


def test_a_check_must_suit_the_gates_subject(tmp_path):
    def change(data):
        _with_gate(_criterion(0, check="plan_complete"))(data)

    assert_rejected(tmp_path, "gates.yaml", change, "'plan_complete' can't check a request")


def test_the_checks_cover_exactly_the_subjects_the_loader_allows():
    from app.gates import CHECKS
    from app.policy import CHECK_SUBJECTS

    assert CHECKS == dict(CHECK_SUBJECTS)


def test_gates_digest_changes_with_the_file(tmp_path):
    first = load_policies(VALID).gates.digest
    changed = policy_dir(tmp_path)
    (changed / "gates.yaml").write_text("# a comment\n" + (VALID / "gates.yaml").read_text())
    assert load_policies(changed).gates.digest != first


def _with_gate(change: Callable[[dict], None]) -> Callable[[dict], None]:
    """Put the proposed approval gate in the fixture's gates file, then apply `change` to it."""

    def apply(data: dict) -> None:
        gate = yaml.safe_load(gates_file().read_text())["gates"][0]
        change(gate)
        data["gates"] = [gate]

    return apply


def _set(key, value):
    return lambda gate: gate.update({key: value})


def _criterion(index, **changes):
    return lambda gate: gate["criteria"][index].update(changes)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (_criterion(0, check="budget_is_small"), "unknown check 'budget_is_small'"),
        (_set("deciders", ["sponsor", "chief"]), "unknown role 'chief'"),
        (_set("deciders", ["approver"]), "unknown role 'approver'"),
        (_set("from_stage", "initiation"), "unknown stage 'initiation'"),
        (_set("to_stage", "done"), "unknown stage 'done'"),
        (_set("to_stage", "intake"), "must lead to a different stage"),
        (_set("applies_to", "programme"), "unknown subject 'programme'"),
        (_set("deciders", []), "non-empty list of roles"),
        (_set("deciders", ["sponsor", "sponsor"]), "roles must not repeat"),
        (_set("criteria", []), "non-empty list of criteria"),
        (_criterion(1, id="value_case_confirmed"), "duplicate criterion id 'value_case_confirmed'"),
        (_criterion(4, check="readiness_not_red"), "exactly one of `check` or `person`"),
        (lambda g: g["criteria"][0].pop("check"), "exactly one of `check` or `person`"),
        (_criterion(4, person=False), "person: must be true"),
        (_criterion(0, id="Value Case"), "lower-case words joined by underscores"),
        (_set("id", "approval gate"), "lower-case words joined by underscores"),
        (_criterion(0, text=""), "expected non-empty text"),
        (_set("owner", "sponsor"), "unknown keys: owner"),
        (lambda g: g.pop("deciders"), "missing keys: deciders"),
    ],
)
def test_bad_gate_raises(tmp_path, change, match):
    assert_rejected(tmp_path, "gates.yaml", _with_gate(change), match)


def test_duplicate_gate_id_raises(tmp_path):
    def change(data):
        _with_gate(lambda gate: None)(data)
        data["gates"].append(dict(data["gates"][0]))

    assert_rejected(tmp_path, "gates.yaml", change, "duplicate gate id 'approval'")


def test_gates_must_be_a_list(tmp_path):
    assert_rejected(tmp_path, "gates.yaml", lambda d: d.update(gates={}), "expected a list")


def test_every_known_role_may_decide_a_gate(tmp_path):
    def change(gate):
        gate["deciders"] = [role.value for role in Role]

    gates = load_policies(policy_dir(tmp_path, "gates.yaml", _with_gate(change))).gates
    assert set(gates.gates["approval"].deciders) == {role.value for role in Role}


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
        # app/standards.py reads the one other YAML file, the standards map (item 1.6); config.py
        # names that file's default path.
        yaml_text = text.replace("standards-map.yaml", "") if name == "app/config.py" else text
        readers = ("app/policy.py", "app/standards.py")
        if name not in readers and re.search(r"yaml", yaml_text, re.IGNORECASE):
            offenders.append(f"{name} mentions yaml")
        if name not in ("app/policy.py", "app/config.py") and "policies_dir" in text:
            offenders.append(f"{name} uses policies_dir")
    assert offenders == []


# --- The reporting policy (item 7.3)

REPORTING_FIXTURE = Path(__file__).parent / "fixtures" / "policies" / "reporting"


def _reporting(change: Callable[[dict], None] | None = None):
    from app.policy import parse_reporting

    data = yaml.safe_load((REPORTING_FIXTURE / "reporting.yaml").read_text())
    if change:
        change(data)
    return parse_reporting(data)


def test_reporting_fixture_loads():
    from app.policy import load_reporting

    policy = load_reporting(REPORTING_FIXTURE)
    assert (policy.schedule.amber, policy.schedule.red) == (Decimal(0), Decimal(10))
    assert (policy.cost.amber, policy.cost.red) == (Decimal(5), Decimal(15))
    assert (policy.scope.amber, policy.scope.red) == (Decimal(5), Decimal(10))
    assert dict(policy.risk_amber_at) == {"Medium": 1, "High": 1}
    assert dict(policy.risk_red_at) == {"High": 2}


def test_reporting_missing_file_names_where_the_draft_is(tmp_path):
    from app.policy import load_reporting

    with pytest.raises(PolicyError, match="reporting.yaml: policy file not found"):
        load_reporting(tmp_path)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda d: d.update(extra=1), "unknown keys: extra"),
        (lambda d: d.pop("scope"), "missing keys: scope"),
        (lambda d: d["schedule"].update(red_above_working_days=-1), "whole number"),
        (lambda d: d["schedule"].update(amber_above_working_days=1.5), "whole number"),
        (lambda d: d["cost"].update(red_above_points=True), "expected a number"),
        (lambda d: d["scope"].update(amber_above_pct=101), "expected a number"),
        (lambda d: d["scope"].update(amber_above_pct=5.001), "2 decimal places"),
        (lambda d: d["cost"].update(red_above_points=4), "must be at least amber_above_points"),
        (lambda d: d["risk"].update(red_at={}), "at least one band"),
        (lambda d: d["risk"]["amber_at"].update(High=0), "at least 1"),
        (lambda d: d["risk"].update(watch={}), "unknown keys: watch"),
    ],
)
def test_reporting_rejects_bad_values(change, match):
    with pytest.raises(PolicyError, match=match):
        _reporting(change)
