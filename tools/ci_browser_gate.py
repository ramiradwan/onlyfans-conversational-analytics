"""Verify complete browser execution before assembling existing Legal evidence.

The reporter is deliberately not imported. This consumer validates its closed
event protocol independently and binds each selected artifact to that job's
newest execution, including retained dependencies from partial reruns.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from tools.engineering_attestation import ContractError, GitHubApi, latest_ci_jobs, load_json_strict
except ModuleNotFoundError:
    from engineering_attestation import ContractError, GitHubApi, latest_ci_jobs, load_json_strict

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "tools/e2e-capture/ci/registry.json"
LANES = ("core", "catchup")
JOBS = {lane: f"browser-e2e-{lane}" for lane in LANES} | {"legacy": "browser-e2e-serial-control"}
HEX = re.compile(r"[a-f0-9]{64}")
RECEIPT_KEYS = {
    "schema", "source_commit", "workflow_run_id", "run_attempt", "logical_job", "lane",
    "platform", "playwright_version", "registry_sha256", "qualification", "collect_only",
    "inventory_exit_code", "execution_exit_code", "runner_exit_code", "interrupted",
    "inventory_sha256", "execution_sha256", "inventory_count", "execution_count",
    "completed_count", "status", "started_at", "finished_at", "duration_ms",
}
# These are the existing schema-v2 packager inputs. Do not copy an unrestricted
# test-results tree (which can contain browser profiles, screenshots or traces).
LEGAL_EVIDENCE_NAMES = (
    "ae-01-terms-abandoned", "ae-02-terms-risk-abandoned", "ae-03-preview-envelope",
    "ae-04-full-initial-authorization", "ae-05-preview-to-full", "ae-06-revocation",
    "ae-07-crash-retry", "ae-08-instrument-binding-negative", "ae-09-schema-v2-lock",
)
LEGAL_FILES = tuple(
    [f"legal/execution/scenario-results/AE-{n:02}.json" for n in range(1, 10)]
    + [f"legal/execution/evidence/{name}.json" for name in LEGAL_EVIDENCE_NAMES]
    + ["legal/execution/test-results/activation-evidence.tap", "legal/runtime-ui-proof.json"]
)


class BrowserGateError(ValueError):
    """The required browser execution has not been demonstrated."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BrowserGateError(message)


def _int(value: Any, label: str, minimum: int = 0) -> int:
    _require(type(value) is int and minimum <= value <= 9007199254740991, f"invalid {label}")
    return value


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == keys, f"invalid {label} fields")
    return value


def _read(path: Path) -> bytes:
    _require(path.is_file() and not any(part.is_symlink() for part in (path, *path.parents)),
             "missing or nonregular browser evidence")
    # Sidecars and explicit logs are small; reject an accidental database/profile.
    _require(path.stat().st_size <= 16 * 1024 * 1024, "browser evidence exceeds the file-size limit")
    return path.read_bytes()


def _json(path: Path) -> Any:
    return load_json_strict(_read(path), label=f"browser evidence {path.name}")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_registry(path: Path = REGISTRY) -> tuple[dict[str, Any], str]:
    raw = _read(path)
    document = load_json_strict(raw, label="browser CI registry")
    _object(document, {"schema", "playwright_version", "project", "repeat_index", "tests"}, "registry")
    _require(document["schema"] == "browser-ci-registry/v1"
             and document["project"] == "" and type(document["repeat_index"]) is int
             and document["repeat_index"] == 0
             and isinstance(document["playwright_version"], str)
             and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", document["playwright_version"]),
             "unsupported browser registry configuration")
    _require(isinstance(document["tests"], list) and bool(document["tests"]), "empty browser registry")
    ids: set[str] = set()
    identities: set[tuple[str, tuple[str, ...]]] = set()
    files: dict[str, str] = {}
    for row in document["tests"]:
        _object(row, {"id", "file", "title_path", "shard", "retry_override"}, "registry test")
        _require(isinstance(row["id"], str) and re.fullmatch(r"[a-zA-Z0-9_-]+", row["id"])
                 and row["id"] not in ids, "duplicate or invalid browser test ID")
        _require(isinstance(row["file"], str)
                 and re.fullmatch(r"tests/[a-zA-Z0-9_./-]+\.spec\.mjs", row["file"])
                 and ".." not in row["file"].split("/"), "invalid browser registry file")
        _require(isinstance(row["title_path"], list) and bool(row["title_path"])
                 and all(isinstance(x, str) and x for x in row["title_path"]), "invalid browser title identity")
        _require(row["shard"] in LANES and (row["retry_override"] is None
                 or type(row["retry_override"]) is int and row["retry_override"] == 0), "invalid browser shard/retry policy")
        identity = (row["file"], tuple(row["title_path"]))
        _require(identity not in identities, "duplicate browser registry identity")
        _require(files.setdefault(row["file"], row["shard"]) == row["shard"], "browser file split between lanes")
        ids.add(row["id"])
        identities.add(identity)
    _require({row["shard"] for row in document["tests"]} == set(LANES), "empty required browser lane")
    return document, _hash(raw)


def _events(data: bytes) -> list[dict[str, Any]]:
    _require(data.endswith(b"\n"), "browser event stream has an incomplete final record")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise BrowserGateError("browser event stream is not UTF-8") from exc
    _require(bool(lines) and all(lines), "empty or blank browser event stream")
    rows = []
    for sequence, line in enumerate(lines):
        row = load_json_strict(line.encode(), label="browser event")
        _require(isinstance(row, dict) and row.get("schema") == "browser-ci-events/v1"
                 and type(row.get("seq")) is int and row["seq"] == sequence,
                 "browser event sequence is incomplete or duplicated")
        rows.append(row)
    return rows


def validate_stream(data: bytes, *, registry: dict[str, Any], registry_digest: str,
                    lane: str, mode: str, qualification: bool) -> dict[str, Any]:
    rows = _events(data)
    _require(len(rows) >= 3, "incomplete browser stream")
    prefix = {"schema", "seq", "event"}
    begin = _object(rows[0], prefix | {"mode", "lane", "workers", "default_retries",
        "timeout_ms", "playwright_version", "registry_sha256"}, "browser begin")
    _require(begin["event"] == "begin" and begin["mode"] == mode and begin["lane"] == lane
             and type(begin["workers"]) is int and begin["workers"] == 1
             and type(begin["default_retries"]) is int and begin["default_retries"] == 1
             and type(begin["timeout_ms"]) is int and begin["timeout_ms"] == 180000
             and begin["playwright_version"] == registry["playwright_version"]
             and begin["registry_sha256"] == registry_digest, "browser configuration differs from the required CI contract")
    collection = _object(rows[1], prefix | {"tests"}, "browser collection")
    _require(collection["event"] == "collection" and isinstance(collection["tests"], list), "missing browser collection")
    expected = {row["id"]: row for row in registry["tests"]
                if mode == "inventory" or lane == "legacy" or row["shard"] == lane}
    selected: dict[str, int] = {}
    for row in collection["tests"]:
        _object(row, {"id", "retries", "repeat_index", "project"}, "collected browser test")
        test_id = row["id"]
        _require(isinstance(test_id, str) and test_id in expected and test_id not in selected,
                 "unknown or duplicate collected browser test")
        retries = 1 if expected[test_id]["retry_override"] is None else 0
        _require(type(row["retries"]) is int and row["retries"] == retries
                 and type(row["repeat_index"]) is int and row["repeat_index"] == 0
                 and row["project"] == registry["project"], "browser retries/project/repetition changed")
        selected[test_id] = retries
    _require(set(selected) == set(expected), "browser collection omits required tests")
    end = _object(rows[-1], prefix | {"status", "total", "completed", "global_errors"}, "browser end")
    _require(end["event"] == "end" and end["status"] == "passed"
             and _int(end["total"], "browser total") == len(expected)
             and _int(end["global_errors"], "global errors") == 0, "browser stream did not complete successfully")
    if mode == "inventory":
        _require(len(rows) == 3 and _int(end["completed"], "inventory completed") == 0,
                 "collection reference unexpectedly claims execution")
        return {"ids": set(selected), "attempts": 0, "retries": 0}
    _require(_int(end["completed"], "browser completed") == len(expected), "incomplete browser execution")
    attempts: dict[str, list[str]] = {test_id: [] for test_id in selected}
    active: tuple[str, int] | None = None
    outcomes: set[str] = set()
    duration_ms = 0
    outcome_phase = False
    for row in rows[2:-1]:
        event = row.get("event")
        test_id = row.get("id")
        _require(isinstance(test_id, str) and test_id in selected, "browser event has no selected identity")
        if event == "attempt_begin":
            _object(row, prefix | {"id", "retry", "worker_index"}, "browser attempt begin")
            retry = _int(row["retry"], "browser retry")
            _int(row["worker_index"], "browser worker index")
            _require(not outcome_phase and active is None and test_id not in outcomes and retry == len(attempts[test_id])
                     and retry <= selected[test_id] and (not qualification or retry == 0)
                     and (retry == 0 or attempts[test_id][-1] in {"failed", "timedOut"}),
                     "duplicate, overlapping, unauthorized or noncontiguous browser attempt")
            active = (test_id, retry)
        elif event == "attempt_end":
            _object(row, prefix | {"id", "retry", "status", "expected_status", "duration_ms"}, "browser attempt end")
            _int(row["retry"], "browser retry")
            _int(row["duration_ms"], "browser duration")
            duration_ms += row["duration_ms"]
            _require(not outcome_phase and active == (test_id, row["retry"]) and row["expected_status"] == "passed"
                     and row["status"] in {"passed", "failed", "timedOut"}, "missing, skipped, interrupted or invalid browser attempt")
            attempts[test_id].append(row["status"])
            active = None
        elif event == "outcome":
            outcome_phase = True
            _object(row, prefix | {"id", "outcome", "expected_status", "attempts"}, "browser outcome")
            history = attempts[test_id]
            _require(active is None and test_id not in outcomes and bool(history) and history[-1] == "passed"
                     and row["expected_status"] == "passed"
                     and _int(row["attempts"], "browser attempt count", 1) == len(history)
                     and row["outcome"] == ("flaky" if len(history) > 1 else "expected"),
                     "browser terminal outcome is missing, failed or inconsistent")
            outcomes.add(test_id)
        else:
            raise BrowserGateError("unsupported browser event or reporter fault")
    _require(active is None and outcomes == set(selected), "browser execution has unfinished or missing tests")
    return {"ids": set(selected), "attempts": sum(map(len, attempts.values())),
            "retries": sum(len(values) - 1 for values in attempts.values()), "duration_ms": duration_ms,
            "outcomes": {test_id: "flaky" if len(history) > 1 else "expected"
                         for test_id, history in attempts.items()}}


def _payload_paths(lane: str) -> set[str]:
    return {"browser-e2e.txt"} | (set(LEGAL_FILES) if lane == "core" else set())


def _safe_receipt(receipt: Any, registry: dict[str, Any], digest: str) -> dict[str, Any]:
    """Closed fields only, including failed/partial runs; never echo bad input."""
    _object(receipt, RECEIPT_KEYS, "browser receipt")
    lane = receipt["lane"]
    _require(isinstance(lane, str) and lane in JOBS
             and receipt["schema"] == "browser-ci-receipt/v1"
             and isinstance(receipt["source_commit"], str)
             and re.fullmatch(r"[a-f0-9]{40}", receipt["source_commit"])
             and isinstance(receipt["workflow_run_id"], str)
             and re.fullmatch(r"[1-9][0-9]*", receipt["workflow_run_id"])
             and receipt["logical_job"] == JOBS[lane]
             and receipt["platform"] in {"Windows", "Linux", "Darwin"}
             and receipt["playwright_version"] == registry["playwright_version"]
             and receipt["registry_sha256"] == digest
             and receipt["status"] in {"passed", "failed", "interrupted"}, "unsafe browser receipt values")
    _int(receipt["run_attempt"], "browser run attempt", 1)
    for key in ("qualification", "collect_only", "interrupted"):
        _require(type(receipt[key]) is bool, "unsafe browser receipt flags")
    for key in ("duration_ms", "inventory_count", "execution_count", "completed_count", "runner_exit_code"):
        _int(receipt[key], "browser receipt number")
    for mode in ("inventory", "execution"):
        code, value = receipt[f"{mode}_exit_code"], receipt[f"{mode}_sha256"]
        _require(code is None or type(code) is int and 0 <= code <= 4294967295, "unsafe browser exit code")
        _require(value is None or isinstance(value, str) and HEX.fullmatch(value), "unsafe browser digest")
    for key in ("started_at", "finished_at"):
        _require(isinstance(receipt[key], str)
                 and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", receipt[key]),
                 "unsafe browser timestamp")
    return receipt


def _safe_events(data: bytes, registry: dict[str, Any], digest: str) -> None:
    """Allow partial diagnostic streams only after independent field validation."""
    ids = {test["id"] for test in registry["tests"]}
    prefix = {"schema", "seq", "event"}
    statuses = {"passed", "failed", "timedOut", "skipped", "interrupted"}
    for row in _events(data):
        event = row.get("event")
        if event == "begin":
            _object(row, prefix | {"mode", "lane", "workers", "default_retries", "timeout_ms",
                    "playwright_version", "registry_sha256"}, "diagnostic begin")
            _require(row["mode"] in {"inventory", "execution"} and row["lane"] in JOBS
                     and type(row["workers"]) is int and row["workers"] == 1
                     and type(row["default_retries"]) is int and row["default_retries"] in {0, 1}
                     and type(row["timeout_ms"]) is int and row["timeout_ms"] == 180000
                     and row["playwright_version"] == registry["playwright_version"]
                     and row["registry_sha256"] == digest, "unsafe diagnostic configuration")
        elif event == "collection":
            _object(row, prefix | {"tests"}, "diagnostic collection")
            _require(isinstance(row["tests"], list) and len(row["tests"]) <= len(ids), "unsafe diagnostic collection")
            for test in row["tests"]:
                _object(test, {"id", "retries", "repeat_index", "project"}, "diagnostic test")
                _require(test["id"] in ids and type(test["retries"]) is int and test["retries"] in {0, 1}
                         and type(test["repeat_index"]) is int and test["repeat_index"] == 0
                         and test["project"] == "", "unsafe diagnostic test")
        elif event in {"attempt_begin", "attempt_end", "outcome"}:
            keys = {"attempt_begin": {"id", "retry", "worker_index"},
                    "attempt_end": {"id", "retry", "status", "expected_status", "duration_ms"},
                    "outcome": {"id", "outcome", "expected_status", "attempts"}}[event]
            _object(row, prefix | keys, "diagnostic execution")
            _require(row["id"] in ids, "unsafe diagnostic identity")
            for key in keys & {"retry", "worker_index", "duration_ms", "attempts"}:
                _int(row[key], "diagnostic number")
            if "expected_status" in row:
                _require(row["expected_status"] in statuses, "unsafe diagnostic expected status")
            if "status" in row:
                _require(row["status"] in statuses, "unsafe diagnostic status")
            if "outcome" in row:
                _require(row["outcome"] in {"expected", "unexpected", "flaky", "skipped"}, "unsafe diagnostic outcome")
        elif event == "end":
            _object(row, prefix | {"status", "total", "completed", "global_errors"}, "diagnostic end")
            _require(row["status"] in {"passed", "failed", "timedout", "interrupted"}, "unsafe diagnostic terminal")
            for key in ("total", "completed", "global_errors"):
                _int(row[key], "diagnostic count")
        elif event == "fault":
            _object(row, prefix | {"code"}, "diagnostic fault")
            _require(row["code"] in {"unknown_identity", "duplicate_identity", "invalid_callback",
                     "invalid_status", "global_error", "invalid_config", "write_error"}, "unsafe diagnostic fault")
        else:
            raise BrowserGateError("unsafe diagnostic event")


def publish_diagnostics(directory: Path, destination: Path, registry: dict[str, Any], digest: str) -> None:
    _require(not destination.exists(), "diagnostic publication directory already exists")
    destination.mkdir(parents=True)
    published = []
    for name in ("receipt.json", "inventory.ndjson", "execution.ndjson"):
        try:
            data = _read(directory / name)
            if name == "receipt.json":
                _safe_receipt(load_json_strict(data, label="browser receipt"), registry, digest)
            else:
                _safe_events(data, registry, digest)
            (destination / name).write_bytes(data)
            published.append(name)
        except (BrowserGateError, ContractError, OSError, TypeError, ValueError, KeyError):
            # Only this constant code is published; attacker-controlled JSON
            # keys, error messages and raw Playwright output never reach it.
            pass
    document = {"schema": "browser-ci-diagnostics/v1", "published": published,
                "status": "restricted_metadata" if published else "metadata_unavailable"}
    (destination / "diagnostic.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def publish_complete(directory: Path, destination: Path, *, registry: dict[str, Any], digest: str) -> None:
    receipt = _safe_receipt(_json(directory / "receipt.json"), registry, digest)
    _require(receipt["source_commit"] == os.environ.get("PRODUCT_SHA")
             and receipt["workflow_run_id"] == os.environ.get("GITHUB_RUN_ID")
             and str(receipt["run_attempt"]) == os.environ.get("GITHUB_RUN_ATTEMPT"),
             "producer receipt does not match the running workflow")
    flag = os.environ.get("BROWSER_QUALIFICATION", "false")
    _require(flag in {"true", "false"}, "invalid qualification mode")
    seal_payload(directory)
    validate_receipt(directory, registry=registry, registry_digest=digest,
                     source=receipt["source_commit"], run_id=int(receipt["workflow_run_id"]),
                     job={"run_attempt": receipt["run_attempt"]}, lane=receipt["lane"], qualification=flag == "true")
    _require(not destination.exists(), "complete publication directory already exists")
    for name in sorted(_payload_paths(receipt["lane"]) | {"receipt.json", "inventory.ndjson", "execution.ndjson", "payload.json"}):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_read(directory / name))


def seal_payload(directory: Path) -> None:
    receipt = _json(directory / "receipt.json")
    _require(isinstance(receipt, dict) and receipt.get("lane") in JOBS, "cannot seal an unidentified browser lane")
    payload = {"schema": "browser-ci-payload/v1", "receipt_sha256": _hash(_read(directory / "receipt.json")),
               "files": {name: _hash(_read(directory / name)) for name in sorted(_payload_paths(receipt["lane"]))}}
    (directory / "payload.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _verify_payload(directory: Path, lane: str) -> None:
    payload = _object(_json(directory / "payload.json"), {"schema", "receipt_sha256", "files"}, "browser payload")
    _require(payload["schema"] == "browser-ci-payload/v1"
             and payload["receipt_sha256"] == _hash(_read(directory / "receipt.json"))
             and isinstance(payload["files"], dict) and set(payload["files"]) == _payload_paths(lane),
             "browser payload is unbound or incomplete")
    for name, digest in payload["files"].items():
        _require(isinstance(digest, str) and HEX.fullmatch(digest)
                 and digest == _hash(_read(directory / name)), "browser payload bytes differ from the receipt")
    _require(bool(_read(directory / "browser-e2e.txt").strip()), "browser console output is empty")


def validate_receipt(directory: Path, *, registry: dict[str, Any], registry_digest: str,
                     source: str, run_id: int, job: dict[str, Any], lane: str,
                     qualification: bool) -> dict[str, Any]:
    receipt = _safe_receipt(_json(directory / "receipt.json"), registry, registry_digest)
    _require(receipt["schema"] == "browser-ci-receipt/v1" and receipt["source_commit"] == source
             and receipt["workflow_run_id"] == str(run_id)
             and type(receipt["run_attempt"]) is int and receipt["run_attempt"] == job["run_attempt"]
             and receipt["logical_job"] == JOBS[lane] and receipt["lane"] == lane
             and receipt["platform"] == "Windows" and receipt["qualification"] is qualification
             and receipt["playwright_version"] == registry["playwright_version"]
             and receipt["registry_sha256"] == registry_digest, "browser receipt source, execution or configuration mismatch")
    _require(receipt["status"] == "passed" and receipt["collect_only"] is False
             and receipt["interrupted"] is False and all(type(receipt[k]) is int and receipt[k] == 0
                 for k in ("inventory_exit_code", "execution_exit_code", "runner_exit_code")),
             "browser runner failed, was interrupted or only collected tests")
    _int(receipt["duration_ms"], "browser runner duration")
    try:
        start, finish = (datetime.fromisoformat(receipt[key].replace("Z", "+00:00"))
                         for key in ("started_at", "finished_at"))
        _require(start.tzinfo is not None and finish.tzinfo is not None and finish >= start,
                 "invalid browser receipt timestamps")
    except (TypeError, ValueError, AttributeError) as exc:
        raise BrowserGateError("invalid browser receipt timestamps") from exc
    result = {}
    for mode in ("inventory", "execution"):
        data = _read(directory / f"{mode}.ndjson")
        _require(receipt[f"{mode}_sha256"] == _hash(data), "browser sidecar digest mismatch")
        result[mode] = validate_stream(data, registry=registry, registry_digest=registry_digest,
                                      lane=lane, mode=mode, qualification=qualification)
        _require(_int(receipt[f"{mode}_count"], "receipt collection count") == len(result[mode]["ids"]),
                 "browser receipt collection count mismatch")
    _require(_int(receipt["completed_count"], "receipt completed count") == len(result["execution"]["ids"]),
             "browser receipt completion count mismatch")
    _verify_payload(directory, lane)
    return result


def select_evidence(directory: Path, *, jobs: dict[str, dict[str, Any]], source: str,
                    run_id: int, lanes: tuple[str, ...] = LANES) -> dict[str, Path]:
    chosen: dict[str, Path] = {}
    identities: set[tuple[str, int]] = set()
    for path in sorted(directory.rglob("receipt.json")):
        receipt = _object(_json(path), RECEIPT_KEYS, "browser receipt")
        lane, attempt = receipt["lane"], receipt["run_attempt"]
        _require(lane in lanes and receipt["source_commit"] == source
                 and receipt["workflow_run_id"] == str(run_id)
                 and receipt["logical_job"] == JOBS[lane], "unexpected browser artifact identity")
        _int(attempt, "browser artifact attempt", 1)
        _require(path.parent.name == f"browser-e2e-inputs-{lane}-{source}-{run_id}-{attempt}",
                 "browser artifact name does not bind its producer identity")
        _require((lane, attempt) not in identities, "duplicate browser artifact for one execution")
        identities.add((lane, attempt))
        latest = jobs[JOBS[lane]]["run_attempt"]
        _require(attempt <= latest, "browser artifact claims a nonexistent newer execution")
        if attempt == latest:
            chosen[lane] = path.parent
    _require(set(chosen) == set(lanes), "missing evidence for the newest browser job execution")
    return chosen


def validate_jobs(needs: Any, jobs: dict[str, dict[str, Any]], *, serial: bool = False,
                  serial_requested: bool = False, qualification: bool = False) -> None:
    _require(type(serial_requested) is bool and type(qualification) is bool
             and (not serial_requested or qualification), "serial control requires qualification mode")
    names = {"browser-reporting-safety", JOBS["legacy"]} | (set() if serial else {JOBS[lane] for lane in LANES})
    dependencies = {"browser-reporting-safety", JOBS["legacy"]} | (set() if serial else {"browser-e2e-execution"})
    _object(needs, dependencies, "browser gate dependencies")
    for name, value in needs.items():
        expected = "skipped" if name == JOBS["legacy"] and not serial and not serial_requested else "success"
        _require(isinstance(value, dict) and value.get("result") == expected,
                 "a mandatory browser dependency failed or an optional control had an invalid result")
    for name in names:
        job = jobs.get(name, {})
        expected = "skipped" if name == JOBS["legacy"] and not serial and not serial_requested else "success"
        _require(job.get("status") == "completed" and job.get("conclusion") == expected,
                 f"newest mandatory browser job {name} did not succeed")


def assemble_legal_inputs(chosen: dict[str, Path], execution_root: Path, runtime_proof: Path) -> None:
    """Preserve raw bytes; the existing Node packager still verifies Legal semantics."""
    _require(not execution_root.exists() or not any(execution_root.iterdir()), "Legal assembly destination is not empty")
    _require(not runtime_proof.exists(), "Legal runtime proof destination already exists")
    core = chosen["core"]
    for relative in LEGAL_FILES:
        source = core / relative
        destination = (runtime_proof if relative == "legal/runtime-ui-proof.json" else
                       execution_root / relative.removeprefix("legal/execution/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    output = execution_root / "test-results/browser-e2e.txt"
    with output.open("wb") as stream:
        for lane in LANES:
            stream.write(f"===== BEGIN BROWSER LANE {lane} =====\n".encode("ascii"))
            stream.write(_read(chosen[lane] / "browser-e2e.txt"))
            stream.write(f"\n===== END BROWSER LANE {lane} =====\n".encode("ascii"))


def validate_evidence(directory: Path, *, jobs: dict[str, dict[str, Any]], source: str,
                      run_id: int, current_attempt: int, qualification: bool,
                      serial: bool = False, serial_requested: bool = False,
                      registry_path: Path = REGISTRY) -> tuple[dict[str, Path], dict[str, Any]]:
    """JSON-safe proof for same-SHA comparisons; does not claim paired qualification.

    Callers must obtain ``jobs`` through latest_ci_jobs and validate_jobs first.
    The ordinary gate never accepts another run's producer evidence. A separate
    qualification comparison verifies an independently collected serial control
    and both split producers within the same workflow run through this API.
    """
    registry, digest = load_registry(registry_path)
    _int(run_id, "workflow run", 1)
    _int(current_attempt, "workflow attempt", 1)
    _require(not (serial and serial_requested) and (not serial_requested or qualification),
             "paired serial control requires qualification mode")
    lanes = ("legacy",) if serial else LANES + (("legacy",) if serial_requested else ())
    for lane in lanes:
        job = jobs.get(JOBS[lane], {})
        _require(job.get("name") == JOBS[lane] and job.get("status") == "completed"
                 and job.get("conclusion") == "success" and job.get("head_sha") == source
                 and type(job.get("run_id")) is int and job["run_id"] == run_id
                 and type(job.get("run_attempt")) is int and 1 <= job["run_attempt"] <= current_attempt
                 and type(job.get("id")) is int and job["id"] > 0,
                 "browser proof has an invalid or unsuccessful newest producer job")
    chosen = select_evidence(directory, jobs=jobs, source=source, run_id=run_id, lanes=lanes)
    results = {lane: validate_receipt(path, registry=registry, registry_digest=digest,
                source=source, run_id=run_id, job=jobs[JOBS[lane]], lane=lane,
                qualification=qualification) for lane, path in chosen.items()}
    full = {row["id"] for row in registry["tests"]}
    union: set[str] = set()
    summaries = {}
    for lane, result in results.items():
        _require(result["inventory"]["ids"] == full, "independent full inventory differs between browser lanes")
        if lane != "legacy" or serial:
            _require(not union & result["execution"]["ids"], "browser lanes overlap")
            union |= result["execution"]["ids"]
        receipt = _json(chosen[lane] / "receipt.json")
        summaries[lane] = {"producer_attempt": receipt["run_attempt"], "job_id": jobs[JOBS[lane]]["id"],
            "selected_ids": sorted(result["execution"]["ids"]), "selected_count": len(result["execution"]["ids"]),
            "outcomes": result["execution"]["outcomes"], "retries": result["execution"]["retries"],
            "runner_duration_ms": receipt["duration_ms"], "execution_duration_ms": result["execution"]["duration_ms"],
            "receipt_sha256": _hash(_read(chosen[lane] / "receipt.json"))}
    _require(union == full, "browser lanes do not cover the full inventory")
    def proof(selected_summaries: dict[str, Any], is_serial: bool) -> dict[str, Any]:
        return {"schema": "browser-ci-verified/v1", "source_commit": source,
            "workflow_run_id": run_id, "current_attempt": current_attempt, "qualification": qualification,
            "serial_control": is_serial, "registry_sha256": digest, "selected_ids": sorted(full),
            "selected_count": len(full), "retries": sum(item["retries"] for item in selected_summaries.values()),
            "lanes": selected_summaries, "paired_control": False, "control": None}
    if serial_requested:
        split_outcomes = {test_id: outcome for lane in LANES for test_id, outcome in summaries[lane]["outcomes"].items()}
        _require(results["legacy"]["execution"]["ids"] == full
                 and summaries["legacy"]["outcomes"] == split_outcomes,
                 "serial control differs from the complete split execution")
        control = proof({"legacy": summaries["legacy"]}, True)
        result = proof({lane: summaries[lane] for lane in LANES}, False)
        result.update(paired_control=True, control=control)
    else:
        result = proof(summaries, serial)
    return chosen, result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "seal":
        parser = argparse.ArgumentParser(description="Bind only approved raw console and Legal inputs")
        parser.add_argument("--artifact-dir", type=Path, required=True)
        parser.add_argument("--publish-dir", type=Path, required=True)
        parser.add_argument("--diagnostics-dir", type=Path, required=True)
        args = parser.parse_args(argv[1:])
        try:
            registry, digest = load_registry()
            publish_diagnostics(args.artifact_dir, args.diagnostics_dir, registry, digest)
            publish_complete(args.artifact_dir, args.publish_dir, registry=registry, digest=digest)
            return 0
        except (BrowserGateError, ContractError, OSError, TypeError, ValueError, KeyError):
            print("Browser evidence sealing failed; only independently restricted diagnostics may be published")
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--needs-json", default=os.environ.get("CI_NEEDS_JSON", ""))
    parser.add_argument("--execution-root", type=Path, default=Path("artifacts/legal/execution"))
    parser.add_argument("--runtime-proof", type=Path, default=Path("artifacts/legal/runtime-ui-proof.json"))
    parser.add_argument("--serial-control", action="store_true")
    parser.add_argument("--verified-output", type=Path)
    args = parser.parse_args(argv)
    try:
        source = os.environ.get("PRODUCT_SHA", "")
        _require(bool(re.fullmatch(r"[a-f0-9]{40}", source)), "PRODUCT_SHA is not an exact source commit")
        run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
        attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
        _int(run_id, "workflow run", 1)
        _int(attempt, "workflow attempt", 1)
        qualification_value = os.environ.get("BROWSER_QUALIFICATION", "false")
        _require(qualification_value in {"true", "false"}, "invalid browser qualification flag")
        qualification = qualification_value == "true"
        serial_value = os.environ.get("BROWSER_SERIAL_CONTROL", "false")
        _require(serial_value in {"true", "false"}, "invalid browser serial control flag")
        serial_requested = serial_value == "true"
        _require(not serial_requested or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
                 "serial control must be explicitly requested by workflow dispatch")
        client = GitHubApi(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""))
        jobs = latest_ci_jobs(client, run_id=run_id, run_attempt=attempt, source_commit=source)
        needs = load_json_strict(args.needs_json.encode(), label="browser gate dependencies")
        validate_jobs(needs, jobs, serial=args.serial_control, serial_requested=serial_requested,
                      qualification=qualification)
        chosen, proof = validate_evidence(args.reports_dir, jobs=jobs, source=source,
            run_id=run_id, current_attempt=attempt, qualification=qualification, serial=args.serial_control,
            serial_requested=serial_requested)
        if not args.serial_control:
            assemble_legal_inputs(chosen, args.execution_root, args.runtime_proof)
        if args.verified_output:
            args.verified_output.parent.mkdir(parents=True, exist_ok=True)
            args.verified_output.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
        message = f"Browser evidence verified: tests={proof['selected_count']}, retries={proof['retries']}, qualification={str(qualification).lower()}"
        code = 0
    except (BrowserGateError, ContractError, ValueError, TypeError, KeyError, OSError):
        message, code = "Browser evidence refused: failed, incomplete, stale or invalid execution evidence", 1
    print(message)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write("\n## Browser evidence\n\n" + message + "\n")
        except OSError:
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
