"""Collect measured local evidence for the Task 5B Agent state machines."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

import hypothesis


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "architecture" / "task5b-agent-local-evidence.json"
FAILURE_RECORD = ROOT / "tests" / "fixtures" / "agent-delivery-minimized-frame-failure.json"
TARGETS = {
    "general": "tests/stateful/test_agent_delivery.py::TestAgentDeliveryGeneral",
    "deletion": "tests/stateful/test_agent_delivery.py::TestAgentDeliveryDeletion",
}


def read_metrics(path: Path) -> list[dict[str, int]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def run_profile(profile: str, target: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="task5b-agent-") as directory:
        metrics_path = Path(directory) / "histories.jsonl"
        environment = {
            **os.environ,
            "HYPOTHESIS_PROFILE": f"agent_tier_a_{profile}",
            "AGENT_QUALIFICATION_METRICS_PATH": str(metrics_path),
        }
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "--override-ini=addopts=", "--basetemp", str(Path(directory) / "pytest"), target, "-q"],
            cwd=ROOT, env=environment, capture_output=True, text=True, check=False,
        )
        elapsed = time.perf_counter() - started
        if completed.returncode:
            raise RuntimeError(f"{profile} profile failed:\n{completed.stdout}\n{completed.stderr}")
        histories = read_metrics(metrics_path)
    if not histories:
        raise RuntimeError(f"{profile} profile did not report any completed history metrics")
    totals = {key: sum(history[key] for history in histories) for key in histories[0]}
    return {
        "profile": profile,
        "histories_executed": totals["histories_executed"],
        "driver_transitions_executed": totals["driver_transitions"],
        "node_processes_started": totals["node_processes"],
        "reconnect_operations": totals["reconnect_operations"],
        "restart_operations": totals["restart_operations"],
        "snapshot_operations": totals["snapshot_operations"],
        "deletion_operations": totals["deletion_operations"],
        "sync_required_operations": totals["sync_required_operations"],
        "same_client_reconnect_operations": totals["same_client_reconnect_operations"],
        "partial_snapshot_restart_operations": totals["partial_snapshot_restart_operations"],
        "frame_comparisons": totals["driver_transitions"],
        "wall_seconds": round(elapsed, 6),
        "runner_percentiles": None,
        "runner_percentiles_reason": "One local profile run has no sample distribution for p50/p90/p95.",
    }


def run_falsifier_probe() -> dict[str, object]:
    """Run the faulting Hypothesis test and require its minimized example."""
    environment = {**os.environ, "AGENT_FALSIFIER_PROBE": "1"}
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--override-ini=addopts=", "tests/stateful/test_agent_delivery.py::test_agent_falsifier_probe_shrinks_a_real_oracle_fault", "-q"],
        cwd=ROOT, env=environment, capture_output=True, text=True, check=False,
    )
    elapsed = time.perf_counter() - started
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 or "Falsifying example" not in output:
        raise RuntimeError(f"semantic falsifier probe did not fail and shrink as expected:\n{output}")
    record = json.loads(FAILURE_RECORD.read_text(encoding="utf-8"))
    return {
        "falsifier": "BrokenNoopAllocatesSequence",
        "detected": True,
        "wall_seconds": round(elapsed, 6),
        "shrink_phase_seconds": None,
        "shrink_phase_reason": "Hypothesis exposes the total test duration, not a separate first-failure versus shrinking timing API.",
        "minimized_failure_record": str(FAILURE_RECORD.relative_to(ROOT)).replace("\\", "/"),
        "minimized_failure_command_count": len(record["commands"]),
        "minimized_failure_frame_count": len(record["actual_frames"]),
    }


def node_version() -> str:
    return subprocess.check_output(["node", "--version"], text=True).strip()


def main() -> None:
    profiles = [run_profile(name, target) for name, target in TARGETS.items()]
    all_histories = sum(profile["histories_executed"] for profile in profiles)
    deletion_histories = next(profile["histories_executed"] for profile in profiles if profile["profile"] == "deletion")
    evidence = {
        "schema_version": 2,
        "scope": "Task 5B local Agent durable-delivery evidence",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runner": {
            "os": platform.platform(),
            "python_version": sys.version.split()[0],
            "hypothesis_version": hypothesis.__version__,
            "node_version": node_version(),
        },
        "profiles": profiles,
        "deletion_history_fraction": deletion_histories / all_histories,
        "falsifier_probe": run_falsifier_probe(),
        "limitations": [
            "Local FakeIndexedDb evidence drives real DurableIngestOutbox and AgentWebSocketClient but is not a browser-storage performance benchmark.",
            "No PR-runner percentile is claimed from one local sample.",
        ],
    }
    OUTPUT.write_text(f"{json.dumps(evidence, indent=2, sort_keys=True)}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
