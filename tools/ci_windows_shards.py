"""Independent file partition of the frozen, exhaustive Windows pytest selection.

This module deliberately has no dependency on analytics tiers or their manifest.
"""
from __future__ import annotations

import json
import hashlib
import math
import statistics
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


LEGACY_EXCLUDED = frozenset({"slow", "stateful_tier_a", "stateful_tier_b", "stateful_agent_tier_a"})
LEGACY_WINDOWS_EXPRESSION = "not slow and not stateful_tier_a and not stateful_tier_b and not stateful_agent_tier_a"
SHARDS = (1, 2, 3)
SHARD_LANES = tuple(f"windows-full-regression-{shard}" for shard in SHARDS)


class WindowsShardError(ValueError):
    """The independent Windows partition is invalid or incomplete."""


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WindowsShardError(f"Duplicate Windows shard manifest key: {key}")
        result[key] = value
    return result


def load_manifest(rootpath: str | Path, path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else Path(rootpath) / "ci/windows-full-test-shards.json"
    if not target.is_absolute():
        target = Path(rootpath) / target
    document = json.loads(target.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise WindowsShardError("Unsupported full Windows shard manifest schema")
    files = document.get("files")
    if not isinstance(files, dict) or not files or any(type(value) is not int or value not in SHARDS for value in files.values()):
        raise WindowsShardError("Every full Windows test file must belong to shard 1, 2 or 3")
    for name in files:
        path = PurePosixPath(name)
        if (not isinstance(name, str) or "\\" in name or ":" in name or path.is_absolute()
                or ".." in path.parts or str(path) != name or path.suffix != ".py"):
            raise WindowsShardError(f"Invalid full Windows test file path: {name!r}")
    if set(files.values()) != set(SHARDS):
        raise WindowsShardError("Every full Windows shard must have test files")
    return document


def legacy_nodeids(collected: Iterable[dict[str, Any]]) -> set[str]:
    """Use only the original marker predicate; preserve parameter IDs exactly."""
    return {row["nodeid"] for row in collected if not LEGACY_EXCLUDED.intersection(row["markers"])}


def node_path(nodeid: str) -> str:
    return nodeid.split("::", 1)[0].replace("\\", "/")


def validate_inventory(collected: Iterable[dict[str, Any]], manifest: dict[str, Any], *, complete: bool = True) -> None:
    rows = list(collected)
    nodeids = [row["nodeid"] for row in rows]
    if len(nodeids) != len(set(nodeids)):
        raise WindowsShardError("Duplicate node IDs in full Windows collection")
    actual = {node_path(nodeid) for nodeid in legacy_nodeids(rows)}
    assigned = set(manifest["files"])
    unknown, missing = actual - assigned, assigned - actual if complete else set()
    if unknown or missing:
        details = []
        if unknown:
            details.append("unassigned files: " + ", ".join(sorted(unknown)))
        if missing:
            details.append("missing files: " + ", ".join(sorted(missing)))
        raise WindowsShardError("Full Windows shard inventory differs (" + "; ".join(details) +
            "). Collect the raw unsharded lane, then run python tools/test_backend.py update-windows-manifest --inventory <report.json>; review the change.")


def selected_nodeids(collected: Iterable[dict[str, Any]], manifest: dict[str, Any], shard: int, *, complete: bool = True) -> set[str]:
    if type(shard) is not int or shard not in SHARDS:
        raise WindowsShardError("Full Windows shard must be 1, 2 or 3")
    rows = list(collected)
    validate_inventory(rows, manifest, complete=complete)
    return {nodeid for nodeid in legacy_nodeids(rows) if manifest["files"][node_path(nodeid)] == shard}


def read_inventory(path: Path, *, execution: bool = False) -> dict[str, Any]:
    """Accept full raw scope only, even when a producer did not flag narrowing.

    For timings, a directory holding every exhaustive shard execution report
    of one run is joined into one full-scope report.
    """
    if execution and path.is_dir():
        return combine_shard_reports(sorted(path.rglob("report.json")))
    raw = path.read_bytes()
    report = json.loads(raw, object_pairs_hook=_unique_pairs)
    _check_scope(report, execution=execution)
    report["_report_sha256"] = hashlib.sha256(raw).hexdigest()
    return report


def combine_shard_reports(paths: Iterable[Path]) -> dict[str, Any]:
    """Join every successful exhaustive shard execution of one run.

    Evidence from a run with fewer shards remains usable when its reports
    together select the full scope exactly once.
    """
    shards = {}
    for path in paths:
        raw = path.read_bytes()
        report = json.loads(raw, object_pairs_hook=_unique_pairs)
        if isinstance(report, dict) and report.get("lane") in SHARD_LANES:
            if report["lane"] in shards:
                raise WindowsShardError(f"Duplicate Windows timing report for {report['lane']}")
            shards[report["lane"]] = (report, hashlib.sha256(raw).hexdigest())
    lanes = [lane for lane in SHARD_LANES if lane in shards]
    if not lanes:
        raise WindowsShardError("Timings require every full Windows shard execution report of one run")
    reports = [shards[lane][0] for lane in lanes]
    first = reports[0]
    if any(first.get(key) != report.get(key) for report in reports[1:]
           for key in ("source_commit", "workflow_run_id", "run_attempt", "collected")):
        raise WindowsShardError("Full Windows shard reports come from different runs or collections")
    selected = [node for report in reports for node in report.get("selected", [])]
    nodes = [row["nodeid"] for row in first.get("collected", [])]
    combined = dict(first, lane="windows-full-regression", selected=selected,
                    deselected=[node for node in nodes if node not in set(selected)],
                    reports=[phase for report in reports for phase in report.get("reports", [])],
                    exit_code=0 if all(report.get("exit_code") == 0 for report in reports) else 1,
                    complete=all(report.get("complete") is True for report in reports))
    _check_scope(combined, execution=True)
    combined["_report_sha256"] = shards[lanes[0]][1]
    combined["_execution_reports"] = [{"lane": lane, "report_sha256": shards[lane][1]} for lane in lanes]
    return combined


def _check_scope(report: Any, *, execution: bool) -> None:
    if (not isinstance(report, dict) or report.get("schema") != "ci-test-report/v1" or report.get("complete") is not True
            or type(report.get("exit_code")) is not int or report["exit_code"] != 0
            or type(report.get("collect_only")) is not bool or report.get("partial") is not False
            or report.get("platform") != "Windows"
            or report.get("collection_errors") != [] or report.get("profile") != "" or report.get("selection_profile") != ""):
        raise WindowsShardError("Inventory requires a complete successful unprofiled CI report")
    selection = report.get("selection", {})
    if (selection.get("keyword") or selection.get("marker") not in ("", LEGACY_WINDOWS_EXPRESSION)
            or selection.get("targets") not in (["tests"], ["."])):
        raise WindowsShardError("Inventory must collect the full test tree without narrowed targets or markers")
    rows = report.get("collected", [])
    expected = legacy_nodeids(rows)
    selected = report.get("selected", [])
    if (not expected or len(rows) != len({row["nodeid"] for row in rows})
            or len(selected) != len(set(selected)) or set(selected) != expected):
        raise WindowsShardError("Inventory must select exactly the raw legacy Windows node IDs")
    if execution and (report.get("platform") != "Windows" or report.get("collect_only") is not False):
        raise WindowsShardError("Timings require a complete successful Windows execution report")
    deselected = report.get("deselected", [])
    if len(deselected) != len(set(deselected)) or set(deselected) != {row["nodeid"] for row in rows} - expected:
        raise WindowsShardError("Inventory deselection must match only the frozen legacy exclusions")


def file_timings(report: dict[str, Any], *, only: Iterable[str] | None = None) -> dict[str, float]:
    """Sum measured phase seconds per file, optionally of the given node IDs only."""
    only = None if only is None else set(only)
    expected = legacy_nodeids(report["collected"])
    phases: dict[str, dict[str, Any]] = {}
    children: dict[str, list[int]] = {}
    totals = {node_path(nodeid): 0.0 for nodeid in expected}
    for phase in report.get("reports", []):
        nodeid, when = phase["nodeid"], phase["when"]
        if nodeid not in expected or when not in ("setup", "call", "teardown"):
            raise WindowsShardError("Unexpected phase in Windows timing evidence")
        duration = phase["duration"]
        if type(duration) not in (int, float) or duration < 0 or not math.isfinite(duration):
            raise WindowsShardError("Timing values must be finite non-negative seconds")
        if "subtest" in phase:
            child = phase["subtest"]
            if (phase.get("outcome") != "passed" or when != "call" or not isinstance(child, dict)
                    or set(child) != {"index", "msg", "kwargs"} or type(child["index"]) is not int
                    or child["index"] < 1 or child["msg"] is not None and not isinstance(child["msg"], str)
                    or not isinstance(child["kwargs"], dict)
                    or any(not isinstance(key, str) or not isinstance(value, str) for key, value in child["kwargs"].items())):
                raise WindowsShardError("Malformed or unsuccessful child subtest in Windows timing evidence")
            children.setdefault(nodeid, []).append(child["index"])
            continue
        by_phase = phases.setdefault(nodeid, {})
        if when in by_phase or phase["outcome"] not in ("passed", "skipped"):
            raise WindowsShardError("Duplicate or unsuccessful phase in Windows timing evidence")
        by_phase[when] = phase
        if only is None or nodeid in only:
            totals[node_path(nodeid)] += duration
    for nodeid in expected:
        observed = phases.get(nodeid, {})
        if set(observed) not in ({"setup", "call", "teardown"}, {"setup", "teardown"}):
            raise WindowsShardError(f"Incomplete Windows timing evidence: {nodeid}")
        if "call" not in observed and observed["setup"]["outcome"] != "skipped":
            raise WindowsShardError(f"Missing call without setup skip: {nodeid}")
        if observed["teardown"]["outcome"] != "passed":
            raise WindowsShardError(f"Windows timing evidence requires successful teardown: {nodeid}")
        if "call" in observed and observed["setup"]["outcome"] != "passed":
            raise WindowsShardError(f"Windows timing evidence cannot call after skipped setup: {nodeid}")
        if nodeid in children and (sorted(children[nodeid]) != list(range(1, len(children[nodeid]) + 1))
                                   or observed.get("call", {}).get("outcome") != "passed"):
            raise WindowsShardError(f"Child subtests require contiguous indices and a successful parent: {nodeid}")
    return totals


def phase_model(workers: Any, contention: Any) -> dict[str, Any]:
    """Expected job seconds: parallel tests share workers at a measured slowdown."""
    if (type(workers) is not int or workers < 1 or type(contention) not in (int, float)
            or not math.isfinite(contention) or contention < 1):
        raise WindowsShardError("Phase model needs workers >= 1 and contention >= 1")
    return {"workers": workers, "contention": contention}


def updated_manifest(manifest: dict[str, Any] | None, inventory: dict[str, Any], *,
                     timing_report: dict[str, Any] | None = None, rebalance: bool = False,
                     model: dict[str, Any] | None = None) -> dict[str, Any]:
    """Preserve reviewed owners; explicitly identify newly unmeasured files.

    With a phase model, each file weighs its serial-marked test seconds plus
    its other test seconds times contention divided by workers. Serial tests
    are those the current inventory marks serial.
    """
    files = {node_path(nodeid) for nodeid in legacy_nodeids(inventory["collected"])}
    prior = manifest or {"files": {}, "assignment_basis": {}}
    basis = prior.get("assignment_basis", {})
    if model is None and timing_report is None:
        model = basis.get("phase_model")
    if model is not None:
        model = phase_model(model.get("workers"), model.get("contention"))
    serial_nodes = {row["nodeid"] for row in inventory["collected"] if "serial" in row["markers"]}
    if timing_report is not None:
        timings = file_timings(timing_report)
        serial = file_timings(timing_report, only=serial_nodes) if model else {}
    else:
        timings, serial = basis.get("timing_seconds", {}), basis.get("serial_seconds", {}) if model else {}
    timings = {path: seconds for path, seconds in timings.items() if path in files}
    serial = {path: seconds for path, seconds in serial.items() if path in timings and seconds}
    if any(type(value) not in (int, float) or value < 0 or not math.isfinite(value)
           for value in (*timings.values(), *serial.values())):
        raise WindowsShardError("Timing values must be finite non-negative seconds")
    if any(seconds > timings[path] for path, seconds in serial.items()):
        raise WindowsShardError("Serial seconds cannot exceed a file's measured seconds")
    if rebalance and set(timings) != files:
        raise WindowsShardError("Rebalancing requires measured Windows timing seconds for every current file")
    assignments = {} if rebalance else {path: owner for path, owner in prior["files"].items() if path in files}
    def expected(path: str) -> float:
        seconds, serial_seconds = timings[path], serial.get(path, 0.0)
        if model is None:
            return seconds
        return (seconds - serial_seconds) * model["contention"] / model["workers"] + serial_seconds
    measured = {path: expected(path) for path in timings}
    fallback = statistics.median(measured.values()) if measured else 1.0
    weight = lambda path: measured.get(path, fallback)
    loads = {shard: sum(weight(path) for path, owner in assignments.items() if owner == shard) for shard in SHARDS}
    for path in sorted(files - assignments.keys(), key=lambda name: (-weight(name), name)):
        shard = min(loads, key=lambda owner: (loads[owner], owner))
        assignments[path] = shard
        loads[shard] += weight(path)
    source = (dict({key: timing_report[key] for key in ("source_commit", "workflow_run_id", "run_attempt")},
                   report_sha256=timing_report["_report_sha256"],
                   **({"execution_reports": timing_report["_execution_reports"]} if "_execution_reports" in timing_report else {}))
              if timing_report is not None else basis.get("source"))
    phase_fields = {} if model is None else dict(
        phase_model=model, serial_seconds=dict(sorted(serial.items())),
        expected_shard_seconds={str(shard): round(load, 1) for shard, load in sorted(loads.items())})
    return dict(schema_version=1, files=dict(sorted(assignments.items())), assignment_basis=dict(
        status="measured" if set(timings) == files else "measured_with_unmeasured_additions" if timings else "unmeasured",
        method=("expected job seconds (serial tests plus other tests times contention per worker)"
                if model else "file-duration")
               + " longest-first allocation; existing owners retained unless --rebalance",
        source=source, timing_seconds=dict(sorted(timings.items())), **phase_fields,
        unmeasured_files=sorted(files - timings.keys())))



def model_candidate_partitions(report: dict[str, Any], *, workers: int = 4,
                               contention: float = 1.0,
                               counts: Iterable[int] = range(3, 13)) -> list[dict[str, Any]]:
    """Estimate alternative file partitions without modifying the active manifest.

    Timings must be complete Windows execution evidence. The parallel component
    divides measured test-phase work by the worker count; serial work is never
    divided. Estimates exclude collection, setup outside pytest and queue time,
    and are not approval to change the required CI topology.
    """
    _check_scope(report, execution=True)
    model = phase_model(workers, contention)
    totals = file_timings(report)
    serial = file_timings(report, only={row["nodeid"] for row in report["collected"]
                                       if "serial" in row["markers"]})
    counts = list(counts)
    if (not counts or any(type(count) is not int or not 3 <= count <= 12 or count > len(totals)
                          for count in counts) or len(counts) != len(set(counts))):
        raise WindowsShardError("Candidate counts must be distinct integers from 3 to 12 with sufficient files")
    weights = {path: serial[path] + (seconds - serial[path]) * model["contention"] / model["workers"]
               for path, seconds in totals.items()}
    candidates = []
    for count in sorted(counts):
        loads = dict.fromkeys(range(1, count + 1), 0.0)
        assignments = {}
        for path in sorted(weights, key=lambda name: (-weights[name], name)):
            # Prefer an unused shard on ties, including all-zero timing reports.
            assigned = set(assignments.values())
            shard = min(loads, key=lambda owner: (loads[owner], owner in assigned, owner))
            assignments[path] = shard
            loads[shard] += weights[path]
        peak = max(loads.values())
        candidates.append(dict(shards=count, files=dict(sorted(assignments.items())),
                               expected_work_seconds=loads, peak_work_seconds=peak,
                               stress_1_5_work_seconds=peak * 1.5,
                               stress_1_7_work_seconds=peak * 1.7))
    return candidates
