"""Verify source-enumerated visual coverage before publishing approved artifacts.

Expected cases come from a fresh, dependency-free source inventory, never from a
producer's claimed selection. Actual transitions and successful raw report
indexes independently corroborate each receipt. Partial diagnostics contain
only registered identities, provenance and closed outcomes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from tools.engineering_attestation import (ContractError, GitHubApi, latest_ci_jobs,
        load_json_strict, _ci_literal_mapping, _ci_literal_list)
except ModuleNotFoundError:
    from engineering_attestation import (ContractError, GitHubApi, latest_ci_jobs,
        load_json_strict, _ci_literal_mapping, _ci_literal_list)

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("dynamic", "remaining")
JOBS = {group: f"visual-capture-{group}" for group in GROUPS} | {"all": "visual-capture-serial-control"}
SERIAL_IF = "${{ github.event_name == 'workflow_dispatch' && inputs.visual_serial_control }}"
POLICY = {"schema": "visual-ci-policy/v1", "policy_version": "visual-v1",
    "workflow": ".github/workflows/visual-capture.yml", "groups": list(GROUPS),
    "workers_per_runner": 4, "max_parallel": 2, "contracts_job": "visual-contracts",
    "execution_job": "visual-capture-execution", "aggregate_job": "visual-capture",
    "producer_jobs": JOBS, "allowed_skipped_jobs": [JOBS["all"]], "serial_condition": SERIAL_IF}
RECEIPT_KEYS = {"schema", "source_commit", "workflow_run_id", "run_attempt", "logical_job", "group",
    "platform", "workers", "inventory_sha256", "complete", "exit_code", "retries", "skips", "cases",
    "file_sha256", "phase_timings", "started_at", "finished_at", "duration_ms"}
CASE_KEYS = {"id", "configuration", "observations", "outcome", "files", "duration_ms"}
INVENTORY_CASE_KEYS = {"id", "group", "kind", "configuration", "observations", "report", "files"}
PHASES = {"prepare", "frontend", "dynamic", "freshness", "review", "static", "cleanup", "manifest"}


class VisualGateError(ValueError):
    """Closed error code with optional registered identities only."""
    def __init__(self, code: str, case_id: str | None = None):
        super().__init__(code)
        self.code = code
        self.case_id = case_id


def require(value: Any, code: str, case_id: str | None = None) -> None:
    if not value:
        raise VisualGateError(code, case_id)


def number(value: Any, code: str = "invalid_number", *, integer: bool = False, minimum: float = 0) -> Any:
    require(type(value) is int if integer else type(value) in (int, float), code)
    require(math.isfinite(value) and minimum <= value <= 9007199254740991, code)
    return value


def exact(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    require(isinstance(value, dict) and set(value) == keys, code)
    return value


def read(path: Path) -> bytes:
    require(path.is_file() and not any(item.is_symlink() for item in (path, *path.parents)), "missing_or_unsafe_file")
    require(path.stat().st_size <= 128 * 1024 * 1024, "oversized_visual_file")
    return path.read_bytes()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def same(first: Any, second: Any) -> bool:
    # Python otherwise considers True == 1, which is not an exact JSON config.
    return json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(second, sort_keys=True, separators=(",", ":"))


def document(path: Path) -> Any:
    return load_json_strict(read(path), label="visual evidence")


def safe_path(value: Any) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_./-]+\.(?:png|json)", value)
            and not PurePosixPath(value).is_absolute() and ".." not in value.split("/")
            and "//" not in value, "invalid_visual_path")
    return value


def validate_policy(workflow_source: bytes, policy_source: bytes) -> dict[str, Any]:
    policy = load_json_strict(policy_source, label="visual CI source policy")
    require(policy == POLICY and type(policy.get("workers_per_runner")) is int
            and type(policy.get("max_parallel")) is int, "unsupported_visual_policy")
    source = workflow_source.decode("utf-8").replace("\r\n", "\n")
    require(source.count("  VISUAL_CI_POLICY_VERSION: visual-v1\n") == 1
            and not re.search(r"(?m)^    paths(?:-ignore)?:", source), "visual_policy_declaration")
    blocks = re.split(r"(?m)^jobs:\s*\n", source)
    require(len(blocks) == 2, "visual_job_mapping")
    jobs = _ci_literal_mapping(blocks[1], 2)
    require(set(jobs) == {"visual-contracts", "visual-capture-execution", "visual-capture", JOBS["all"]},
            "visual_job_set")
    fields = {name: _ci_literal_mapping(body, 4) for name, body in jobs.items()}
    matrix = fields["visual-capture-execution"]
    strategy = _ci_literal_mapping(matrix.get("strategy", ""), 6)
    axes = _ci_literal_mapping(strategy.get("matrix", ""), 8)
    require(matrix.get("name") == "visual-capture-${{ matrix.group }}"
            and matrix.get("runs-on") == "ubuntu-latest" and matrix.get("timeout-minutes") == "20"
            and "if" not in matrix and matrix.get("continue-on-error", "false") == "false"
            and "needs" not in matrix
            and set(strategy) == {"fail-fast", "max-parallel", "matrix"}
            and strategy.get("fail-fast") == "false" and strategy.get("max-parallel") == "2"
            and set(axes) == {"group"} and _ci_literal_list(axes["group"]) == list(GROUPS), "visual_matrix_policy")
    contracts, gate, serial = (fields[name] for name in ("visual-contracts", "visual-capture", JOBS["all"]))
    require("if" not in contracts and contracts.get("continue-on-error", "false") == "false"
            and contracts.get("name", "visual-contracts") == "visual-contracts", "visual_contracts_policy")
    require(gate.get("if") == "${{ always() }}" and gate.get("name", "visual-capture") == "visual-capture"
            and gate.get("continue-on-error", "false") == "false"
            and set(_ci_literal_list(gate.get("needs", ""))) == {
                "visual-contracts", "visual-capture-execution", JOBS["all"]}, "visual_aggregate_policy")
    require(serial.get("if") == SERIAL_IF and serial.get("name", JOBS["all"]) == JOBS["all"]
            and serial.get("needs") == "visual-contracts" and serial.get("runs-on") == "ubuntu-latest"
            and serial.get("timeout-minutes") == "20" and "strategy" not in serial
            and serial.get("continue-on-error", "false") == "false", "visual_serial_policy")
    return policy


def load_policy(root: Path = ROOT) -> dict[str, Any]:
    return validate_policy(read(root / POLICY["workflow"]), read(root / "ci/visual-ci-policy.json"))


def validate_inventory(data: bytes) -> tuple[dict[str, Any], str]:
    inventory = load_json_strict(data, label="source visual inventory")
    exact(inventory, {"schema", "workers", "fixed_now", "shared_files", "cases"}, "inventory_fields")
    require(inventory["schema"] == "visual-ci-inventory/v1" and type(inventory["workers"]) is int
            and inventory["workers"] == 4 and isinstance(inventory["fixed_now"], str)
            and isinstance(inventory["cases"], list) and inventory["cases"], "inventory_schema")
    exact(inventory["shared_files"], set(GROUPS), "inventory_shared_files")
    for paths in inventory["shared_files"].values():
        require(isinstance(paths, list) and len(paths) == len(set(paths)), "inventory_shared_paths")
        for path in paths:
            safe_path(path)
    seen = set()
    for case in inventory["cases"]:
        exact(case, INVENTORY_CASE_KEYS, "inventory_case_fields")
        require(isinstance(case["id"], str) and re.fullmatch(r"[a-zA-Z0-9_.:-]+", case["id"])
                and case["id"] not in seen and case["group"] in GROUPS
                and case["kind"] in {"ordinary", "dynamic", "freshness", "review", "static"}
                and isinstance(case["configuration"], dict) and case["configuration"]
                and isinstance(case["observations"], list) and case["observations"]
                and all(isinstance(label, str) and label for label in case["observations"]), "inventory_case")
        require(isinstance(case["files"], list) and case["files"]
                and len(case["files"]) == len(set(case["files"])) and case["report"] in case["files"], "inventory_case_files")
        for path in case["files"]:
            safe_path(path)
        seen.add(case["id"])
    require({case["group"] for case in inventory["cases"]} == set(GROUPS), "empty_visual_group")
    return inventory, sha(data)


def load_inventory(root: Path = ROOT, *, source: str | None = None) -> tuple[dict[str, Any], str]:
    if source is not None:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, timeout=15)
        require(head.returncode == 0 and head.stdout.decode().strip() == source, "inventory_checkout_source")
        paths = ["tools/visual-capture", "extension/qualification/surface-states.mjs"]
        dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *paths], cwd=root, timeout=15)
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--", *paths],
                                  cwd=root, capture_output=True, timeout=15)
        require(dirty.returncode == 0 and untracked.returncode == 0 and not untracked.stdout,
                "inventory_checkout_modified")
    env = dict(os.environ)
    for key in ("VISUAL_CAPTURE_ONLY", "STATIC_SURFACE_ONLY"):
        env.pop(key, None)
    result = subprocess.run(["node", str(root / "tools/visual-capture/ci/inventory.mjs")], cwd=root,
        env=env, capture_output=True, timeout=60, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 32 * 1024 * 1024, "source_inventory_failed")
    return validate_inventory(result.stdout)


def selected_cases(inventory: dict[str, Any], group: str) -> dict[str, dict[str, Any]]:
    require(group in JOBS, "unknown_visual_group")
    return {case["id"]: case for case in inventory["cases"] if group == "all" or case["group"] == group}


def expected_files(inventory: dict[str, Any], group: str) -> set[str]:
    return {path for case in selected_cases(inventory, group).values() for path in case["files"]} | {
        path for selected in (GROUPS if group == "all" else (group,)) for path in inventory["shared_files"][selected]}


def _rows(rows: Any, key: str, code: str) -> dict[str, Any]:
    require(isinstance(rows, list), code)
    result = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get(key), str) and row[key] not in result, code)
        result[row[key]] = row
    return result


def _watcher(report: dict[str, Any], case_id: str) -> None:
    require(report.get("failures") == [] and "error" not in report
            and isinstance(report.get("frames"), list) and report["frames"]
            and type(report.get("samples")) is int and report["samples"] == len(report["frames"]),
            "missing_or_failed_geometry", case_id)
    for frame in report["frames"]:
        require(isinstance(frame, dict) and frame.get("errors") == []
                and isinstance(frame.get("regions"), list), "invalid_geometry_frame", case_id)
    require(any(frame["regions"] for frame in report["frames"]), "empty_geometry_measurement", case_id)


def _snapshots(rows: list[dict[str, Any]], case_id: str) -> None:
    for row in rows:
        require(isinstance(row, dict), "invalid_transition", case_id)
        for side in ("before", "after"):
            sample = row.get(side)
            require(isinstance(sample, dict) and type(sample.get("at")) in (int, float)
                    and math.isfinite(sample["at"]) and sample["at"] >= 0, "missing_transition_snapshot", case_id)


def validate_raw_reports(directory: Path, inventory: dict[str, Any], group: str, source: str) -> None:
    cases = selected_cases(inventory, group)
    cache: dict[str, Any] = {}
    def report(path: str) -> Any:
        if path not in cache:
            cache[path] = document(directory / path)
        return cache[path]
    manifest = report("manifest.json")
    require(isinstance(manifest, dict) and manifest.get("revision") == source
            and manifest.get("fixedNow") == inventory["fixed_now"] and manifest.get("failures") == [], "capture_manifest")
    entries = _rows(manifest.get("entries"), "file", "ordinary_index")
    visions = _rows(manifest.get("diagnostics"), "file", "vision_index")
    dynamic_index = _rows(manifest.get("dynamic"), "file", "dynamic_index")
    freshness_index = _rows(manifest.get("freshness"), "file", "freshness_index")
    ordinary_files, vision_files, dynamic_files, freshness_files, static_files = set(), set(), set(), set(), set()
    review_names, static_names = set(), set()
    for case_id, case in cases.items():
        kind, config = case["kind"], case["configuration"]
        raw = report(case["report"])
        require(isinstance(raw, dict) and raw.get("revision") == source and raw.get("failures") == []
                and "error" not in raw, "failed_or_wrong_source_report", case_id)
        if kind in {"dynamic", "freshness"}:
            _watcher(raw, case_id)
            require(all(same(raw.get(key), config[key]) for key in ("mode", "fontScale", "motion"))
                    and isinstance(raw.get("viewport"), dict) and same(raw["viewport"].get("width"), config["width"]),
                    "transition_configuration", case_id)
            transitions = raw.get("transitions")
            require(isinstance(transitions, list), "transition_inventory", case_id)
            _snapshots(transitions, case_id)
            if kind == "dynamic":
                require(raw.get("view") == config["view"]
                        and [row.get("id") for row in transitions] == case["observations"], "dynamic_sequence", case_id)
                dynamic_files.add(case["report"])
                index = dynamic_index.get(case["report"], {})
                require(index.get("view") == config["view"] and same(index.get("width"), config["width"])
                        and index.get("states") == case["observations"] and type(index.get("transitions")) is int
                        and index["transitions"] == len(transitions)
                        and index.get("failures") == [], "dynamic_index_evidence", case_id)
            else:
                overlays = raw.get("overlays")
                require(isinstance(overlays, list), "freshness_overlays", case_id)
                _snapshots(overlays, case_id)
                labels = [f"edge:{row.get('from')}:{row.get('to')}" for row in transitions] + [f"overlay:{row.get('state')}" for row in overlays]
                require(raw.get("view") == "freshness" and labels == case["observations"]
                        and type(raw.get("expected")) is int and raw["expected"] == len(transitions), "freshness_sequence", case_id)
                freshness_files.add(case["report"])
                index = freshness_index.get(case["report"], {})
                require(type(index.get("transitions")) is int and index["transitions"] == len(transitions)
                        and index.get("failures") == [], "freshness_index_evidence", case_id)
        elif kind == "ordinary":
            _watcher(raw, case_id)
            require(raw.get("view") == config["workspace"] and raw.get("transition") == f"boot:{config['state']}"
                    and raw.get("mode") == config["mode"] and isinstance(raw.get("viewport"), dict)
                    and all(same(raw["viewport"].get(key), config[target]) for key, target in (
                        ("name", "viewport"), ("width", "width"), ("height", "height"))), "ordinary_configuration", case_id)
            for path in case["files"]:
                if path.startswith("diagnostics/vision/"):
                    row = visions.get(path, {})
                    require(row.get("type") in [label.removeprefix("vision:") for label in case["observations"] if label.startswith("vision:")]
                            and path.endswith(f"-{row.get('type')}.png")
                            and row.get("diagnosticOnly") is True and row.get("mode") == config["mode"]
                            and row.get("viewport") == config["viewport"], "vision_evidence", case_id)
                    vision_files.add(path)
                elif path.endswith(".png"):
                    row = entries.get(path, {})
                    capture = "fold" if path.endswith("-fold.png") else "full"
                    require(row.get("capture") == capture and all(same(row.get(key), config[key]) for key in (
                        "workspace", "state", "variant", "mode", "viewport", "width")), "ordinary_screenshot_evidence", case_id)
                    ordinary_files.add(path)
        elif kind == "review":
            checks = _rows(raw.get("checks"), "name", "review_checks")
            require(case["observations"] == [config["name"]] and checks.get(config["name"], {}).get("result") == "passed",
                    "review_check_missing", case_id)
            review_names.add(config["name"])
        elif kind == "static":
            name = case["observations"][0].removeprefix("check:")
            checks = _rows(raw.get("checks"), "name", "static_checks")
            require(case["observations"] == [f"check:{name}", "fold", "full"]
                    and checks.get(name, {}).get("geometry") == f"{name}-geometry.json", "static_check_missing", case_id)
            geometry = report(f"static-surfaces/{name}-geometry.json")
            _watcher(geometry, case_id)
            require(geometry.get("revision") == source and geometry.get("view") == config["surface"]
                    and geometry.get("transition") == f"boot:{config['state']}" and geometry.get("mode") == config["mode"]
                    and same(geometry.get("viewport"), config["viewport"]), "static_configuration", case_id)
            images = _rows(raw.get("entries"), "file", "static_entries")
            for capture in ("fold", "full"):
                path = f"static-surfaces/{name}-{capture}.png"
                row = images.get(path, {})
                require(row.get("capture") == capture and all(same(row.get(key), value) for key, value in config.items()),
                        "static_screenshot_evidence", case_id)
                static_files.add(path)
            static_names.add(name)
    require(set(entries) == ordinary_files and set(visions) == vision_files
            and set(dynamic_index) == dynamic_files and set(freshness_index) == freshness_files, "unexpected_capture_index")
    if dynamic_files:
        require(report("transitions/manifest.json") == manifest["dynamic"], "dynamic_index_mismatch")
    if freshness_files:
        require(report("freshness-transitions.json") == manifest["freshness"], "freshness_index_mismatch")
    if review_names:
        require(set(_rows(report("review/acceptance.json")["checks"], "name", "review_checks")) == review_names
                and same(manifest.get("review"), {"file": "review/acceptance.json", "passed": len(review_names), "failures": 0}),
                "review_inventory_mismatch")
    else:
        require(manifest.get("review") is None, "unexpected_review")
    if static_names:
        raw = report("static-surfaces/acceptance.json")
        require(set(_rows(raw["checks"], "name", "static_checks")) == static_names
                and set(_rows(raw["entries"], "file", "static_entries")) == static_files
                and same(manifest.get("static"), {"file": "static-surfaces/acceptance.json", "passed": len(static_names),
                                               "screenshots": len(static_files), "failures": 0}), "static_inventory_mismatch")
    else:
        require(manifest.get("static") is None, "unexpected_static")


def validate_receipt(directory: Path, *, inventory: dict[str, Any], digest: str, source: str,
                     run_id: int, attempt: int, group: str) -> dict[str, Any]:
    receipt = exact(document(directory / "capture-ci.json"), RECEIPT_KEYS, "receipt_fields")
    require(receipt["schema"] == "visual-ci-receipt/v1" and receipt["source_commit"] == source
            and receipt["workflow_run_id"] == str(run_id) and type(receipt["run_attempt"]) is int
            and receipt["run_attempt"] == attempt and receipt["logical_job"] == JOBS[group] and receipt["group"] == group
            and receipt["platform"] == "Linux" and type(receipt["workers"]) is int and receipt["workers"] == 4
            and receipt["inventory_sha256"] == digest, "receipt_provenance")
    require(receipt["complete"] is True and all(type(receipt[key]) is int and receipt[key] == 0
            for key in ("exit_code", "retries", "skips")), "incomplete_capture")
    number(receipt["duration_ms"], integer=True)
    try:
        timestamps = [datetime.fromisoformat(receipt[key].replace("Z", "+00:00")) for key in ("started_at", "finished_at")]
        require(all(value.tzinfo is not None for value in timestamps) and timestamps[1] >= timestamps[0], "capture_timestamps")
    except (ValueError, AttributeError, TypeError) as exc:
        raise VisualGateError("capture_timestamps") from exc
    require(isinstance(receipt["phase_timings"], dict) and receipt["phase_timings"]
            and set(receipt["phase_timings"]) <= PHASES, "phase_timings")
    for duration in receipt["phase_timings"].values():
        number(duration)
    expected = selected_cases(inventory, group)
    actual = _rows(receipt["cases"], "id", "case_inventory")
    require(set(actual) == set(expected), "incomplete_case_inventory")
    for case_id, row in actual.items():
        exact(row, CASE_KEYS, "case_receipt_fields")
        case = expected[case_id]
        require(same(row["configuration"], case["configuration"]) and row["observations"] == case["observations"]
                and row["outcome"] == "passed" and isinstance(row["files"], list)
                and len(row["files"]) == len(set(row["files"])) and set(row["files"]) == set(case["files"]),
                "case_execution_mismatch", case_id)
        number(row["duration_ms"], integer=True)
    paths = expected_files(inventory, group)
    require(isinstance(receipt["file_sha256"], dict) and set(receipt["file_sha256"]) == paths, "file_inventory")
    for path, value in receipt["file_sha256"].items():
        data = read(directory / path)
        require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) and sha(data) == value, "file_digest")
        if path.endswith(".png"):
            require(len(data) >= 33 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR"
                    and all(0 < dimension <= 32768 for dimension in struct.unpack(">II", data[16:24])), "invalid_png")
    require(same(document(directory / "manifest.json").get("phaseTimings"), receipt["phase_timings"]), "phase_timing_mismatch")
    validate_raw_reports(directory, inventory, group, source)
    return receipt


def validate_jobs(needs: Any, jobs: dict[str, dict[str, Any]], *, serial_requested: bool = False,
                  qualification: bool = False) -> None:
    require(type(serial_requested) is bool and type(qualification) is bool
            and (not serial_requested or qualification), "serial_requires_qualification")
    exact(needs, {"visual-contracts", "visual-capture-execution", JOBS["all"]}, "visual_gate_dependencies")
    for name, row in needs.items():
        expected = "skipped" if name == JOBS["all"] and not serial_requested else "success"
        require(isinstance(row, dict) and row.get("result") == expected, "visual_dependency_result")
    for name in {"visual-contracts", *JOBS.values()}:
        expected = "skipped" if name == JOBS["all"] and not serial_requested else "success"
        require(jobs.get(name, {}).get("status") == "completed"
                and jobs[name].get("conclusion") == expected, "latest_visual_job_failed")


def select_evidence(directory: Path, *, jobs: dict[str, dict[str, Any]], source: str,
                    run_id: int, groups: tuple[str, ...]) -> dict[str, Path]:
    chosen: dict[str, Path] = {}
    identities = set()
    for path in sorted(directory.rglob("capture-ci.json")):
        receipt = exact(document(path), RECEIPT_KEYS, "receipt_fields")
        group, attempt = receipt["group"], receipt["run_attempt"]
        require(isinstance(group, str) and group in groups and receipt["source_commit"] == source
                and receipt["workflow_run_id"] == str(run_id) and receipt["logical_job"] == JOBS[group], "unexpected_visual_artifact")
        number(attempt, integer=True, minimum=1)
        require((group, attempt) not in identities, "duplicate_visual_artifact")
        identities.add((group, attempt))
        require(path.parent.name == f"visual-inputs-{group}-{source}-{run_id}-{attempt}", "visual_artifact_name")
        require(attempt <= jobs[JOBS[group]]["run_attempt"], "future_visual_artifact")
        if attempt == jobs[JOBS[group]]["run_attempt"]:
            chosen[group] = path.parent
    require(set(chosen) == set(groups), "missing_newest_visual_artifact")
    return chosen


def validate_evidence(directory: Path, *, jobs: dict[str, dict[str, Any]], source: str,
                      run_id: int, current_attempt: int, qualification: bool,
                      serial_requested: bool = False, root: Path = ROOT,
                      inventory: dict[str, Any] | None = None, digest: str | None = None) -> tuple[dict[str, Path], dict[str, Any]]:
    require(not serial_requested or qualification, "serial_requires_qualification")
    number(run_id, integer=True, minimum=1)
    number(current_attempt, integer=True, minimum=1)
    if inventory is None:
        inventory, digest = load_inventory(root, source=source)
    require(isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest), "inventory_digest")
    groups = GROUPS + (("all",) if serial_requested else ())
    for group in groups:
        job = jobs.get(JOBS[group], {})
        require(job.get("name") == JOBS[group] and job.get("status") == "completed"
                and job.get("conclusion") == "success" and job.get("head_sha") == source
                and type(job.get("run_id")) is int and job["run_id"] == run_id
                and type(job.get("run_attempt")) is int and 1 <= job["run_attempt"] <= current_attempt
                and type(job.get("id")) is int and job["id"] > 0, "visual_producer_identity")
    chosen = select_evidence(directory, jobs=jobs, source=source, run_id=run_id, groups=groups)
    receipts = {group: validate_receipt(path, inventory=inventory, digest=digest, source=source,
        run_id=run_id, attempt=jobs[JOBS[group]]["run_attempt"], group=group) for group, path in chosen.items()}
    expected = {case["id"] for case in inventory["cases"]}
    split = [{row["id"] for row in receipts[group]["cases"]} for group in GROUPS]
    require(not split[0] & split[1] and split[0] | split[1] == expected, "visual_coverage_union")
    summaries = {}
    for group, receipt in receipts.items():
        cases = selected_cases(inventory, group)
        summaries[group] = {"producer_attempt": receipt["run_attempt"], "job_id": jobs[JOBS[group]]["id"],
            "case_ids": sorted(cases), "case_count": len(cases), "counts_by_kind": dict(Counter(case["kind"] for case in cases.values())),
            "outcomes": {row["id"]: row["outcome"] for row in receipt["cases"]},
            "runner_duration_ms": receipt["duration_ms"], "phase_timings": receipt["phase_timings"],
            "receipt_sha256": sha(read(chosen[group] / "capture-ci.json")), "retries": 0, "skips": 0}
    proof = {"schema": "visual-ci-verified/v1", "source_commit": source, "workflow_run_id": run_id,
        "current_attempt": current_attempt, "qualification": qualification, "inventory_sha256": digest,
        "policy_version": "visual-v1", "selected_ids": sorted(expected), "selected_count": len(expected),
        "retries": 0, "skips": 0, "groups": {group: summaries[group] for group in GROUPS},
        "serial_control": False, "paired_control": serial_requested, "control": None}
    if serial_requested:
        combined = {case_id: outcome for group in GROUPS for case_id, outcome in summaries[group]["outcomes"].items()}
        require(summaries["all"]["outcomes"] == combined, "visual_control_coverage")
        proof["control"] = {key: value for key, value in proof.items()
                            if key not in {"groups", "control", "paired_control", "serial_control"}}
        proof["control"].update(groups={"all": summaries["all"]}, serial_control=True,
                                paired_control=False, control=None)
    return chosen, proof


def _summary(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text, end="")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        try:
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
                stream.write(text)
        except OSError:
            pass


def _rerun(group: str) -> str:
    return f"node tools/visual-capture/capture.mjs artifacts/visual-rerun-{group} --stage-group {group}"


def failure_summary(code: str, case_id: str | None = None) -> None:
    # Callers supply a closed code and independently registered case identity.
    lines = ["## Visual capture refused", "", f"Reason: `{code}`."]
    if case_id:
        lines.append(f"Registered case: `{case_id}`.")
    lines += ["", "Reproduce the complete capture groups:", ""]
    lines += [f"- `{_rerun(group)}`" for group in GROUPS]
    _summary(lines)


def publish_diagnostics(raw: Path, destination: Path, inventory: dict[str, Any], *, source: str,
                        run_id: int, attempt: int) -> dict[str, Any]:
    require(not destination.exists(), "diagnostic_destination_exists")
    known = {case["id"]: case for case in inventory["cases"]}
    result: dict[str, Any] = {"schema": "visual-ci-diagnostics/v1", "source_commit": source,
        "workflow_run_id": str(run_id), "run_attempt": attempt, "group": None, "status": "metadata_unavailable",
        "cases": [], "phase_timings": {}}
    try:
        receipt = document(raw / "capture-ci.json")
        exact(receipt, RECEIPT_KEYS, "receipt_fields")
        group = receipt["group"]
        require(isinstance(group, str) and group in JOBS and receipt["source_commit"] == source
                and receipt["workflow_run_id"] == str(run_id) and type(receipt["run_attempt"]) is int
                and receipt["run_attempt"] == attempt and receipt["logical_job"] == JOBS[group]
                and isinstance(receipt["cases"], list), "partial_provenance")
        phases = receipt["phase_timings"]
        require(isinstance(phases, dict) and phases and set(phases) <= PHASES, "partial_phase_timings")
        for duration in phases.values():
            number(duration, "partial_phase_timings")
        seen = set()
        safe = []
        for row in receipt["cases"]:
            exact(row, CASE_KEYS, "partial_case_fields")
            case_id = row["id"]
            require(isinstance(case_id, str) and case_id in known and case_id not in seen
                    and (group == "all" or known[case_id]["group"] == group)
                    and row["outcome"] in {"passed", "failed", "incomplete"}, "partial_case_values")
            safe.append({"id": case_id, "outcome": row["outcome"]})
            seen.add(case_id)
        result.update(group=group, status="restricted_metadata", cases=safe, phase_timings=dict(sorted(phases.items())))
    except (VisualGateError, ContractError, OSError, ValueError, TypeError, KeyError):
        pass
    destination.mkdir(parents=True)
    (destination / "diagnostics.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    failed = [row for row in result["cases"] if row["outcome"] != "passed"]
    if result["status"] == "metadata_unavailable":
        failure_summary("restricted_metadata_unavailable")
    elif not result["cases"]:
        failure_summary("no_case_execution_metadata")
    elif failed:
        lines = ["## Visual capture incomplete", "", f"Group: `{result['group']}`; producer attempt: {attempt}.",
                 "", "| Registered case | Outcome |", "| --- | --- |"]
        lines += [f"| `{row['id']}` | {row['outcome']} |" for row in failed]
        lines += ["", "Phase seconds: " + ", ".join(f"{phase}={duration:.3f}"
                    for phase, duration in result["phase_timings"].items()) + "."]
        lines += ["", "Reproduce the complete group:", "", f"`{_rerun(result['group'])}`"]
        _summary(lines)
    return result


def publish_complete(raw: Path, destination: Path, *, inventory: dict[str, Any], digest: str,
                     source: str, run_id: int, attempt: int) -> None:
    receipt = document(raw / "capture-ci.json")
    group = receipt.get("group") if isinstance(receipt, dict) else None
    require(isinstance(group, str) and group in JOBS, "unknown_visual_group")
    validate_receipt(raw, inventory=inventory, digest=digest, source=source, run_id=run_id, attempt=attempt, group=group)
    require(not destination.exists(), "publication_destination_exists")
    for name in sorted(expected_files(inventory, group) | {"capture-ci.json"}):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read(raw / name))


def assemble(chosen: dict[str, Path], output: Path, inventory: dict[str, Any], proof: dict[str, Any]) -> dict[str, Any]:
    require(not output.exists(), "assembly_destination_exists")
    manifests = {group: document(chosen[group] / "manifest.json") for group in GROUPS}
    # Build outside the publication path. A failed write never leaves a folder
    # that could be mistaken for the complete visual artifact.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-assembly-", dir=output.parent) as temporary:
        stage = Path(temporary) / "complete"
        stage.mkdir()
        seen = set()
        for group in GROUPS:
            for name in sorted(expected_files(inventory, group) - {"manifest.json"}):
                require(name not in seen, "overlapping_visual_file")
                seen.add(name)
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(read(chosen[group] / name))
        dynamic, remaining = manifests["dynamic"], manifests["remaining"]
        timings = {}
        for manifest in manifests.values():
            for phase, seconds in manifest["phaseTimings"].items():
                timings[phase] = timings.get(phase, 0) + seconds
        manifest = {"revision": proof["source_commit"], "fixedNow": inventory["fixed_now"], "phaseTimings": timings,
            "entries": remaining["entries"], "diagnostics": remaining["diagnostics"], "dynamic": dynamic["dynamic"],
            "freshness": remaining["freshness"], "static": remaining["static"], "review": remaining["review"], "failures": []}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        # Recheck the assembled full view against the same independently enumerated
        # source contract, without treating the merge itself as test execution.
        validate_raw_reports(stage, inventory, "all", proof["source_commit"])
        proof = dict(proof, assembled_file_sha256={name: sha(read(stage / name)) for name in sorted(expected_files(inventory, "all"))},
                     finalized=False)
        stage.rename(output)
    return proof


def finalize(output: Path, verified_output: Path) -> dict[str, Any]:
    proof = document(verified_output)
    require(isinstance(proof, dict) and proof.get("schema") == "visual-ci-verified/v1"
            and proof.get("finalized") is False and isinstance(proof.get("assembled_file_sha256"), dict), "missing_assembly_proof")
    require(proof.get("source_commit") == os.environ.get("PRODUCT_SHA")
            and str(proof.get("workflow_run_id")) == os.environ.get("GITHUB_RUN_ID")
            and str(proof.get("current_attempt")) == os.environ.get("GITHUB_RUN_ATTEMPT"), "finalization_provenance")
    expected = set(proof["assembled_file_sha256"]) | {"color-qualification.json"}
    actual = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
    require(actual == expected, "final_visual_file_inventory")
    for name, digest in proof["assembled_file_sha256"].items():
        safe_path(name)
        require(sha(read(output / name)) == digest, "assembly_modified_before_publication")
    require(isinstance(document(output / "color-qualification.json"), (dict, list)), "invalid_color_report")
    receipt = {"schema": "visual-ci-artifact/v1", "source_commit": proof["source_commit"],
        "workflow_run_id": proof["workflow_run_id"], "run_attempt": proof["current_attempt"],
        "inventory_sha256": proof["inventory_sha256"], "policy_version": "visual-v1",
        "producers": {group: {key: value[key] for key in ("job_id", "producer_attempt", "receipt_sha256")}
                      for group, value in proof["groups"].items()},
        "file_sha256": {name: sha(read(output / name)) for name in sorted(expected)}}
    (output / "ci-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    proof.update(finalized=True, final_receipt_sha256=sha(read(output / "ci-receipt.json")))
    verified_output.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    return receipt


def _identity() -> tuple[str, int, int]:
    source = os.environ.get("PRODUCT_SHA", "")
    require(bool(re.fullmatch(r"[a-f0-9]{40}", source)), "invalid_source")
    run_id, attempt = int(os.environ.get("GITHUB_RUN_ID", "0")), int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
    number(run_id, integer=True, minimum=1)
    number(attempt, integer=True, minimum=1)
    return source, run_id, attempt


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv.pop(0) if argv and argv[0] in {"seal", "finalize", "diagnostics"} else "verify"
    parser = argparse.ArgumentParser(description=__doc__)
    if action in {"seal", "diagnostics"}:
        for name in (("artifact-dir", "publish-dir", "diagnostics-dir") if action == "seal" else ("artifact-dir", "diagnostics-dir")):
            parser.add_argument("--" + name, type=Path, required=True)
    else:
        parser.add_argument("--output-dir", type=Path, required=True)
        parser.add_argument("--verified-output", type=Path, required=True)
        if action == "verify":
            parser.add_argument("--reports-dir", type=Path, required=True)
            parser.add_argument("--needs-json", default=os.environ.get("CI_NEEDS_JSON", ""))
    args = parser.parse_args(argv)
    try:
        source, run_id, attempt = _identity()
        if action != "diagnostics":
            load_policy()
        if action == "finalize":
            finalize(args.output_dir, args.verified_output)
        else:
            inventory, digest = load_inventory(source=None if action == "diagnostics" else source)
            if action in {"seal", "diagnostics"}:
                publish_diagnostics(args.artifact_dir, args.diagnostics_dir, inventory, source=source, run_id=run_id, attempt=attempt)
                if action == "seal":
                    publish_complete(args.artifact_dir, args.publish_dir, inventory=inventory, digest=digest,
                                     source=source, run_id=run_id, attempt=attempt)
            else:
                qualification = os.environ.get("VISUAL_QUALIFICATION", "false")
                serial = os.environ.get("VISUAL_SERIAL_CONTROL", "false")
                require(qualification in {"true", "false"} and serial in {"true", "false"}, "invalid_visual_mode")
                require(serial != "true" or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "serial_requires_dispatch")
                client = GitHubApi(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""))
                jobs = latest_ci_jobs(client, run_id=run_id, run_attempt=attempt, source_commit=source)
                needs = load_json_strict(args.needs_json.encode(), label="visual dependencies")
                validate_jobs(needs, jobs, serial_requested=serial == "true", qualification=qualification == "true")
                chosen, proof = validate_evidence(args.reports_dir, jobs=jobs, source=source, run_id=run_id,
                    current_attempt=attempt, qualification=qualification == "true", serial_requested=serial == "true",
                    inventory=inventory, digest=digest)
                proof = assemble(chosen, args.output_dir, inventory, proof)
                args.verified_output.parent.mkdir(parents=True, exist_ok=True)
                args.verified_output.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
                lines = ["## Visual capture evidence", "", f"Verified {proof['selected_count']} cases; retries=0; skips=0.",
                         "", "| Group | Cases | Producer attempt | Phase seconds |", "| --- | ---: | ---: | --- |"]
                for group, value in proof["groups"].items():
                    phases = ", ".join(f"{key}={seconds:.3f}" for key, seconds in sorted(value["phase_timings"].items()))
                    lines.append(f"| {group} | {value['case_count']} | {value['producer_attempt']} | {phases} |")
                lines.extend(["", "Phase totals describe executed work; they are not workflow critical-path measurements.", ""])
                _summary(lines)
        print(f"visual_ci_{action}_verified")
        return 0
    except VisualGateError as exc:
        print("visual_ci_refused:" + exc.code)
        failure_summary(exc.code, exc.case_id)
    except (ContractError, ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
        print("visual_ci_refused:invalid_or_unavailable_evidence")
        failure_summary("invalid_or_unavailable_evidence")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
