"""Executable tests for architecture boundaries contract and machine manifest authority."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from tools.validate_architecture_boundaries import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_MARKDOWN_PATH,
    ArchitectureBoundaryError,
    PathClassification,
    UnclassifiedProductionPathError,
    check_manifest_markdown_consistency,
    classify_path,
    get_tracked_files,
    load_manifest,
    resolve_executable_reference,
    validate_all_files_coverage,
    validate_manifest_schema,
)

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "architecture_boundaries"


def test_manifest_schema_and_integrity() -> None:
    """The machine manifest must satisfy all structural and semantic schema constraints."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    errors = validate_manifest_schema(manifest, repo_root=ROOT)
    assert not errors, f"Manifest schema validation failed: {errors}"


def test_manifest_markdown_consistency() -> None:
    """Human-readable documentation must be consistent with the machine manifest."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    errors = check_manifest_markdown_consistency(manifest, DEFAULT_MARKDOWN_PATH)
    assert not errors, f"Markdown consistency check failed: {errors}"


def test_markdown_consistency_detects_altered_zone(tmp_path: Path) -> None:
    """Markdown consistency check must fail if a module zone is altered."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    mutated_md = original_md.replace(
        "| `agent-capture` | Red | Authoritative |",
        "| `agent-capture` | Green | Authoritative |",
    )
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text(mutated_md, encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0, "Expected error when module zone was mutated"
    assert any("agent-capture" in err and "green" in err.lower() for err in errors), errors


def test_markdown_consistency_detects_altered_authority(tmp_path: Path) -> None:
    """Markdown consistency check must fail if a module authority is altered."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    mutated_md = original_md.replace(
        "| `packaging-release` | Red | Authoritative |",
        "| `packaging-release` | Red | Derived |",
    )
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text(mutated_md, encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0, "Expected error when module authority was mutated"
    assert any("packaging-release" in err and "derived" in err.lower() for err in errors), errors


def test_markdown_consistency_detects_missing_module_row(tmp_path: Path) -> None:
    """Markdown consistency check must fail if a module row is deleted from Markdown."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    mutated_lines = [
        line for line in original_md.splitlines()
        if "`canonical-persistence`" not in line
    ]
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text("\n".join(mutated_lines) + "\n", encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0
    assert any("missing module id: canonical-persistence" in err for err in errors), errors


def test_markdown_consistency_detects_extra_module_row(tmp_path: Path) -> None:
    """Markdown consistency check must fail if an extra module row is added to Markdown."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    extra_row = "| `phantom-module` | Red | Authoritative | A nonexistent module. |"
    mutated_md = original_md.replace(
        "| `architecture-governance` | Red | Authoritative |",
        f"{extra_row}\n| `architecture-governance` | Red | Authoritative |",
    )
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text(mutated_md, encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0
    assert any("extra undeclared module id: phantom-module" in err for err in errors), errors


def test_markdown_consistency_detects_altered_rule_enforcement(tmp_path: Path) -> None:
    """Markdown consistency check must fail if a rule enforcement status is altered."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    mutated_md = original_md.replace(
        "| `rule-runtime-policy-confinement` | Protected | Enforced |",
        "| `rule-runtime-policy-confinement` | Protected | Documented |",
    )
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text(mutated_md, encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0, "Expected error when rule enforcement was mutated"
    assert any("rule-runtime-policy-confinement" in err for err in errors), errors


def test_markdown_consistency_detects_altered_rule_source_modules(tmp_path: Path) -> None:
    """Markdown consistency check must fail if rule source modules are altered."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    mutated_md = original_md.replace(
        "| `rule-runtime-policy-confinement` | Protected | Enforced | `brain-api-presentation`, `application-services` |",
        "| `rule-runtime-policy-confinement` | Protected | Enforced | `agent-capture` |",
    )
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text(mutated_md, encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0
    assert any("rule-runtime-policy-confinement" in err and "source modules" in err for err in errors), errors


def test_markdown_consistency_detects_missing_rule_row(tmp_path: Path) -> None:
    """Markdown consistency check must fail if a rule row is deleted from Markdown."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    mutated_lines = [
        line for line in original_md.splitlines()
        if "`rule-canonical-persistence-no-upward`" not in line
    ]
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text("\n".join(mutated_lines) + "\n", encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0
    assert any("missing rule id: rule-canonical-persistence-no-upward" in err for err in errors), errors


def test_markdown_consistency_detects_extra_rule_row(tmp_path: Path) -> None:
    """Markdown consistency check must fail if an extra rule row is added to Markdown."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    extra_row = "| `rule-phantom` | Forbidden | Documented | `agent-capture` | `brain-transport` | Phantom rule |"
    mutated_md = original_md.replace(
        "| `rule-canonical-persistence-no-upward` |",
        f"{extra_row}\n| `rule-canonical-persistence-no-upward` |",
    )
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text(mutated_md, encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0
    assert any("extra undeclared rule id: rule-phantom" in err for err in errors), errors


def test_markdown_consistency_detects_altered_exception(tmp_path: Path) -> None:
    """Markdown consistency check must fail if an exception attribute is altered."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    mutated_md = original_md.replace(
        "| `app/persistence/projection_activation.py` | `app.analytics` | `rule-projection-coordination-boundary` | `current_design` | None | `DESIGN-PROJECTION-ACTIVATION-IDENTITY` |",
        "| `app/persistence/projection_activation.py` | `app.analytics` | `rule-projection-coordination-boundary` | `temporary_exception` | None | `DESIGN-PROJECTION-ACTIVATION-IDENTITY` |",
    )
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text(mutated_md, encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0, "Expected error when exception status was mutated"
    assert any("app/persistence/projection_activation.py" in err for err in errors), errors


def test_markdown_consistency_detects_empty_exceptions_discrepancy() -> None:
    """Markdown consistency check must fail if JSON has no exceptions while Markdown defines them."""
    manifest = copy.deepcopy(load_manifest(DEFAULT_MANIFEST_PATH))
    manifest["exceptions"] = []

    errors = check_manifest_markdown_consistency(manifest, DEFAULT_MARKDOWN_PATH)
    assert len(errors) > 0
    assert any("extra undeclared exception" in err for err in errors), errors


def test_markdown_consistency_detects_altered_invariant_owner(tmp_path: Path) -> None:
    """Markdown consistency check must fail if invariant owner is altered."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    mutated_md = original_md.replace(
        "| `canonical-authority` | Brain canonical persistence |",
        "| `canonical-authority` | Wrong Subsystem Owner |",
    )
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text(mutated_md, encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0
    assert any("canonical-authority" in err and "owner" in err for err in errors), errors


def test_markdown_consistency_detects_altered_invariant_requirement(tmp_path: Path) -> None:
    """Markdown consistency check must fail if invariant requirement text is altered."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    mutated_md = original_md.replace(
        "HistoryRepository is the sole authoritative commit point for acknowledged platform conversation effects. (ADR 0001, ADR 0009)",
        "Mutated requirement text that deviates from machine manifest.",
    )
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text(mutated_md, encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0
    assert any("canonical-authority" in err and "requirement" in err for err in errors), errors


def test_markdown_consistency_detects_missing_invariant_row(tmp_path: Path) -> None:
    """Markdown consistency check must fail if an invariant row is deleted from Markdown."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    mutated_lines = [
        line for line in original_md.splitlines()
        if "`canonical-authority`" not in line
    ]
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text("\n".join(mutated_lines) + "\n", encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0
    assert any("missing invariant id: canonical-authority" in err for err in errors), errors


def test_markdown_consistency_detects_extra_invariant_row(tmp_path: Path) -> None:
    """Markdown consistency check must fail if an extra invariant row is added to Markdown."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    original_md = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    extra_row = "| `phantom-invariant` | Subsystem | Critical | Documented | Nonexistent invariant |"
    mutated_md = original_md.replace(
        "| `canonical-authority` |",
        f"{extra_row}\n| `canonical-authority` |",
    )
    temp_md = tmp_path / "architecture-boundaries.md"
    temp_md.write_text(mutated_md, encoding="utf-8")

    errors = check_manifest_markdown_consistency(manifest, temp_md)
    assert len(errors) > 0
    assert any("extra undeclared invariant id: phantom-invariant" in err for err in errors), errors


def test_adversarial_json_mutation_triggers_consistency_error() -> None:
    """Direct mutations to JSON manifest against unchanged Markdown must fail consistency validation."""
    manifest = copy.deepcopy(load_manifest(DEFAULT_MANIFEST_PATH))

    # Mutating a rule's source_modules in JSON triggers consistency failure
    m1 = copy.deepcopy(manifest)
    m1["rules"][0]["source_modules"] = ["agent-capture"]
    errs1 = check_manifest_markdown_consistency(m1, DEFAULT_MARKDOWN_PATH)
    assert len(errs1) > 0
    assert any("source modules" in e for e in errs1), errs1

    # Mutating an invariant's owner in JSON triggers consistency failure
    m2 = copy.deepcopy(manifest)
    m2["semantic_invariants"][0]["owner"] = "Adversarial Subsystem"
    errs2 = check_manifest_markdown_consistency(m2, DEFAULT_MARKDOWN_PATH)
    assert len(errs2) > 0
    assert any("owner" in e for e in errs2), errs2


def test_all_tracked_production_files_are_classified() -> None:
    """Every tracked file in the repository must be successfully classified without unclassified errors."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    tracked_files = get_tracked_files(ROOT)
    assert len(tracked_files) > 0, "Expected tracked files to exist"

    errors = validate_all_files_coverage(manifest, tracked_files)
    assert not errors, f"Unclassified production files found: {errors}"


def test_external_vendored_contracts_are_excluded() -> None:
    """External vendored contracts under contracts/ must be excluded from internal modules."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    test_paths = [
        "contracts/manifest.json",
        "contracts/consumer-pin.json",
        "contracts/loader.py",
        "contracts/schemas/commercial/v1/capability-permit.schema.json",
    ]
    for p in test_paths:
        classification = classify_path(p, manifest)
        assert classification.is_vendored_contract is True, f"{p} should be vendored contract"
        assert classification.is_production is False, f"{p} should not be internal production"
        assert classification.module_id is None, f"{p} should not have an internal module id"


def test_unknown_production_path_fails_closed() -> None:
    """Unknown production paths must fail closed by raising UnclassifiedProductionPathError."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    unknown_paths = [
        "app/unknown_namespace/new_service.py",
        "extension/mystery_dir/component.js",
        "frontend/src/untracked_subsystem/module.ts",
        "worker/new_runtime.py",
    ]
    for p in unknown_paths:
        with pytest.raises(UnclassifiedProductionPathError) as exc_info:
            classify_path(p, manifest)
        assert "matches no declared module" in str(exc_info.value) or "not classified" in str(
            exc_info.value
        )


def test_resolve_executable_reference_safety() -> None:
    """Executable reference resolver must safely resolve valid symbols and reject invalid ones."""
    # Valid existing reference to test function in test_architecture_runtime_policy.py
    errors = resolve_executable_reference(
        "tests/test_architecture_runtime_policy.py::test_runtime_authorization_is_confined_to_the_security_kernel",
        ROOT,
        "test",
    )
    assert not errors, f"Expected valid reference to resolve, got: {errors}"

    # Documentation files rejected
    for doc in ["README.md", "docs/README.md", "CONTRIBUTING.md", "app/README.md"]:
        errs = resolve_executable_reference(doc, ROOT, "test")
        assert any("non-executable" in e for e in errs), f"Expected rejection for {doc}: {errs}"

    # Non-executable file formats rejected
    for non_code in ["manifest.json", "docs/architecture-boundaries.json", "notes.txt"]:
        errs = resolve_executable_reference(non_code, ROOT, "test")
        assert any("non-executable" in e for e in errs)

    # Missing file
    errors = resolve_executable_reference("tests/non_existent_file.py::test_fn", ROOT, "test")
    assert any("does not exist" in e for e in errors)

    # Missing symbol in existing file
    errors = resolve_executable_reference(
        "tests/test_architecture_runtime_policy.py::definitely_non_existent_symbol_123",
        ROOT,
        "test",
    )
    assert any("not found" in e for e in errors)

    # Paths escaping repo rejected
    for escaping in ["../outside.py", "../../secret.py", "/etc/passwd", "C:/Windows/System32/cmd.exe"]:
        errs = resolve_executable_reference(escaping, ROOT, "test")
        assert any("escapes repository root" in e or "cannot be absolute" in e for e in errs), f"Expected escape rejection for {escaping}: {errs}"

    # Planned reference rejected for executable check
    errors = resolve_executable_reference("planned:Task-2-import-linter", ROOT, "test")
    assert any("cannot be planned" in e for e in errors)

    # Non-string reference rejected
    errors = resolve_executable_reference(None, ROOT, "test")
    assert any("must be a non-empty string" in e for e in errors)


def test_canonical_persistence_and_coordination_split() -> None:
    """Canonical persistence core must be split from coordination and have no upward imports."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    mod_map = {m["id"]: m for m in manifest["modules"]}

    assert "canonical-persistence" in mod_map
    assert "persistence-coordination" in mod_map
    assert "persistence-factory" in mod_map
    assert "persistence-projection-coordination" in mod_map

    canonical_patterns = set(mod_map["canonical-persistence"]["path_patterns"])
    # Canonical persistence core must NOT include repositories, backup, or retention_restore
    assert "app/persistence/repositories.py" not in canonical_patterns
    assert "app/persistence/backup.py" not in canonical_patterns
    assert "app/persistence/retention_restore.py" not in canonical_patterns
    assert "app/persistence/factory.py" not in canonical_patterns
    assert "app/persistence/sqlcipher_runtime.py" in canonical_patterns

    runtime = classify_path("app/persistence/sqlcipher_runtime.py", manifest)
    assert runtime.module_id == "canonical-persistence"
    assert runtime.zone == "red"
    assert runtime.authority == "authoritative"

    # Persistence coordination contains the coordination files
    coordination_patterns = set(mod_map["persistence-coordination"]["path_patterns"])
    assert "app/persistence/repositories.py" in coordination_patterns
    assert "app/persistence/backup.py" in coordination_patterns
    assert "app/persistence/retention_restore.py" in coordination_patterns

    # Separate rules exist for factory and projection coordination
    rules_map = {r["id"]: r for r in manifest["rules"]}
    assert "rule-persistence-factory-no-analytics" in rules_map
    assert rules_map["rule-persistence-factory-no-analytics"]["source_modules"] == ["persistence-factory"]
    assert "rule-projection-coordination-boundary" in rules_map
    assert rules_map["rule-projection-coordination-boundary"]["source_modules"] == ["persistence-projection-coordination"]


def test_exception_source_must_classify_to_rule_source_modules() -> None:
    """An exception whose source does not classify into a module in rule.source_modules is invalid."""
    manifest = copy.deepcopy(load_manifest(DEFAULT_MANIFEST_PATH))

    # Existing valid exceptions pass validation
    errors = validate_manifest_schema(manifest, repo_root=ROOT)
    assert not errors

    # Add an exception where source does not classify into the rule's source_modules
    manifest["exceptions"].append({
        "source": "app/api/endpoints/webauthn.py",  # brain-auth-api
        "target": "app.analytics.canonical_source",
        "rule": "rule-persistence-factory-no-analytics",  # source_modules: ["persistence-factory"]
        "reason": "Test mismatch exception",
        "status": "temporary_exception",
        "introduced_by": "test",
        "tracking_issue": "TEST-MISMATCH",
        "expires": "2027-12-31",
    })
    invalid_errors = validate_manifest_schema(manifest, repo_root=ROOT)
    assert len(invalid_errors) > 0
    assert any("is not in rule" in e for e in invalid_errors), invalid_errors


def test_snapshot_integrity_invariant_prose() -> None:
    """snapshot-integrity invariant must describe count/order and conflicting-content rejection."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    inv_map = {inv["id"]: inv for inv in manifest["semantic_invariants"]}

    assert "snapshot-integrity" in inv_map
    req = inv_map["snapshot-integrity"]["requirement_or_scenario"]
    assert "order" in req.lower() or "ordering" in req.lower()
    assert "count" in req.lower()
    assert "conflicting" in req.lower() or "rejected" in req.lower()
    assert "history.py" in req or "HistoryRepository" in req


def test_projection_reproducibility_invariant_prose() -> None:
    """projection-reproducibility invariant must specify equivalence under ReproducibilityContext."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    inv_map = {inv["id"]: inv for inv in manifest["semantic_invariants"]}

    assert "projection-reproducibility" in inv_map
    req = inv_map["projection-reproducibility"]["requirement_or_scenario"]
    assert "reproducibilitycontext" in req.lower()
    assert "transient" in req.lower()


def test_markdown_authoritative_mutations_prose() -> None:
    """Markdown authority section must not contain blanket irreversibility assertion on line 21."""
    md_text = DEFAULT_MARKDOWN_PATH.read_text(encoding="utf-8")
    assert "Mutations to authoritative state must be atomic and irreversible." not in md_text
    assert "approved ingress interfaces" in md_text


def test_engineering_attestation_classification() -> None:
    """tools/engineering_attestation.py and tools/packaging_policy.py must be classified correctly."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)

    attestation_cls = classify_path("tools/engineering_attestation.py", manifest)
    assert attestation_cls.module_id == "attestation-verification"
    assert attestation_cls.zone == "red"
    assert attestation_cls.authority == "authoritative"
    assert "release-integrity" in attestation_cls.protected_invariants

    packaging_cls = classify_path("tools/packaging_policy.py", manifest)
    assert packaging_cls.module_id == "packaging-release"
    assert packaging_cls.zone == "red"
    assert packaging_cls.authority == "authoritative"


def test_non_production_policy_structure() -> None:
    """non_production_policy must define external contracts, dev/test namespaces, and root files."""
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    policy = manifest.get("non_production_policy")
    assert isinstance(policy, dict)
    assert "external_vendored_contracts" in policy
    assert "development_and_test_namespaces" in policy
    assert "root_metadata_files" in policy


def apply_fixture_mutation(
    base_manifest: dict[str, Any],
    mutation_spec: dict[str, Any],
    *,
    scenario_name: str = "",
) -> dict[str, Any]:
    """Apply an allowlisted declarative mutation to a deepcopy of the authoritative manifest.

    Verifies that the mutation was actually applied to prevent silent no-ops.
    """
    manifest = copy.deepcopy(base_manifest)
    op = mutation_spec.get("op")
    target = mutation_spec.get("target")

    ALLOWED_TARGETS = {"modules", "rules", "semantic_invariants", "exceptions"}
    ALLOWED_OPS = {"set", "set_fields", "delete", "append", "duplicate", "remove_item"}

    if op not in ALLOWED_OPS:
        raise ValueError(f"Unsupported mutation op {op!r} in scenario {scenario_name!r}")
    if target not in ALLOWED_TARGETS:
        raise ValueError(f"Unsupported mutation target {target!r} in scenario {scenario_name!r}")

    target_list = manifest.get(target)
    if not isinstance(target_list, list):
        raise ValueError(f"Manifest target {target!r} is not a list in scenario {scenario_name!r}")

    if op == "set":
        idx = mutation_spec["index"]
        field = mutation_spec["field"]
        new_val = mutation_spec["value"]
        item = target_list[idx]
        old_val = item.get(field)
        assert old_val != new_val, (
            f"Silent no-op mutation in {scenario_name!r}: field {field!r} was already {new_val!r}"
        )
        item[field] = new_val
        assert item[field] == new_val

    elif op == "set_fields":
        idx = mutation_spec["index"]
        fields = mutation_spec["fields"]
        item = target_list[idx]
        for dotted_field, new_val in fields.items():
            parts = dotted_field.split(".")
            curr = item
            for p in parts[:-1]:
                curr = curr[p]
            last_key = parts[-1]
            old_val = curr.get(last_key)
            assert old_val != new_val, (
                f"Silent no-op mutation in {scenario_name!r}: {dotted_field!r} was already {new_val!r}"
            )
            curr[last_key] = new_val
            assert curr[last_key] == new_val

    elif op == "delete":
        idx = mutation_spec["index"]
        field = mutation_spec["field"]
        item = target_list[idx]
        assert field in item, (
            f"Silent no-op mutation in {scenario_name!r}: field {field!r} does not exist in target"
        )
        del item[field]
        assert field not in item

    elif op == "append":
        new_item = mutation_spec["item"]
        old_len = len(target_list)
        target_list.append(new_item)
        assert len(target_list) == old_len + 1
        assert target_list[-1] == new_item

    elif op == "duplicate":
        idx = mutation_spec["index"]
        old_len = len(target_list)
        dup = copy.deepcopy(target_list[idx])
        target_list.append(dup)
        assert len(target_list) == old_len + 1
        assert target_list[-1] == dup

    elif op == "remove_item":
        match = mutation_spec["match"]
        old_len = len(target_list)
        matched_indices = [
            i for i, x in enumerate(target_list)
            if all(x.get(k) == v for k, v in match.items())
        ]
        assert len(matched_indices) > 0, (
            f"Silent no-op mutation in {scenario_name!r}: no item in {target} matched {match}"
        )
        manifest[target] = [
            x for x in target_list
            if not all(x.get(k) == v for k, v in match.items())
        ]
        assert len(manifest[target]) < old_len
        assert not any(all(x.get(k) == v for k, v in match.items()) for x in manifest[target])

    return manifest


def load_negative_fixture(fixture_name: str, base_manifest: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Load a miniature negative fixture specification and apply it to a deepcopy of the authoritative manifest."""
    fixture_path = FIXTURES_DIR / f"{fixture_name}.json"
    assert fixture_path.exists(), f"Negative fixture specification missing: {fixture_path}"
    spec = json.loads(fixture_path.read_text(encoding="utf-8"))
    scenario = spec.get("scenario", fixture_name)
    mutation = spec["mutation"]
    mutated_manifest = apply_fixture_mutation(base_manifest, mutation, scenario_name=scenario)
    expected_diagnostic = spec.get("expected_diagnostic", "")
    return mutated_manifest, expected_diagnostic


@pytest.mark.parametrize(
    "fixture_name,expected_error_substring",
    [
        ("unknown_zone", "invalid zone"),
        ("missing_module_id", "missing or empty id"),
        ("invalid_authority", "invalid authority"),
        ("module_missing_role", "missing or empty role"),
        ("module_missing_permitted_inputs", "missing permitted_inputs"),
        ("module_missing_permitted_outputs", "missing permitted_outputs"),
        ("duplicate_rule_id", "duplicate rule id"),
        ("duplicate_invariant_id", "duplicate semantic invariant id"),
        ("enforced_rule_without_control", "enforced rule must name at least one executable control"),
        ("enforced_rule_with_planned_control", "executable reference cannot be planned"),
        ("enforced_rule_with_missing_control_file", "referenced file does not exist"),
        ("enforced_rule_with_missing_symbol", "not found"),
        ("documented_semantic_invariant_without_requirement", "missing requirement_or_scenario"),
        ("qualified_semantic_invariant_without_evidence", "qualified status requires non-empty evidence list"),
        ("qualified_semantic_invariant_without_falsifier", "qualified status requires permanent falsifier reference"),
        ("qualified_invariant_with_planned_falsifier", "executable reference cannot be planned"),
        ("qualified_invariant_with_missing_evidence", "referenced file does not exist"),
        ("qualified_invariant_with_non_string_evidence", "reference must be a non-empty string"),
        ("exception_without_reason", "missing or empty reason"),
        ("temporary_exception_without_tracking", "missing or empty tracking_issue"),
        ("exception_missing_tracking_issue", "missing or empty tracking_issue"),
        ("expired_temporary_exception", "temporary exception expired"),
        ("qualified_invariant_with_readme_evidence", "non-executable"),
        ("qualified_invariant_with_readme_falsifier", "non-executable"),
        ("enforced_rule_with_external_ref", "escapes repository root"),
        ("enforced_rule_with_readme_control", "non-executable"),
        ("exception_source_not_in_rule_scope", "is not in rule"),
        ("duplicate_exception_identity", "duplicate exception identity"),
    ],
)
def test_permanent_negative_fixtures_are_rejected(
    fixture_name: str, expected_error_substring: str
) -> None:
    """Every permanent negative fixture must be rejected with the expected failure diagnostic."""
    base_manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    manifest, expected_diag = load_negative_fixture(fixture_name, base_manifest)
    assert expected_error_substring.lower() in expected_diag.lower(), (
        f"Fixture {fixture_name} declared diagnostic {expected_diag!r} does not match test expectation {expected_error_substring!r}"
    )

    errors = validate_manifest_schema(manifest, repo_root=ROOT)
    assert len(errors) > 0, f"Expected validation errors for {fixture_name}, but none occurred"
    assert any(
        expected_error_substring.lower() in err.lower() for err in errors
    ), f"Expected error substring '{expected_error_substring}' in errors for {fixture_name}: {errors}"


def test_unclassified_production_namespace_fixture_is_rejected() -> None:
    """Fixture missing canonical-persistence must fail classification on app/persistence/history.py."""
    base_manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    manifest, _ = load_negative_fixture("unclassified_production_namespace", base_manifest)
    with pytest.raises(UnclassifiedProductionPathError):
        classify_path("app/persistence/history.py", manifest)
