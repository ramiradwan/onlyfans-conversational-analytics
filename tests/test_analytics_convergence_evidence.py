"""Integrity checks for the generated analytics-convergence evidence."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "architecture" / "analytics-convergence-local-evidence.json"
QUALIFIER = ROOT / "tools" / "qualify_analytics_convergence.py"
HARNESS = ROOT / "tests" / "stateful" / "test_analytics_equivalence.py"


def test_analytics_convergence_evidence_is_from_runtime_instrumentation() -> None:
    """Reject an artifact that cannot account for its measured executions."""

    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == "analytics-convergence-local-evidence.v2"
    assert evidence["evidence_origin"] == "tools/qualify_analytics_convergence.py"
    assert "file-backed SQLCipher" in evidence["scope"]["canonical_authority"]
    assert "InMemoryAnalyticsProjectionStore" in evidence["scope"]["derived_stores"]
    assert all(evidence["runtime"].values())

    general = evidence["profiles"]["analytics_convergence_fast"]["measured"]
    deletion = evidence["profiles"]["analytics_deletion_fast"]["measured"]
    for run, deliveries_per_history, rebuilds_per_history in (
        (general, 14, 2),
        (deletion, 11, 3),
    ):
        histories = run["actual_history_count"]
        assert histories > 0
        assert run["actual_canonical_mutation_deliveries"] == (
            histories * deliveries_per_history
        )
        assert run["actual_clean_rebuild_count"] == histories * rebuilds_per_history
        assert run["actual_repository_restart_count"] == histories
        assert run["actual_alternative_order_history_count"] == histories
        assert len(run["history_wall_clock_seconds"]) == histories
        assert all(value >= 0 for value in run["history_wall_clock_seconds"])
    assert general["actual_accepted_deletion_count"] == general["actual_history_count"]
    assert deletion["actual_deletion_delivery_count"] == (
        deletion["actual_history_count"] * 3
    )
    assert deletion["actual_accepted_deletion_count"] == (
        deletion["actual_history_count"] * 2
    )
    mix = evidence["profile_mix"]
    assert mix["actual_general_to_deletion_ratio"] == mix[
        "research_contract_general_to_deletion_ratio"
    ]

    falsifier = evidence["deliberate_falsifier"]["measured"]
    assert (
        falsifier["status"]
        == "expected_failure_with_generate_and_shrink_configured"
    )
    assert falsifier["actual_generated_call_count"] > 0
    assert falsifier["wall_clock_seconds"] >= 0
    assert falsifier["shrink_phase_seconds"] is None
    assert falsifier["shrink_phase_reason"]

    qualifier = QUALIFIER.read_text(encoding="utf-8")
    harness = HARNESS.read_text(encoding="utf-8")
    assert "ANALYTICS_CONVERGENCE_METRICS_PATH" in qualifier
    assert "subprocess.run" in qualifier
    assert "ANALYTICS_CONVERGENCE_METRICS_PATH" in harness
    assert "_record_generated_history" in harness
    assert "_record_deliberate_falsifier" in harness
