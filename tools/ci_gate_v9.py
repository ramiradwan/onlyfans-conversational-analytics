"""Production v9 coverage contract, with v8 checks retained for historical source.

The focused Windows selection and the exhaustive complement are independent
of the Linux classifier. Every removed duplicate must execute on Windows.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import json
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import ci_gate as gate
from tools.ci_windows_shards import candidate_groups, candidate_counts, LEGACY_WINDOWS_EXPRESSION, _unique_pairs

ROOT = Path(__file__).resolve().parents[1]
COUNTS = {"full": 8, "platform": 5, "analytics": 2}
FULL = tuple(f"candidate-full-{n}" for n in range(1, 9))
PLATFORM = tuple(f"candidate-platform-{n}" for n in range(1, 6))
ANALYTICS = tuple(f"candidate-analytics-{n}" for n in range(1, 3))
FOCUSED = PLATFORM + ANALYTICS
PARTITION = FULL + FOCUSED
QUALIFICATION = "windows-qualification"
PREFLIGHT = "candidate-preflight"
SPECS = {lane: spec for lane, spec in gate.SPECS.items()
         if lane not in {*gate.WINDOWS_FULL_LANES, *gate.WINDOWS_PLATFORM_LANES, "analytics-windows-contract"}}
for lane, spec in tuple(SPECS.items()):
    if spec.job == "windows-platform-contract-1":
        SPECS[lane] = replace(spec, job=QUALIFICATION, producer_job=QUALIFICATION)
for prefix, lanes, producer in (("windows-full-regression", FULL, "windows-full-shards"),
                                ("windows-platform-contract", PLATFORM, "windows-platform-contract"),
                                ("analytics-windows-contract", ANALYTICS, "analytics-windows-contract")):
    for index, lane in enumerate(lanes, 1):
        SPECS[lane] = gate.ReportSpec(f"{prefix}-{index}", "Windows", producer_job=producer, artifact_job=lane)
NEEDED = {spec.logical_job for spec in SPECS.values()} | {
    "web-build-and-test", "desktop-native-contracts", "fixed-sqlcipher-wheel", "windows-browser-e2e",
    "windows-full-regression", PREFLIGHT, "web-build-assets",
}
ACTUAL = {spec.job for spec in SPECS.values()} | {
    "web-build-and-test", "desktop-native-contracts", "fixed-sqlcipher-wheel", "windows-browser-e2e",
    "windows-full-regression", PREFLIGHT, "web-build-assets",
}
FULL_JOBS = {SPECS[lane].job for lane in FULL}
# Exact pre-existing full-Windows skips observed in qualified baseline runs.
# These also have corresponding existing Linux skip authority. Never accept
# a new arbitrary skip in the exhaustive complement merely because Linux passed.
WINDOWS_COMPLEMENT_SKIPS = {node: reason for (node, system), reason in gate.PR_SKIP_BASELINE.items()
                            if system == "Linux" and node != "tests/test_nlp_process_owners.py::test_joined_process_with_exit_code_259_is_absent_while_handle_is_retained"}


def required_lanes(event):
    gate.required_report_lanes(event)
    return set(SPECS) - ({gate.SCALE_LANE, *FULL} if event == "pull_request" else set())


def load_plan(root=ROOT):
    from tools.ci_windows_v9 import load_manifest
    plan = load_manifest(root)
    if candidate_counts(plan) != COUNTS or plan["schema"] != 2:
        raise gate.GateError("v9 Windows shard counts differ from the qualified policy")
    if set(plan["retain_in_full"]) != set(gate.PR_WINDOWS_SKIP_LANES):
        raise gate.GateError("v9 retained overlap must equal the reviewed Windows skip identities")
    return plan


def validate_coverage(reports, *, event="push", plan=None, contracts=None):
    if set(reports) != required_lanes(event):
        raise gate.GateError("v9 execution report set differs from CI event")
    plan = load_plan() if plan is None else plan
    if candidate_counts(plan) != COUNTS or plan["schema"] != 2 or set(plan["retain_in_full"]) != set(gate.PR_WINDOWS_SKIP_LANES):
        raise gate.GateError("v9 partition or retained overlap differs from policy")
    if contracts is None:
        contracts = gate.load_backend_manifest(ROOT)["windows_contracts"]
    inventories = {system: gate.inventory(reports[f"legacy-{system.lower()}-reference"]) for system in ("Linux", "Windows")}
    legacy, linux_lanes, linux_owners = gate._common_selection(reports, inventories, SPECS)
    groups = candidate_groups(inventories["Windows"].values(), plan, contracts)
    expected = {f"candidate-{name}": nodes for name, nodes in groups.items()}
    # Independently derive required ownership from the raw reference and the
    # declared selector contract, rather than trusting the runner's partition.
    from collections import Counter
    ownership = Counter(node for nodes in expected.values() for node in nodes)
    if set(ownership) != legacy["Windows"] or {node for node, count in ownership.items() if count != 1} != set(plan["retain_in_full"]) or any(count != 2 for node, count in ownership.items() if node in plan["retain_in_full"]):
        raise gate.GateError("v9 full/focused union or retained overlap differs from independent Windows selection")
    for kind, lanes in (("platform", PLATFORM), ("analytics", ANALYTICS)):
        declared = {node for node in legacy["Windows"] if any(gate._matches(node, entry["selector"]) for entry in contracts[kind])}
        actual = set().union(*(expected[lane] for lane in lanes))
        if actual != declared:
            raise gate.GateError(f"v9 {kind} ownership differs from independent selector contract")
        for lane in lanes:
            if any(plan[kind].get(node.split("::", 1)[0]) != int(lane.rsplit("-", 1)[1]) for node in expected[lane]):
                raise gate.GateError(f"{lane}: focused file owner differs from manifest")
    for lane in FULL:
        if any(plan["full"].get(node.split("::", 1)[0]) != int(lane.rsplit("-", 1)[1]) for node in expected[lane]):
            raise gate.GateError(f"{lane}: exhaustive file owner differs from manifest")
    signature = gate._raw_inventory_signature(inventories["Windows"])
    reference = reports["legacy-windows-reference"]
    for lane in set(PARTITION) & set(reports):
        report = reports[lane]
        if gate._raw_inventory_signature(gate.inventory(report)) != signature:
            raise gate.GateError(f"{lane}: raw Windows inventory differs")
        gate._exact_selection(report, expected[lane], lane)
        if (report.get("partial") is not False or report.get("selection") != {
                "keyword": "", "marker": LEGACY_WINDOWS_EXPRESSION, "targets": ["tests"]}
                or report.get("execution", {}).get("workers") != 4
                or report.get("execution", {}).get("dist") != "load"
                or any(report.get(key) != reference.get(key) for key in ("python", "python_implementation", "pytest"))):
            raise gate.GateError(f"{lane}: selection or execution environment changed")
    focused = set().union(*(expected[lane] for lane in FOCUSED))
    required_windows = {node for node in legacy["Windows"] if node in focused
                        or signature[node] & {"windows_compat", "windows_production"} or node not in inventories["Linux"]}
    windows_lanes = (*FOCUSED, "windows-production-boot")
    windows_union = focused | set(reports["windows-production-boot"]["selected"])
    if not required_windows <= windows_union or not legacy["Windows"] <= set(linux_owners) | windows_union:
        raise gate.GateError("mandatory Windows or legacy PR execution missing")
    outcomes = {lane: gate.validate_report(report, SPECS[lane]) for lane, report in reports.items()}
    loop = gate._validate_python313_loop_coverage(reports, inventories, outcomes, owner_lane="legacy-windows-reference")
    if any(value != "executed" for value in outcomes["windows-production-boot"].values()):
        raise gate.GateError("Windows production boot requires actual execution")
    retained = set(plan["retain_in_full"])
    for lane in FOCUSED:
        for node, outcome in outcomes[lane].items():
            if node in loop:
                # The independently verified 3.13 counterpart handles these.
                continue
            if node not in retained and outcome != "executed":
                raise gate.GateError(f"{node}: omitted full-regression node did not execute in focused job")
            if node in retained and outcome != "executed":
                allowed = gate.PR_SKIP_BASELINE[(node, "Windows")]
                if (outcome != "skipped" or gate._skip_reason(reports[lane], node) != allowed
                        or SPECS[lane].logical_job != gate.PR_WINDOWS_SKIP_LANES[node]):
                    raise gate.GateError(f"{node}: retained Windows skip differs from reviewed reason or lane")
                if allowed == "Skipped: Linux process filesystem observation" and not any(
                        outcomes[other].get(node) == "executed" for other in linux_lanes):
                    raise gate.GateError(f"{node}: retained Windows skip requires Linux execution")
                if event != "pull_request":
                    owner = next(other for other in FULL if node in expected[other])
                    if outcomes[owner][node] != "skipped" or gate._skip_reason(reports[owner], node) != allowed:
                        raise gate.GateError(f"{node}: retained Windows skip lacks full-path corroboration")
    if event != "pull_request":
        for lane in FULL:
            for node, outcome in outcomes[lane].items():
                if outcome == "executed" or node in loop:
                    continue
                reason = gate.PR_SKIP_BASELINE.get((node, "Windows"), WINDOWS_COMPLEMENT_SKIPS.get(node))
                if outcome != "skipped" or reason is None or gate._skip_reason(reports[lane], node) != reason:
                    raise gate.GateError(f"{node}: unreviewed exhaustive Windows outcome")
    for node in legacy["Linux"] | legacy["Windows"]:
        if node in loop:
            continue
        lanes = windows_lanes if node in required_windows else (*linux_lanes, *windows_lanes)
        observed = [outcomes[lane][node] for lane in lanes if node in outcomes[lane]]
        if any(value in {"executed", "xfailed"} for value in observed):
            continue
        system = "Windows" if node in required_windows else "Linux"
        allowed = gate.PR_SKIP_BASELINE.get((node, system))
        reasons = [gate._skip_reason(reports[lane], node) for lane in lanes if node in outcomes[lane]]
        if not observed or not allowed or any(reason != allowed for reason in reasons):
            raise gate.GateError(f"{node}: selected coverage has an unreviewed skip")
        if system == "Windows" and {SPECS[lane].logical_job for lane in lanes if node in outcomes[lane]} != {gate.PR_WINDOWS_SKIP_LANES.get(node)}:
            raise gate.GateError(f"{node}: reviewed Windows skip moved out of required lane")
    scale = gate._validate_profiles_and_scale(reports, inventories, outcomes, SPECS, event)
    return {"legacy_linux": len(legacy["Linux"]), "legacy_windows": len(legacy["Windows"]),
            "required_windows": len(required_windows), "scale": len(scale), "reports": len(reports)}


def validate_needs(needs, jobs, *, event="push"):
    gate.validate_needs(needs, jobs, event=event, needed_jobs=NEEDED, actual_jobs=ACTUAL, full_jobs=FULL_JOBS)


def load_reports(directory, *, source_commit, run_id, jobs, event="push"):
    return gate.load_reports(directory, source_commit=source_commit, run_id=run_id, jobs=jobs, event=event,
                             specs=SPECS, expected_lanes=required_lanes(event))
