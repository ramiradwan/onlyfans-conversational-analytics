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
    if not isinstance(files, dict) or not files or any(type(value) is not int or value not in (1, 2) for value in files.values()):
        raise WindowsShardError("Every full Windows test file must belong to shard 1 or 2")
    for name in files:
        path = PurePosixPath(name)
        if (not isinstance(name, str) or "\\" in name or ":" in name or path.is_absolute()
                or ".." in path.parts or str(path) != name or path.suffix != ".py"):
            raise WindowsShardError(f"Invalid full Windows test file path: {name!r}")
    if set(files.values()) != {1, 2}:
        raise WindowsShardError("Both full Windows shards must have test files")
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
    if type(shard) is not int or shard not in (1, 2):
        raise WindowsShardError("Full Windows shard must be 1 or 2")
    rows = list(collected)
    validate_inventory(rows, manifest, complete=complete)
    return {nodeid for nodeid in legacy_nodeids(rows) if manifest["files"][node_path(nodeid)] == shard}


def read_inventory(path: Path, *, execution: bool = False) -> dict[str, Any]:
    """Accept full raw scope only, even when a producer did not flag narrowing."""
    raw = path.read_bytes()
    report = json.loads(raw, object_pairs_hook=_unique_pairs)
    if (not isinstance(report, dict) or report.get("schema") != "ci-test-report/v1" or report.get("complete") is not True
            or type(report.get("exit_code")) is not int or report["exit_code"] != 0
            or type(report.get("collect_only")) is not bool or type(report.get("partial")) is not bool
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
    report["_report_sha256"] = hashlib.sha256(raw).hexdigest()
    return report


def file_timings(report: dict[str, Any]) -> dict[str, float]:
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


def updated_manifest(manifest: dict[str, Any] | None, inventory: dict[str, Any], *,
                     timing_report: dict[str, Any] | None = None, rebalance: bool = False) -> dict[str, Any]:
    """Preserve reviewed owners; explicitly identify newly unmeasured files."""
    files = {node_path(nodeid) for nodeid in legacy_nodeids(inventory["collected"])}
    prior = manifest or {"files": {}, "assignment_basis": {}}
    basis = prior.get("assignment_basis", {})
    timings = file_timings(timing_report) if timing_report is not None else basis.get("timing_seconds", {})
    timings = {path: seconds for path, seconds in timings.items() if path in files}
    if any(type(value) not in (int, float) or value < 0 or not math.isfinite(value) for value in timings.values()):
        raise WindowsShardError("Timing values must be finite non-negative seconds")
    if rebalance and set(timings) != files:
        raise WindowsShardError("Rebalancing requires measured Windows timing seconds for every current file")
    assignments = {} if rebalance else {path: owner for path, owner in prior["files"].items() if path in files}
    fallback = statistics.median(timings.values()) if timings else 1.0
    weight = lambda path: timings.get(path, fallback)
    loads = {shard: sum(weight(path) for path, owner in assignments.items() if owner == shard) for shard in (1, 2)}
    for path in sorted(files - assignments.keys(), key=lambda name: (-weight(name), name)):
        shard = min(loads, key=lambda owner: (loads[owner], owner))
        assignments[path] = shard
        loads[shard] += weight(path)
    source = (dict({key: timing_report[key] for key in ("source_commit", "workflow_run_id", "run_attempt")},
                   report_sha256=timing_report["_report_sha256"])
              if timing_report is not None else basis.get("source"))
    return dict(schema_version=1, files=dict(sorted(assignments.items())), assignment_basis=dict(
        status="measured" if set(timings) == files else "measured_with_unmeasured_additions" if timings else "unmeasured",
        method="file-duration longest-first allocation; existing owners retained unless --rebalance",
        source=source, timing_seconds=dict(sorted(timings.items())), unmeasured_files=sorted(files - timings.keys())))
