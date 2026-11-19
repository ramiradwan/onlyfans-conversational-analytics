"""Maintain the production Windows v9 allocation; historical v8 is separate."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path, PurePosixPath

from tools import ci_windows_shards as legacy
from tools.ci_selection import matches

PATH = Path("ci/windows-v9-test-shards.json")
COUNTS = {"full": 8, "platform": 5, "analytics": 2}
GROUPS = tuple(f"{kind}-{i}" for kind, count in COUNTS.items() for i in range(1, count + 1))
Error = legacy.WindowsShardError


def check_manifest(plan):
    from tools.ci_gate import PR_WINDOWS_SKIP_LANES
    if plan.get("schema") != 2 or legacy.candidate_counts(plan) != COUNTS:
        raise Error("Production v9 requires exactly 8 full, 5 platform and 2 analytics shards")
    retained = plan.get("retain_in_full")
    if not isinstance(retained, list) or any(not isinstance(n, str) for n in retained) or len(retained) != len(set(retained)) or set(retained) != set(PR_WINDOWS_SKIP_LANES):
        raise Error("Retained overlap must equal the reviewed Windows skip identities")
    for kind in COUNTS:
        for name in plan[kind]:
            path = PurePosixPath(name)
            if not name.startswith("tests/") or path.suffix != ".py" or ".." in path.parts or str(path) != name or "\\" in name or ":" in name:
                raise Error(f"Invalid production Windows file: {name}")
    return plan


def load_manifest(root, path=None):
    target = Path(path) if path is not None else PATH
    if not target.is_absolute():
        target = Path(root) / target
    return check_manifest(json.loads(target.read_text(encoding="utf-8"), object_pairs_hook=legacy._unique_pairs))


def groups(rows, plan, contracts, *, complete=True):
    check_manifest(plan)
    return legacy.candidate_groups(rows, plan, contracts, complete=complete)


def populations(rows, contracts, retained):
    raw = legacy.legacy_nodeids(rows)
    selected = {kind: defaultdict(set) for kind in COUNTS}
    for node in raw:
        file = legacy.node_path(node)
        focused = [kind for kind, entries in contracts.items() if any(matches(node, e["selector"]) for e in entries)]
        if len(focused) > 1:
            raise Error("Windows focused selector overlap")
        selected["full"].setdefault(file, set())
        if focused:
            selected[focused[0]][file].add(node)
        if not focused or node in retained:
            selected["full"][file].add(node)
    return selected


def read_timings(directory, inventory, plan, contracts):
    """Require a complete clean same-source, same-attempt 15-lane sample."""
    from tools import ci_gate as gate, ci_gate_v9 as v9
    expected = groups(inventory["collected"], plan, contracts)
    raw_signature = gate._raw_inventory_signature(gate.inventory(inventory))
    reports, digests = {}, {}
    identity = None
    for path in sorted(Path(directory).rglob("report.json")):
        raw = path.read_bytes()
        report = json.loads(raw, object_pairs_hook=legacy._unique_pairs)
        lane = report.get("lane", "")
        if lane not in v9.PARTITION:
            continue
        spec = v9.SPECS[lane]
        source, run, attempt = (report.get(k) for k in ("source_commit", "workflow_run_id", "run_attempt"))
        coords = (source, str(run), str(attempt))
        artifact = f"ci-tests-{lane}-{source}-{run}-{attempt}"
        if path.relative_to(directory).parts != (artifact, lane, "report.json") or lane in reports:
            raise Error("Wrong or duplicate timing artifact identity")
        if identity is None:
            identity = coords
        if coords != identity or source != inventory.get("source_commit") or not isinstance(source, str) or len(source) != 40 or not str(run).isdigit() or not str(attempt).isdigit():
            raise Error("Timing sample must match the raw inventory source and one run attempt")
        if report.get("job_id") != spec.logical_job or report.get("partial") is not False or report.get("selection") != {"keyword": "", "marker": legacy.LEGACY_WINDOWS_EXPRESSION, "targets": ["tests"]} or report.get("execution", {}).get("workers") != 4 or report.get("execution", {}).get("dist") != "load":
            raise Error("Timing producer, selection or execution configuration differs")
        if gate._raw_inventory_signature(gate.inventory(report)) != raw_signature:
            raise Error("Timing inventory differs from raw reference")
        if any(report.get(k) != inventory.get(k) for k in ("python", "python_implementation", "pytest")):
            raise Error("Timing runtime differs from raw reference")
        outcomes = gate.validate_report(report, spec)
        if not lane.startswith("candidate-full-"):
            for node, outcome in outcomes.items():
                if outcome != "executed" and node not in plan["retain_in_full"]:
                    raise Error("Focused timing sample did not execute a required node")
        gate._exact_selection(report, expected[lane.removeprefix("candidate-")], lane)
        reports[lane] = report
        digests[lane] = sha256(raw).hexdigest()
    if set(reports) != set(v9.PARTITION):
        raise Error("Rebalance requires all fifteen successful Windows execution reports")
    serial = {row["nodeid"] for row in inventory["collected"] if "serial" in row["markers"]}
    weights = {kind: defaultdict(float) for kind in COUNTS}
    for lane, report in reports.items():
        kind = lane.split("-")[1]
        for node in report["selected"]:
            weights[kind].setdefault(legacy.node_path(node), 0.0)
        for phase in report["reports"]:
            if "subtest" in phase:
                continue
            seconds = phase["duration"]
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
                raise Error("Invalid timing duration")
            node = phase["nodeid"]
            weights[kind][legacy.node_path(node)] += seconds / (1 if node in serial else 4)
    return weights, {"source_commit": identity[0], "workflow_run_id": identity[1], "run_attempt": identity[2], "report_sha256": digests}


def updated_manifest(plan, inventory, contracts, *, rebalance=False, timings=None):
    check_manifest(plan)
    retained = set(plan["retain_in_full"])
    rows = inventory["collected"]
    pop = populations(rows, contracts, retained)
    if rebalance != (timings is not None):
        raise Error("Rebalance requires all fifteen complete measured reports; ordinary update preserves owners")
    result = deepcopy(plan)
    weights, provenance = timings if timings is not None else ({}, None)
    changes = {}
    for kind, count in COUNTS.items():
        files = pop[kind]
        if rebalance and set(weights[kind]) != {file for file, nodes in files.items() if nodes}:
            raise Error(f"Incomplete measured {kind} file weights")
        cost = {file: weights[kind].get(file, 0.0) if rebalance else len(nodes) for file, nodes in files.items()}
        owners = {} if rebalance else {f: n for f, n in plan[kind].items() if f in files}
        loads = {i: sum(cost[f] for f, n in owners.items() if n == i) for i in range(1, count + 1)}
        for file in sorted(set(files) - owners.keys(), key=lambda f: (not bool(files[f]), -cost[f], f)):
            occupied = {n for f, n in owners.items() if files[f]}
            shard = min(loads, key=lambda i: (i in occupied, loads[i], i)) if files[file] and len(occupied) < count else min(loads, key=lambda i: (loads[i], i))
            owners[file] = shard
            loads[shard] += cost[file]
        result[kind] = dict(sorted(owners.items()))
        changes[kind] = {"added": sorted(set(files) - plan[kind].keys()), "removed": sorted(plan[kind].keys() - set(files)), "modeled_load": loads}
    groups(rows, result, contracts)
    if result != plan:
        result["maintenance"] = {"method": "four-worker serial-aware measured allocation" if rebalance else "preserve existing owners; allocate new files by selected node count", "inventory_sha256": inventory.get("_report_sha256"), "changes": changes, "timing_source": provenance}
    return result
