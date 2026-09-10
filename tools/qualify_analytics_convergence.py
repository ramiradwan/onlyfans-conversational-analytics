"""Record local analytics-convergence evidence as JSON."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
GENERAL_PROFILE = "analytics_convergence_fast"
DELETION_PROFILE = "analytics_deletion_fast"
RESEARCH_GENERAL_EXAMPLES = 30
RESEARCH_DELETION_EXAMPLES = 20
GENERAL_TARGET = (
    "tests/stateful/test_analytics_equivalence.py::TestAnalyticsConvergence"
)
DELETION_TARGET = (
    "tests/stateful/test_analytics_equivalence.py::TestAnalyticsDeletionConvergence"
)
FALSIFIER_TARGET = (
    "tests/stateful/test_analytics_equivalence.py::"
    "test_deliberate_falsifier_failure_configures_hypothesis_shrink_phase"
)


def _run_target(
    *,
    name: str,
    target: str,
    profile: str | None,
) -> tuple[dict[str, Any], float]:
    """Run one test and return its runtime metrics."""

    with TemporaryDirectory(prefix=f"analytics_convergence-{name}-") as directory:
        root = Path(directory)
        metrics = root / "metrics.json"
        environment = dict(os.environ, ANALYTICS_CONVERGENCE_METRICS_PATH=str(metrics))
        if profile is not None:
            environment["HYPOTHESIS_PROFILE"] = profile
        started = perf_counter()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--basetemp",
                str(root / "pytest"),
                "--override-ini=addopts=",
                target,
                "-q",
                "--hypothesis-show-statistics",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        elapsed = round(perf_counter() - started, 6)
        if result.returncode:
            raise RuntimeError(
                f"{name} qualification failed:\n{result.stdout}\n{result.stderr}"
            )
        if not metrics.exists():
            raise RuntimeError(f"{name} did not emit runtime metrics")
        return json.loads(metrics.read_text(encoding="utf-8")), elapsed


def _measured_profile(
    document: dict[str, Any], suite: str) -> dict[str, Any]:
    run = document["runs"].get(suite)
    if not isinstance(run, dict) or int(run.get("actual_history_count", 0)) <= 0:
        raise RuntimeError(f"missing measured {suite} convergence history data")
    histories = int(run["actual_history_count"])
    if len(run["history_wall_clock_seconds"]) != histories:
        raise RuntimeError(f"{suite} convergence timing count is incomplete")
    return run


def collect() -> dict[str, Any]:
    """Measure CI profiles and the expected-failure check."""

    general_document, general_process_seconds = _run_target(
        name="general", target=GENERAL_TARGET, profile=GENERAL_PROFILE
    )
    deletion_document, deletion_process_seconds = _run_target(
        name="deletion", target=DELETION_TARGET, profile=DELETION_PROFILE
    )
    falsifier_document, falsifier_process_seconds = _run_target(
        name="falsifier", target=FALSIFIER_TARGET, profile=DELETION_PROFILE
    )
    general = _measured_profile(general_document, "general")
    deletion = _measured_profile(deletion_document, "deletion")
    falsifier = falsifier_document.get("deliberate_falsifier")
    if not isinstance(falsifier, dict) or int(
        falsifier.get("actual_generated_call_count", 0)
    ) <= 0:
        raise RuntimeError("deliberate convergence falsifier did not record generated work")
    if general_document["runtime"] != deletion_document["runtime"]:
        raise RuntimeError("runtime changed between convergence profile measurements")

    actual_ratio = general["actual_history_count"] / deletion["actual_history_count"]
    research_contract_ratio = RESEARCH_GENERAL_EXAMPLES / RESEARCH_DELETION_EXAMPLES
    if actual_ratio != research_contract_ratio:
        raise RuntimeError(
            "actual convergence CI profile ratio differs from the configured 30:20 ratio"
        )
    return {
        "schema_version": "analytics-convergence-local-evidence.v2",
        "evidence_origin": "tools/qualify_analytics_convergence.py",
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "canonical_authority": (
                "temporary file-backed SQLCipher canonical and repository projection databases "
                "created by create_canonical_repositories('memory') and exercised "
                "through HistoryRepository"
            ),
            "derived_stores": (
                "fresh in-process InMemoryAnalyticsProjectionStore and "
                "InMemoryGraphRepository for each incremental or clean pipeline"
            ),
        },
        "runtime": general_document["runtime"],
        "profiles": {
            GENERAL_PROFILE: {
                "target": GENERAL_TARGET,
                "process_wall_clock_seconds": general_process_seconds,
                "measured": general,
            },
            DELETION_PROFILE: {
                "target": DELETION_TARGET,
                "process_wall_clock_seconds": deletion_process_seconds,
                "measured": deletion,
            },
        },
        "profile_mix": {
            "research_contract_general_examples": RESEARCH_GENERAL_EXAMPLES,
            "research_contract_deletion_examples": RESEARCH_DELETION_EXAMPLES,
            "actual_general_histories": general["actual_history_count"],
            "actual_deletion_histories": deletion["actual_history_count"],
            "research_contract_general_to_deletion_ratio": research_contract_ratio,
            "actual_general_to_deletion_ratio": actual_ratio,
        },
        "deliberate_falsifier": {
            "target": FALSIFIER_TARGET,
            "process_wall_clock_seconds": falsifier_process_seconds,
            "measured": falsifier,
        },
        "limitations": [
            "Local timing is not hosted-runner p95 evidence.",
            "Temporary canonical SQLCipher files do not qualify shipped fixed-runtime persistence.",
            "Fresh in-memory derived stores do not qualify persistent projection/graph restart behavior.",
            "No live creator data, external enrichment, or production traffic trace was used.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = collect()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
