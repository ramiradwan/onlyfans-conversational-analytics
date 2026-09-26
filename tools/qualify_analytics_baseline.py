"""Record local analytics regression results without claiming product capacity."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
UNIT_TESTS = [
    "test_analytics_analyzers.py", "test_analytics_backend_rework.py",
    "test_analytics_pipeline.py", "test_analytics_retention.py",
    "test_analytics_runtime.py", "test_graph_store_contract.py",
    "test_sqlite_graph_store.py", "test_sqlite_projection_store.py",
    "test_analysis_authorization.py", "test_licensed_analytics_pipeline.py",
    "test_historical_analytics_retention.py", "test_retention_participant_scope.py",
    "test_projection_backup_restore_retention.py", "test_creator_vault_retention.py",
    "test_retention_maintenance.py",
]
STATEFUL = "tests/stateful/"
RUNS = [
    ("analytics", None, ["tests/" + name for name in UNIT_TESTS]),
    ("questions", None, ["tests/" + name for name in (
        "test_analytics_question_cases.py", "test_analytics_question_contracts.py",
        "test_analytics_question_service.py", "test_analytics_query_handlers.py",
        "test_analytics_query_endpoints.py", "test_question_route_guards.py",
        "test_analytics_evidence.py", "test_readiness_identity.py",
    )]),
    ("determinism", "analytics_determinism_fast", [
        STATEFUL + "test_analytics_determinism.py",
    ]),
    ("convergence", "analytics_convergence_fast", [
        STATEFUL + "test_analytics_equivalence.py::TestAnalyticsConvergence",
    ]),
    ("deletion", "analytics_deletion_fast", [
        STATEFUL + "test_analytics_equivalence.py::TestAnalyticsDeletionConvergence",
    ]),
    ("falsifiers", "analytics_convergence_dev", [
        STATEFUL + "test_analytics_equivalence.py::test_analytics_convergence_falsifiers_reject_metric_provenance_identity_graph_and_deletion_faults",
    ]),
]


def git_output(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def counts(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None
    try:
        suites = list(ET.parse(path).getroot().iter("testsuite"))
        values = [{key: int(s.get(key, "0")) for key in ("tests", "failures", "errors", "skipped")} for s in suites]
        if any(any(v < 0 for v in row.values()) or row["failures"] + row["errors"] + row["skipped"] > row["tests"] for row in values):
            return None
    except (ET.ParseError, ValueError, OSError):
        return None
    return {key: sum(row[key] for row in values) for key in ("tests", "failures", "errors", "skipped")}


def baseline_passed(runs: list[dict], expected_runs: int) -> bool:
    if type(expected_runs) is not int or expected_runs <= 0 or len(runs) != expected_runs:
        return False
    for run in runs:
        if (not isinstance(run, dict) or type(run.get("exit_code")) is not int
                or run["exit_code"] != 0 or run.get("timed_out") is not False):
            return False
        stats = run.get("counts")
        if not isinstance(stats, dict) or any(type(stats.get(key)) is not int or stats[key] < 0
                for key in ("tests", "failures", "errors", "skipped")):
            return False
        if not (stats["tests"] > stats["skipped"] and stats["failures"] == stats["errors"] == 0):
            return False
    return True


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--ci-worker":
        sys.path.insert(0, str(ROOT))
        from tools.analytics_qualification_ci import main as ci_main
        return ci_main(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "--collector-worker":
        sys.path.insert(0, str(ROOT))
        from tools.analytics_qualification_worker import main as worker_main
        return worker_main(sys.argv[2])
    if "--closure" in sys.argv:
        sys.path.insert(0, str(ROOT))
        from tools.analytics_qualification_runner import main as closure_main
        return closure_main(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    revision = git_output("rev-parse", "HEAD")
    status = git_output("status", "--porcelain")
    if revision is None or status is None:
        parser.error("Git cannot identify this checkout; run from a native checkout for this Python runtime")
    output = args.output.resolve()
    if output.exists():
        parser.error("--output must name a new directory")
    output.mkdir(parents=True)
    packages = {}
    for name in ("pytest", "hypothesis", "pydantic", "fastapi", "sqlcipher3", "ofca-native-snow"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    report = {
        "schema": "analytics-baseline.v1", "source_revision": revision,
        "working_tree": status,
        "platform": platform.platform(), "python": sys.version,
        "packages": packages, "runs": [], "input_sha256": {},
        "complete": False, "passed": False, "expected_runs": len(RUNS),
    }
    for directory in ("app", "tests", "native/companion-snow"):
        for path in sorted((ROOT / directory).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".sql", ".json", ".rs", ".toml", ".lock"}:
                if "target" not in path.parts:
                    report["input_sha256"][path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("requirements.txt", "requirements-dev.txt", "pytest.ini",
                 "tools/qualify_analytics_baseline.py"):
        report["input_sha256"][name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    report_path = output / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for name, profile, tests in RUNS:
        env = os.environ.copy()
        env.pop("HYPOTHESIS_PROFILE", None)
        if profile:
            env["HYPOTHESIS_PROFILE"] = profile
        junit = output / (name + ".xml")
        command = [sys.executable, "-m", "pytest", "--override-ini=addopts=", "-q",
                   "--hypothesis-seed=20260918", "--hypothesis-show-statistics",
                   "--basetemp", str(output / (name + "-tmp")),
                   "--junitxml", str(junit), *tests]
        start = time.perf_counter()
        with (output / (name + ".log")).open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                        stderr=subprocess.STDOUT, timeout=args.timeout, check=False)
                code, timed_out = result.returncode, False
            except subprocess.TimeoutExpired:
                code, timed_out = None, True
        run = {"name": name, "tests": tests, "profile": profile, "seed": 20260918,
               "exit_code": code, "timed_out": timed_out,
               "seconds": round(time.perf_counter() - start, 3), "counts": counts(junit)}
        with (output / (name + ".result.json")).open("x", encoding="utf-8") as phase:
            phase.write(json.dumps(run, indent=2) + "\n")
            phase.flush()
            os.fsync(phase.fileno())
        report["runs"].append(run)
        report["complete"] = len(report["runs"]) == report["expected_runs"]
        report["passed"] = baseline_passed(report["runs"], report["expected_runs"])
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(run), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
