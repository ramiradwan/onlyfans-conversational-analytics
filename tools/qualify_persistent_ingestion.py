"""Capture dated Tier B SQLite/SQLCipher runtime evidence as JSON.

This records local evidence only.  It intentionally never labels a run
production-equivalent: that requires a reproducible shipped fixed runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3 as stdlib_sqlite
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("WEBSOCKET_AUTH_MODE", "development_stub")

from app.persistence.database import CanonicalSQLite
from app.persistence import sqlite_api as sqlite3
from hypothesis import HealthCheck, settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule, run_state_machine_as_test
from tests.state_models.brain_ingestion_model import ModelSnapshotBeginCommand
from tests.state_models.transition_oracle import OracleMismatchError
from tests.stateful.test_brain_persistent_ingestion import PersistentHarness
from tests.state_models.sqlite_brain_adapter import BrokenReopenAdapter


def _measure_broken_reopen_shrink() -> dict[str, object]:
    """Exercise the falsifier through Hypothesis without inventing shrink timing.

    ``run_state_machine_as_test`` exposes only the whole invocation.  It does
    not publish a first-failure callback or a post-first-failure shrink timer,
    so the two phases must remain explicitly unavailable instead of assigning
    the same total duration to both.
    """
    with TemporaryDirectory(prefix="persistent_ingestion-shrink-") as directory:
        class BrokenReopenMachine(RuleBasedStateMachine):
            @initialize()
            def start(self) -> None:
                self.case_directory = TemporaryDirectory(prefix="persistent_ingestion-shrink-case-")
                self.harness = PersistentHarness(Path(self.case_directory.name), "benchmark-broken", BrokenReopenAdapter)
                self.harness.seed()

            def teardown(self) -> None:
                self.harness.adapter.close()
                self.case_directory.cleanup()

            @rule()
            def reopen_loses_checkpoint(self) -> None:
                self.harness.adapter.close_and_reopen()
                snapshot_id = UUID(self.harness.trace[0]["command"]["snapshot_id"])
                self.harness.frame(ModelSnapshotBeginCommand(snapshot_id, 1, 1, 1, 0, 0), "begin_snapshot")

        started = perf_counter()
        try:
            run_state_machine_as_test(BrokenReopenMachine, settings=settings(max_examples=2, stateful_step_count=2, deadline=None, suppress_health_check=[HealthCheck.too_slow]))
        except OracleMismatchError:
            duration = round(perf_counter() - started, 6)
            unavailable = (
                "Hypothesis run_state_machine_as_test does not expose a "
                "first-failure or post-first-failure shrink boundary"
            )
            return {
                "status": "failed_and_shrunk",
                "wall_clock_seconds": duration,
                "generation_to_first_failure_seconds": None,
                "generation_to_first_failure_reason": unavailable,
                "shrink_duration_seconds": None,
                "shrink_duration_reason": unavailable,
                "executed_transition_count": 1,
            }
    raise RuntimeError("BrokenReopenAdapter did not falsify the persistent oracle")


def _run_profile(profile: str, target: str) -> dict[str, object]:
    with TemporaryDirectory(prefix=f"persistent_ingestion-{profile}-") as directory:
        root = Path(directory)
        metrics = root / "metrics.json"
        env = dict(os.environ, HYPOTHESIS_PROFILE=profile, PERSISTENT_INGESTION_METRICS_PATH=str(metrics))
        started = perf_counter()
        result = subprocess.run([sys.executable, "-m", "pytest", "--basetemp", str(root / "pytest"), "--override-ini=addopts=", target, "-q"], cwd=ROOT, env=env, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"{profile} failed:\n{result.stdout}\n{result.stderr}")
        measured = json.loads(metrics.read_text(encoding="utf-8"))
        return {"wall_clock_seconds": round(perf_counter() - started, 6), **measured}


def collect() -> dict[str, object]:
    started = perf_counter()
    general = _run_profile("tier_b_general", "tests/stateful/test_brain_persistent_ingestion.py::TestPersistentGeneral")
    deletion = _run_profile("tier_b_deletion", "tests/stateful/test_brain_persistent_ingestion.py::TestPersistentDeletion")
    smoke = _run_profile("windows_persistence_smoke", "tests/stateful/test_brain_persistent_ingestion.py::TestWindowsProductionPersistenceSmoke")
    with TemporaryDirectory(prefix="persistent_ingestion-runtime-") as directory:
        database = CanonicalSQLite(Path(directory) / "canonical.sqlite3")
        with database.read() as connection:
            profile = {
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
                "synchronous": int(connection.execute("PRAGMA synchronous").fetchone()[0]),
                "foreign_keys": int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                "busy_timeout_ms": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
                "cipher_memory_security": str(connection.execute("PRAGMA cipher_memory_security").fetchone()[0]),
                "cipher_plaintext_header_size": str(connection.execute("PRAGMA cipher_plaintext_header_size").fetchone()[0]),
            }
            cipher = connection.execute("PRAGMA cipher_version").fetchone()[0]
    return {
        "schema_version": 1,
        "kind": "persistent-ingestion-local-runtime-probe",
        "status": "blocked_unfixed_runtime",
        "qualification_claim": "file-backed semantic evidence only; not production-equivalent",
        "wall_clock_seconds": round(perf_counter() - started, 6),
        "runner": {"os": platform.platform(), "python": sys.version, "sqlite": sqlite3.sqlite_version,
                   "stdlib_sqlite": stdlib_sqlite.sqlite_version, "sqlcipher": str(cipher)},
        "profile": profile,
        "connection_model": "fresh check_same_thread=False SQLCipher connections; BEGIN IMMEDIATE writer transactions",
        "checkpoint_behavior": "SQLite automatic WAL checkpoint at default threshold and last-close checkpoint; no trigger exclusion claimed",
        "benchmark": {"tier_b_general": {"examples": 15, "steps": 25, "executed_transition_count": general["transitions"], "history_count": general["histories"], "reopen_count": general["reopens"], "wall_clock_seconds": general["wall_clock_seconds"]}, "tier_b_deletion": {"examples": 10, "steps": 20, "executed_transition_count": deletion["transitions"], "history_count": deletion["histories"], "reopen_count": deletion["reopens"], "wall_clock_seconds": deletion["wall_clock_seconds"]}, "windows_smoke": {"examples": 5, "steps": 12, "executed_transition_count": smoke["transitions"], "history_count": smoke["histories"], "reopen_count": smoke["reopens"], "wall_clock_seconds": smoke["wall_clock_seconds"]}, "passing_run": {"status": "local_pass", "duration_seconds": general["wall_clock_seconds"]}, "failing_shrink_run": _measure_broken_reopen_shrink(), "pr_runner_baseline_status": {"status": "pending", "reason": "local runs are not representative PR-runner measurements"}},
        "advisories": ["https://sqlite.org/wal.html#walresetbug", "https://www.zetetic.net/sqlcipher/changelog/", "https://pypi.org/project/sqlcipher3/"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(collect(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
