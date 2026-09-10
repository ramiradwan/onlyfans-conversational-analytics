"""Verify the machine-readable architecture baseline and its controls."""

from __future__ import annotations

import json
from pathlib import Path

from tools.validate_architecture_boundaries import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_MARKDOWN_PATH,
    check_manifest_markdown_consistency,
    get_tracked_files,
    load_manifest,
    resolve_executable_reference,
    validate_all_files_coverage,
    validate_manifest_schema,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = DEFAULT_MANIFEST_PATH
ASSURANCE_REPORT = ROOT / "docs" / "architecture-assurance.md"
QUALIFIED_INGESTION_ANALYTICS_INVARIANTS = frozenset(
    {
        "atomic-canonical-commit",
        "snapshot-integrity",
        "deletion-closure",
        "durable-agent-delivery",
        "graph-identity",
        "provenance-integrity",
    }
)

# Negative controls verify that each enforced rule rejects violations.
RULE_NEGATIVE_CONTROLS = {
    "rule-canonical-read-model-ownership": (
        "tests/test_architecture_contracts.py::"
        "test_negative_control_legacy_transport_account_read_model_import"
    ),
    "rule-canonical-persistence-no-upward": (
        "tests/test_architecture_contracts.py::"
        "test_negative_control_protected_persistence_imports_analytics"
    ),
    "rule-canonical-history-gateway": (
        "tests/test_architecture_contracts.py::"
        "test_negative_control_ordinary_analytics_imports_history"
    ),
    "rule-agent-capture-isolation": "extension/tests/architecture-boundaries.test.mjs",
    "rule-persistence-factory-no-analytics": (
        "tests/test_architecture_contracts.py::"
        "test_negative_control_persistence_factory_imports_analytics"
    ),
    "rule-no-service-to-transport": (
        "tests/test_architecture_contracts.py::"
        "test_negative_control_insights_service_imports_transport"
    ),
    "rule-transport-analytics-separation": (
        "tests/test_architecture_contracts.py::"
        "test_negative_control_transport_manager_imports_canonical_analytics_source"
    ),
    "rule-runtime-policy-confinement": (
        "tests/test_architecture_runtime_policy.py::"
        "test_guard_rejects_endpoint_role_decision"
    ),
    "rule-grant-licence-admission-confinement": (
        "tests/test_architecture_admission.py::"
        "test_guard_rejects_permit_identifier_in_grant_or_licence_path"
    ),
    "rule-agent-protected-acyclic": "extension/tests/architecture-boundaries.test.mjs",
    "rule-bridge-protected-acyclic": "frontend/tests/architecture-boundaries.test.ts",
}

# Published labels are part of the checked report contract.
ASSURANCE_COUNT_LABELS = {
    "Modules": "modules",
    "Semantic invariants": "semantic_invariants",
    "Qualified semantic invariants": "qualified_semantic_invariants",
    "Unique permanent semantic-oracle falsifiers": (
        "permanent_semantic_oracle_falsifiers"
    ),
    "Rules": "rules",
    "Enforced rules": "enforced_rules",
    "Documented rules": "documented_rules",
    "Rule-scoped permanent architecture negative controls": (
        "rule_scoped_permanent_architecture_negative_controls"
    ),
    "Python prohibited dependency contracts": "python_prohibited_dependency_contracts",
    "Temporary exceptions": "temporary_exceptions",
    "Current-design exceptions": "current_design_exceptions",
    "Protected-impact classifier mappings": "protected_impact_classifier_rules",
    "Unclassified tracked production paths": "unclassified_tracked_production_paths",
}

REQUIRED_CI_CONTROLS = (
    "fixed-sqlcipher-wheel",
    "windows-tests",
    "windows-browser-e2e",
    "Run Brain Tier A general state machine",
    "Run Agent Tier A general state machine",
    "Run deterministic analytics rebuilds",
    "Run incremental analytics convergence",
    "Run derived deletion closure",
    "Run analytics oracle falsifiers",
    "Build and audit deterministic extension artifact",
    "Qualify bounded 10k-message snapshot repair",
    "Run Product #5 evidence scenarios",
    "Run browser E2E and preserve output",
    "Test the release bindings gate",
    "Test the packaged signing rule gate",
)


def _manifest() -> dict:
    return load_manifest(MANIFEST)


def _invariants(manifest: dict) -> dict[str, dict]:
    return {entry["id"]: entry for entry in manifest["semantic_invariants"]}


def baseline_counts(manifest: dict | None = None) -> dict[str, int]:
    """Compute baseline counts from authoritative repository inputs."""

    manifest = manifest or _manifest()
    qualified = [
        invariant
        for invariant in manifest["semantic_invariants"]
        if invariant["assurance"]["status"] == "qualified"
    ]
    unclassified_paths = validate_all_files_coverage(
        manifest, get_tracked_files(ROOT)
    )
    return {
        "modules": len(manifest["modules"]),
        "semantic_invariants": len(manifest["semantic_invariants"]),
        "qualified_semantic_invariants": len(qualified),
        "permanent_semantic_oracle_falsifiers": len(
            {invariant["assurance"]["falsifier"] for invariant in qualified}
        ),
        "rules": len(manifest["rules"]),
        "enforced_rules": sum(
            rule["enforcement"]["status"] == "enforced"
            for rule in manifest["rules"]
        ),
        "documented_rules": sum(
            rule["enforcement"]["status"] == "documented"
            for rule in manifest["rules"]
        ),
        "rule_scoped_permanent_architecture_negative_controls": len(
            RULE_NEGATIVE_CONTROLS
        ),
        "temporary_exceptions": sum(
            exception["status"] == "temporary_exception"
            for exception in manifest["exceptions"]
        ),
        "current_design_exceptions": sum(
            exception["status"] == "current_design"
            for exception in manifest["exceptions"]
        ),
        "protected_impact_classifier_rules": len(
            manifest["protected_impact"]["invariant_path_mappings"]
        ),
        "python_prohibited_dependency_contracts": (
            ROOT / ".importlinter"
        ).read_text(encoding="utf-8").count("[importlinter:contract:"),
        "unclassified_tracked_production_paths": len(unclassified_paths),
    }


def assurance_report_counts(path: Path = ASSURANCE_REPORT) -> dict[str, int]:
    """Parse the report's computed-count table without treating it as prose."""

    lines = path.read_text(encoding="utf-8").splitlines()
    heading = "## Computed repository-local counts"
    start = lines.index(heading) + 1
    for index in range(start, len(lines) - 1):
        if lines[index].strip() == "| Measure | Count |":
            separator = lines[index + 1].strip()
            assert separator == "| --- | ---: |", (
                "Assurance report computed-count table has an invalid separator"
            )
            rows: dict[str, int] = {}
            for row in lines[index + 2 :]:
                if not row.strip():
                    break
                cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
                assert len(cells) == 2, (
                    f"Assurance report computed-count row must have two cells: {row!r}"
                )
                label, raw_count = cells
                assert label not in rows, f"Duplicate assurance report count label: {label}"
                assert raw_count.isdecimal(), (
                    f"Assurance report count must be a non-negative integer: {row!r}"
                )
                rows[label] = int(raw_count)
            return rows
    raise AssertionError("Assurance report is missing its computed-count table")


def test_machine_authority_consumers_and_classification_are_consistent() -> None:
    manifest = _manifest()
    assert manifest["machine_authority"] == "docs/architecture-boundaries.json"
    assert not validate_manifest_schema(manifest, repo_root=ROOT)
    assert not check_manifest_markdown_consistency(manifest, DEFAULT_MARKDOWN_PATH)
    assert not validate_all_files_coverage(manifest, get_tracked_files(ROOT))

    validator = (ROOT / "tools" / "validate_architecture_boundaries.py").read_text(
        encoding="utf-8"
    )
    impact_gate = (ROOT / "tools" / "check_boundary_declaration.py").read_text(
        encoding="utf-8"
    )
    assert "DEFAULT_MANIFEST_PATH" in validator
    assert "MANIFEST_REPOSITORY_PATH = \"docs/architecture-boundaries.json\"" in impact_gate


def test_enforced_rules_have_executable_controls_and_permanent_negative_controls() -> None:
    manifest = _manifest()
    enforced = {
        rule["id"]: rule
        for rule in manifest["rules"]
        if rule["enforcement"]["status"] == "enforced"
    }
    assert set(enforced) == set(RULE_NEGATIVE_CONTROLS)
    for rule_id, rule in enforced.items():
        controls = rule["enforcement"]["enforced_by"]
        assert controls, rule_id
        for control in controls:
            assert not resolve_executable_reference(
                control, ROOT, f"{rule_id} production control"
            )
        negative = RULE_NEGATIVE_CONTROLS[rule_id]
        assert not resolve_executable_reference(
            negative, ROOT, f"{rule_id} negative control"
        )


def test_ingestion_and_analytics_assurance_entries_are_qualified_and_falsifiable() -> None:
    invariants = _invariants(_manifest())
    assert QUALIFIED_INGESTION_ANALYTICS_INVARIANTS <= set(invariants)
    for invariant_id in QUALIFIED_INGESTION_ANALYTICS_INVARIANTS:
        invariant = invariants[invariant_id]
        assurance = invariant["assurance"]
        assert assurance["status"] == "qualified", invariant_id
        assert invariant["requirement_or_scenario"].strip(), invariant_id
        assert assurance["evidence"], invariant_id
        for evidence in assurance["evidence"]:
            assert not evidence.startswith("planned:"), (invariant_id, evidence)
            assert not resolve_executable_reference(
                evidence, ROOT, f"{invariant_id} evidence"
            )
        assert not assurance["falsifier"].startswith("planned:"), invariant_id
        assert not resolve_executable_reference(
            assurance["falsifier"], ROOT, f"{invariant_id} falsifier"
        )


def test_contracts_and_runtime_evidence_keep_their_scope() -> None:
    ingestion_contract = (ROOT / "docs/architecture/ingestion-state-contract.md").read_text(
        encoding="utf-8"
    )
    rebuild_contract = (ROOT / "docs/architecture/rebuild-equivalence-contract.md").read_text(
        encoding="utf-8"
    )
    for required in (
        "Brain independent reference model",
        "Agent independent reference model",
        "Persistent-backend qualification requirements",
        "shrink",
    ):
        assert required.lower() in ingestion_contract.lower()
    for required in (
        "ReproducibilityContext",
        "Semantic State",
        "Provenance",
        "Lifecycle / Transient",
        "referential closure",
        "Publication freshness",
    ):
        assert required.lower() in rebuild_contract.lower()

    agent_delivery = json.loads(
        (ROOT / "docs/architecture/agent-delivery-local-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    persistent_ingestion = json.loads(
        (ROOT / "docs/architecture/persistent-ingestion-local-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    analytics_convergence = json.loads(
        (ROOT / "docs/architecture/analytics-convergence-local-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    assert agent_delivery["falsifier_probe"]["detected"] is True
    assert agent_delivery["persisted_frame_failure"]["record"].startswith("tests/")
    assert persistent_ingestion["qualification_claim"].endswith("not production-equivalent")
    assert persistent_ingestion["status"] == "blocked_unfixed_runtime"
    assert persistent_ingestion["benchmark"]["pr_runner_baseline_status"]["status"] == "pending"
    assert persistent_ingestion["connection_model"] and persistent_ingestion["checkpoint_behavior"]
    assert {"sqlite", "sqlcipher", "python", "os"} <= set(persistent_ingestion["runner"])
    assert persistent_ingestion["advisories"]
    assert analytics_convergence["limitations"][0] == "Local timing is not hosted-runner p95 evidence."
    assert analytics_convergence["deliberate_falsifier"]["measured"]["status"].startswith(
        "expected_failure"
    )


def test_composition_census_fences_governance_and_ci_remain_closed() -> None:
    factory = (ROOT / "app/persistence/factory.py").read_text(encoding="utf-8")
    transport = (ROOT / "app/transport/manager.py").read_text(encoding="utf-8")
    service = (ROOT / "app/services/insights_service.py").read_text(encoding="utf-8")
    bootstrap = (ROOT / "app/bootstrap.py").read_text(encoding="utf-8")
    assert "app.analytics" not in factory
    assert "HistoryAnalyticsSource" not in transport
    assert "app.transport" not in service
    assert "HistoryAnalyticsSource" in bootstrap

    boundaries = (ROOT / "docs/architecture-boundaries.md").read_text(encoding="utf-8")
    assert "Canonical persistence import census" in boundaries
    assert "None of the four protected modules import" in boundaries
    assert "This rule enforces acyclicity" in boundaries

    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for required in REQUIRED_CI_CONTROLS:
        assert required in workflow
    assert "- Status: proposed" in (
        ROOT / "docs/adr/0012-brain-internal-boundaries.md"
    ).read_text(encoding="utf-8")

    exception = _manifest()["exceptions"]
    assert len(exception) == 1
    assert exception[0]["status"] == "current_design"
    assert exception[0]["expires"] is None
    assert exception[0]["tracking_issue"] == "DESIGN-PROJECTION-ACTIVATION-IDENTITY"
    assert exception[0]["source"] == "app/persistence/projection_activation.py"
    assert exception[0]["target"] == "app.analytics"


def test_computed_baseline_counts_are_stable() -> None:
    assert baseline_counts() == {
        "modules": 25,
        "semantic_invariants": 22,
        "qualified_semantic_invariants": 13,
        "permanent_semantic_oracle_falsifiers": 8,
        "rules": 13,
        "enforced_rules": 11,
        "documented_rules": 2,
        "rule_scoped_permanent_architecture_negative_controls": 11,
        "temporary_exceptions": 0,
        "current_design_exceptions": 1,
        "protected_impact_classifier_rules": 22,
        "python_prohibited_dependency_contracts": 5,
        "unclassified_tracked_production_paths": 0,
    }


def test_assurance_report_computed_table_matches_baseline_counts() -> None:
    expected = baseline_counts()
    reported = assurance_report_counts()

    assert set(reported) == set(ASSURANCE_COUNT_LABELS)
    assert reported == {
        label: expected[count_key]
        for label, count_key in ASSURANCE_COUNT_LABELS.items()
    }
