"""Guard the checked-in Task 5B local-evidence schema against empty claims."""
from __future__ import annotations

import json
from pathlib import Path


EVIDENCE = Path(__file__).resolve().parents[1] / "docs" / "architecture" / "task5b-agent-local-evidence.json"


def test_agent_local_evidence_has_real_profile_counts_and_no_invented_percentiles() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == 2
    assert {"os", "python_version", "hypothesis_version", "node_version"} <= evidence["runner"].keys()
    assert 0.40 <= evidence["deletion_history_fraction"] <= 0.50
    profiles = {profile["profile"]: profile for profile in evidence["profiles"]}
    assert set(profiles) == {"general", "deletion"}
    for profile in profiles.values():
        assert profile["histories_executed"] > 0
        assert profile["driver_transitions_executed"] >= profile["histories_executed"]
        assert profile["node_processes_started"] == profile["histories_executed"]
        for counter in ("reconnect_operations", "restart_operations", "snapshot_operations", "deletion_operations", "sync_required_operations", "same_client_reconnect_operations", "partial_snapshot_restart_operations", "frame_comparisons"):
            assert isinstance(profile[counter], int) and profile[counter] >= 0
        assert profile["sync_required_operations"] > 0
        assert profile["same_client_reconnect_operations"] > 0
        assert profile["frame_comparisons"] >= profile["driver_transitions_executed"]
        assert isinstance(profile["wall_seconds"], float) and profile["wall_seconds"] > 0
        assert profile["runner_percentiles"] is None
        assert isinstance(profile["runner_percentiles_reason"], str) and profile["runner_percentiles_reason"]
    assert profiles["deletion"]["partial_snapshot_restart_operations"] > 0
    probe = evidence["falsifier_probe"]
    assert probe["falsifier"] == "BrokenNoopAllocatesSequence" and probe["detected"] is True
    assert probe["shrink_phase_seconds"] is None and probe["shrink_phase_reason"]
    assert probe["minimized_failure_record"] == "tests/fixtures/agent-delivery-minimized-frame-failure.json"
    assert probe["minimized_failure_command_count"] > 0 and probe["minimized_failure_frame_count"] > 0
