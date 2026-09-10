"""Schema guard for the generated, explicitly local Task 5C evidence file."""

from __future__ import annotations

import json
from pathlib import Path


EVIDENCE = Path(__file__).resolve().parents[1] / "docs" / "architecture" / "task5c-local-runtime-evidence.json"


def test_tier_b_runtime_evidence_has_required_local_benchmark_fields() -> None:
    data = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["status"] == "blocked_unfixed_runtime"
    assert "not production-equivalent" in data["qualification_claim"]
    assert data["profile"] == {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1,
                               "busy_timeout_ms": 5000, "cipher_memory_security": "0",
                               "cipher_plaintext_header_size": "0"}
    benchmark = data["benchmark"]
    for profile, examples, steps in (("tier_b_general", 15, 25), ("tier_b_deletion", 10, 20), ("windows_smoke", 5, 12)):
        entry = benchmark[profile]
        assert entry["examples"] == examples and entry["steps"] == steps
        assert isinstance(entry["wall_clock_seconds"], float)
        assert isinstance(entry["history_count"], int) and entry["history_count"] >= examples
        assert isinstance(entry["executed_transition_count"], int) and entry["executed_transition_count"] >= entry["history_count"]
        assert isinstance(entry["reopen_count"], int)
    shrink = benchmark["failing_shrink_run"]
    assert shrink["status"] == "failed_and_shrunk"
    assert isinstance(shrink["wall_clock_seconds"], float)
    assert shrink["generation_to_first_failure_seconds"] is None
    assert "does not expose" in shrink["generation_to_first_failure_reason"]
    assert shrink["shrink_duration_seconds"] is None
    assert "does not expose" in shrink["shrink_duration_reason"]
    assert shrink["executed_transition_count"] >= 1
    assert benchmark["pr_runner_baseline_status"]["status"] == "pending"
