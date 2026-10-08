"""Independent browser evidence and safe artifact publication falsifiers."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools import ci_browser_gate as gate

pytestmark = pytest.mark.ci_tier("fast")
SOURCE = "a" * 40


@pytest.fixture
def registry():
    return gate.load_registry()


def _stream(registry, digest, lane, *, inventory=False, retry_id=None):
    rows = [{"event": "begin", "mode": "inventory" if inventory else "execution", "lane": lane,
             "workers": 1, "default_retries": 1, "timeout_ms": 180000,
             "playwright_version": registry["playwright_version"], "registry_sha256": digest}]
    tests = [test for test in registry["tests"] if inventory or lane == "legacy" or test["shard"] == lane]
    rows.append({"event": "collection", "tests": [{"id": test["id"], "retries": 0 if test["retry_override"] == 0 else 1,
                 "repeat_index": 0, "project": ""} for test in tests]})
    if not inventory:
        for index, test in enumerate(tests):
            statuses = ["failed", "passed"] if test["id"] == retry_id else ["passed"]
            for retry, status in enumerate(statuses):
                rows.extend([
                    {"event": "attempt_begin", "id": test["id"], "retry": retry, "worker_index": index},
                    {"event": "attempt_end", "id": test["id"], "retry": retry, "status": status,
                     "expected_status": "passed", "duration_ms": 10},
                ])
        for test in tests:
            retry = test["id"] == retry_id
            rows.append({"event": "outcome", "id": test["id"], "outcome": "flaky" if retry else "expected",
                         "expected_status": "passed", "attempts": 2 if retry else 1})
    rows.append({"event": "end", "status": "passed", "total": len(tests),
                 "completed": 0 if inventory else len(tests), "global_errors": 0})
    return [{"schema": "browser-ci-events/v1", "seq": index, **row} for index, row in enumerate(rows)]


def _bytes(rows):
    return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()


def _artifact(root, registry_pair, lane, *, attempt=1, qualification=False, retry_id=None):
    registry, digest = registry_pair
    directory = root / f"browser-e2e-inputs-{lane}-{SOURCE}-42-{attempt}"
    directory.mkdir(parents=True)
    inventory = _bytes(_stream(registry, digest, lane, inventory=True))
    execution = _bytes(_stream(registry, digest, lane, retry_id=retry_id))
    (directory / "inventory.ndjson").write_bytes(inventory)
    (directory / "execution.ndjson").write_bytes(execution)
    selected = [test for test in registry["tests"] if lane == "legacy" or test["shard"] == lane]
    receipt = {"schema": "browser-ci-receipt/v1", "source_commit": SOURCE,
        "workflow_run_id": "42", "run_attempt": attempt, "logical_job": gate.JOBS[lane], "lane": lane,
        "platform": "Windows", "playwright_version": registry["playwright_version"], "registry_sha256": digest,
        "qualification": qualification, "collect_only": False, "inventory_exit_code": 0,
        "execution_exit_code": 0, "runner_exit_code": 0, "interrupted": False,
        "inventory_sha256": gate._hash(inventory), "execution_sha256": gate._hash(execution),
        "inventory_count": len(registry["tests"]), "execution_count": len(selected), "completed_count": len(selected),
        "status": "passed", "started_at": "2026-10-03T12:00:00.000Z", "finished_at": "2026-10-03T12:01:00.000Z", "duration_ms": 60000}
    (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    for name in gate._payload_paths(lane):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"source":"synthetic fixture"}' if name.endswith(".json") else b"original console \x1b[0m\r\n")
    gate.seal_payload(directory)
    return directory


def _jobs(**attempts):
    return {name: {"name": name, "id": 100 + index, "run_id": 42, "run_attempt": attempts.get(lane, 1),
                   "head_sha": SOURCE, "status": "completed", "conclusion": "success"}
            for index, (lane, name) in enumerate({**gate.JOBS, "safety": "browser-reporting-safety"}.items())}


def _verify(path, registry_pair, lane="core", qualification=False, attempt=1):
    return gate.validate_receipt(path, registry=registry_pair[0], registry_digest=registry_pair[1],
        source=SOURCE, run_id=42, job={"run_attempt": attempt}, lane=lane, qualification=qualification)


def _mutate_stream(path, filename, mutate):
    rows = [json.loads(line) for line in (path / filename).read_text().splitlines()]
    mutate(rows)
    data = _bytes(rows)
    (path / filename).write_bytes(data)
    receipt = json.loads((path / "receipt.json").read_text())
    receipt[filename.split(".")[0] + "_sha256"] = gate._hash(data)
    (path / "receipt.json").write_text(json.dumps(receipt))
    gate.seal_payload(path)


def test_complete_disjoint_lanes_and_retained_dependency_produce_exact_proof(tmp_path, registry):
    _artifact(tmp_path, registry, "core", attempt=1)
    _artifact(tmp_path, registry, "catchup", attempt=1)
    newest = _artifact(tmp_path, registry, "catchup", attempt=2)
    chosen, proof = gate.validate_evidence(tmp_path, jobs=_jobs(catchup=2), source=SOURCE,
        run_id=42, current_attempt=2, qualification=False)
    assert chosen["catchup"] == newest
    assert proof["selected_count"] == 15
    assert proof["lanes"]["core"]["producer_attempt"] == 1
    assert proof["lanes"]["catchup"]["producer_attempt"] == 2
    assert proof["retries"] == 0
    assert proof["selected_ids"] == sorted(test["id"] for test in registry[0]["tests"])


def test_serial_control_verifies_complete_inventory_without_claiming_a_pair(tmp_path, registry):
    _artifact(tmp_path, registry, "legacy", qualification=True)
    _, proof = gate.validate_evidence(tmp_path, jobs=_jobs(), source=SOURCE, run_id=42,
        current_attempt=1, qualification=True, serial=True)
    assert proof["serial_control"] is True and proof["selected_count"] == 15
    assert set(proof["lanes"]) == {"legacy"}


def test_normal_retry_success_is_preserved_but_cannot_qualify(tmp_path, registry):
    path = _artifact(tmp_path, registry, "core", retry_id="capture-replay")
    result = _verify(path, registry)
    assert result["execution"]["retries"] == 1
    assert result["execution"]["outcomes"]["capture-replay"] == "flaky"
    receipt = json.loads((path / "receipt.json").read_text())
    receipt["qualification"] = True
    (path / "receipt.json").write_text(json.dumps(receipt))
    gate.seal_payload(path)
    with pytest.raises(gate.BrowserGateError, match="attempt"):
        _verify(path, registry, qualification=True)


@pytest.mark.parametrize("mutation", [
    lambda rows: rows[1]["tests"].pop(),
    lambda rows: rows[1]["tests"].append(copy.deepcopy(rows[1]["tests"][0])),
    lambda rows: rows[1]["tests"][0].update(id="unregistered-test"),
    lambda rows: rows[1]["tests"][0].update(project="other"),
    lambda rows: rows[1]["tests"][0].update(repeat_index=1),
    lambda rows: rows[0].update(workers=2),
    lambda rows: rows[0].update(default_retries=0),
    lambda rows: rows[0].update(timeout_ms=300000),
    lambda rows: rows[-1].update(global_errors=1),
    lambda rows: rows[-1].update(status="failed"),
    lambda rows: rows[-1].update(completed=0),
    lambda rows: rows[2].update(retry=1),
    lambda rows: rows[3].update(status="skipped"),
    lambda rows: rows[3].update(expected_status="failed"),
    lambda rows: rows[3].update(status="interrupted"),
    lambda rows: rows[3].update(duration_ms=True),
    lambda rows: rows.pop(3),
    lambda rows: rows[-2].update(outcome="unexpected"),
    lambda rows: rows[-2].update(attempts=2),
    lambda rows: rows[-2].update(outcome="skipped"),
    lambda rows: rows.append(copy.deepcopy(rows[-1])),
    lambda rows: rows[2].update(extra="must never be accepted"),
])
def test_resealed_invalid_execution_cannot_pass(tmp_path, registry, mutation):
    path = _artifact(tmp_path, registry, "core")
    _mutate_stream(path, "execution.ndjson", mutation)
    with pytest.raises(gate.BrowserGateError):
        _verify(path, registry)


def test_inventory_is_an_independent_full_collection_not_lane_subset(tmp_path, registry):
    path = _artifact(tmp_path, registry, "core")
    _mutate_stream(path, "inventory.ndjson", lambda rows: rows[1].update(tests=[
        row for row in rows[1]["tests"] if not row["id"].startswith("catchup-")]))
    with pytest.raises(gate.BrowserGateError, match="omits"):
        _verify(path, registry)


def test_outcomes_are_terminal_and_no_later_execution_can_be_spliced_in(tmp_path, registry):
    path = _artifact(tmp_path, registry, "core")
    def reorder(rows):
        index = next(index for index, row in enumerate(rows) if row["event"] == "outcome")
        rows.insert(4, rows.pop(index))
        for sequence, row in enumerate(rows):
            row["seq"] = sequence
    _mutate_stream(path, "execution.ndjson", reorder)
    with pytest.raises(gate.BrowserGateError, match="attempt"):
        _verify(path, registry)


def test_even_a_complete_json_terminal_requires_the_written_record_delimiter(registry):
    data = _bytes(_stream(*registry, "core"))
    with pytest.raises(gate.BrowserGateError, match="final record"):
        gate.validate_stream(data[:-1], registry=registry[0], registry_digest=registry[1],
                             lane="core", mode="execution", qualification=False)


@pytest.mark.parametrize("field,value", [
    ("source_commit", "b" * 40), ("workflow_run_id", "99"), ("run_attempt", 2),
    ("logical_job", "browser-e2e-catchup"), ("platform", "Linux"), ("collect_only", True),
    ("interrupted", True), ("execution_exit_code", 1), ("runner_exit_code", True),
    ("completed_count", 1), ("status", "failed"), ("inventory_sha256", "0" * 64),
])
def test_receipt_cannot_substitute_source_or_execution(tmp_path, registry, field, value):
    path = _artifact(tmp_path, registry, "core")
    receipt = json.loads((path / "receipt.json").read_text())
    receipt[field] = value
    (path / "receipt.json").write_text(json.dumps(receipt))
    gate.seal_payload(path)
    with pytest.raises(gate.BrowserGateError):
        _verify(path, registry)


def test_payload_tampering_or_unbound_extra_file_is_refused(tmp_path, registry):
    path = _artifact(tmp_path, registry, "core")
    (path / "browser-e2e.txt").write_bytes(b"forged pass summary")
    with pytest.raises(gate.BrowserGateError, match="payload"):
        _verify(path, registry)
    gate.seal_payload(path)
    payload = json.loads((path / "payload.json").read_text())
    payload["files"]["trace.zip"] = "0" * 64
    (path / "payload.json").write_text(json.dumps(payload))
    with pytest.raises(gate.BrowserGateError, match="payload"):
        _verify(path, registry)


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_newer_failed_job_invalidates_older_success(result):
    jobs = _jobs(core=2)
    jobs["browser-e2e-core"]["conclusion"] = result
    jobs["browser-e2e-serial-control"]["conclusion"] = "skipped"
    needs = _needs()
    with pytest.raises(gate.BrowserGateError, match="newest"):
        gate.validate_jobs(needs, jobs)


def test_old_report_cannot_stand_in_for_newest_execution(tmp_path, registry):
    _artifact(tmp_path, registry, "core")
    _artifact(tmp_path, registry, "catchup")
    with pytest.raises(gate.BrowserGateError, match="newest"):
        gate.select_evidence(tmp_path, jobs=_jobs(core=2), source=SOURCE, run_id=42)


def test_artifact_name_is_part_of_immutable_execution_binding(tmp_path, registry):
    path = _artifact(tmp_path, registry, "core")
    path.rename(tmp_path / "older-sha-artifact")
    with pytest.raises(gate.BrowserGateError, match="artifact name"):
        gate.select_evidence(tmp_path, jobs=_jobs(), source=SOURCE, run_id=42)


def test_legal_assembly_preserves_real_console_bytes_and_existing_file_layout(tmp_path, registry):
    chosen = {lane: _artifact(tmp_path / "inputs", registry, lane) for lane in gate.LANES}
    core = b"core actual line output\r\n\x1b[0m13 passed\r\n"
    catchup = b"catchup actual line output\r\n2 passed\r\n"
    (chosen["core"] / "browser-e2e.txt").write_bytes(core)
    (chosen["catchup"] / "browser-e2e.txt").write_bytes(catchup)
    execution, runtime = tmp_path / "execution", tmp_path / "runtime.json"
    gate.assemble_legal_inputs(chosen, execution, runtime)
    assert (execution / "test-results/browser-e2e.txt").read_bytes() == (
        b"===== BEGIN BROWSER LANE core =====\n" + core + b"\n===== END BROWSER LANE core =====\n"
        b"===== BEGIN BROWSER LANE catchup =====\n" + catchup + b"\n===== END BROWSER LANE catchup =====\n")
    assert runtime.read_bytes() == (chosen["core"] / "legal/runtime-ui-proof.json").read_bytes()
    assert (execution / "scenario-results/AE-09.json").read_bytes() == (
        chosen["core"] / "legal/execution/scenario-results/AE-09.json").read_bytes()
    with pytest.raises(gate.BrowserGateError, match="not empty"):
        gate.assemble_legal_inputs(chosen, execution, runtime)


@pytest.mark.parametrize("kind", ["extra", "identity", "duplicate-key", "fault", "truncated"])
def test_partial_publication_never_copies_arbitrary_or_malformed_metadata(tmp_path, registry, kind):
    path = _artifact(tmp_path / "raw", registry, "core")
    canary = "private-account-example-do-not-publish"
    (path / "receipt.json").write_text(json.dumps({"unknown": canary}))
    rows = _stream(*registry, "core")
    if kind == "extra":
        rows[2]["error"] = {"message": canary}
    elif kind == "identity":
        rows[2]["id"] = canary
    elif kind == "fault":
        rows[2] = {"schema": "browser-ci-events/v1", "seq": 2, "event": "fault", "code": canary}
    data = _bytes(rows)
    if kind == "duplicate-key":
        data = data.replace(b'"seq": 0', ('"' + canary + '":0,"seq":0,"seq":0').encode(), 1)
    elif kind == "truncated":
        data += ('{"error":"' + canary).encode()
    (path / "execution.ndjson").write_bytes(data)
    (path / "trace.zip").write_text(canary)
    (path / "browser-e2e.txt").write_text(canary)
    destination = tmp_path / "diagnostics"
    gate.publish_diagnostics(path, destination, *registry)
    assert not (destination / "receipt.json").exists()
    assert not (destination / "execution.ndjson").exists()
    assert not (destination / "browser-e2e.txt").exists()
    assert not (destination / "trace.zip").exists()
    assert canary.encode() not in b"".join(file.read_bytes() for file in destination.iterdir())


def test_valid_incomplete_closed_stream_is_available_for_diagnosis(tmp_path, registry):
    path = _artifact(tmp_path / "raw", registry, "core")
    rows = _stream(*registry, "core")[:3]
    (path / "execution.ndjson").write_bytes(_bytes(rows))
    destination = tmp_path / "diagnostics"
    gate.publish_diagnostics(path, destination, *registry)
    assert (destination / "execution.ndjson").read_bytes() == _bytes(rows)
    with pytest.raises(gate.BrowserGateError):
        _verify(path, registry)


@pytest.mark.parametrize("failure", ["missing-terminal", "worker-crash", "interrupted"])
def test_failed_runner_publishes_only_safe_partial_metadata(tmp_path, registry, monkeypatch, failure):
    path = _artifact(tmp_path / "raw", registry, "core")
    rows = _stream(*registry, "core")[:3]
    if failure == "worker-crash":
        rows.append({"schema": "browser-ci-events/v1", "seq": 3, "event": "fault", "code": "global_error"})
    data = _bytes(rows)
    (path / "execution.ndjson").write_bytes(data)
    receipt = json.loads((path / "receipt.json").read_text())
    receipt.update(status="interrupted" if failure == "interrupted" else "failed", runner_exit_code=1,
                   execution_exit_code=1, execution_sha256=gate._hash(data), interrupted=failure == "interrupted",
                   completed_count=0)
    (path / "receipt.json").write_text(json.dumps(receipt))
    for key, value in {"PRODUCT_SHA": SOURCE, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
                       "BROWSER_QUALIFICATION": "false"}.items():
        monkeypatch.setenv(key, value)
    assert gate.main(["seal", "--artifact-dir", str(path), "--publish-dir", str(tmp_path / "complete"),
                      "--diagnostics-dir", str(tmp_path / "diagnostics")]) == 1
    assert not (tmp_path / "complete").exists()
    assert (tmp_path / "diagnostics/execution.ndjson").read_bytes() == data
    assert json.loads((tmp_path / "diagnostics/receipt.json").read_text())["status"] == receipt["status"]
    assert not (tmp_path / "diagnostics/browser-e2e.txt").exists()
    assert not (tmp_path / "diagnostics/legal").exists()


def test_serial_verifier_rejects_newer_safety_failure_with_retained_legacy_success():
    jobs = _jobs(safety=2)
    jobs["browser-reporting-safety"]["conclusion"] = "failure"
    needs = {name: {"result": "success"} for name in ("browser-reporting-safety", "browser-e2e-serial-control")}
    with pytest.raises(gate.BrowserGateError, match="newest"):
        gate.validate_jobs(needs, jobs, serial=True)


def _needs(requested=False):
    return {"browser-reporting-safety": {"result": "success"}, "browser-e2e-execution": {"result": "success"},
            "browser-e2e-serial-control": {"result": "success" if requested else "skipped"}}


def test_normal_run_allows_only_the_nonrequested_control_to_skip():
    jobs = _jobs()
    jobs["browser-e2e-serial-control"]["conclusion"] = "skipped"
    gate.validate_jobs(_needs(), jobs)
    for name in ("browser-reporting-safety", "browser-e2e-core", "browser-e2e-catchup"):
        failed = copy.deepcopy(jobs)
        failed[name]["conclusion"] = "skipped"
        with pytest.raises(gate.BrowserGateError):
            gate.validate_jobs(_needs(), failed)


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled", None])
def test_nonrequested_control_must_have_the_explicit_skip_result(conclusion):
    jobs = _jobs()
    jobs["browser-e2e-serial-control"]["conclusion"] = conclusion
    with pytest.raises(gate.BrowserGateError):
        gate.validate_jobs(_needs(), jobs)


def test_requested_control_requires_qualification_and_successful_latest_execution():
    jobs = _jobs()
    gate.validate_jobs(_needs(True), jobs, serial_requested=True, qualification=True)
    with pytest.raises(gate.BrowserGateError, match="qualification"):
        gate.validate_jobs(_needs(True), jobs, serial_requested=True)
    jobs["browser-e2e-serial-control"]["conclusion"] = "failure"
    with pytest.raises(gate.BrowserGateError, match="newest"):
        gate.validate_jobs(_needs(True), jobs, serial_requested=True, qualification=True)


def test_requested_control_is_independent_and_does_not_double_count_split_timings(tmp_path, registry):
    for lane in (*gate.LANES, "legacy"):
        _artifact(tmp_path, registry, lane, qualification=True)
    chosen, proof = gate.validate_evidence(tmp_path, jobs=_jobs(), source=SOURCE, run_id=42,
        current_attempt=1, qualification=True, serial_requested=True)
    assert set(chosen) == {"core", "catchup", "legacy"}
    assert proof["paired_control"] is True
    assert set(proof["lanes"]) == {"core", "catchup"}
    assert proof["control"]["serial_control"] is True
    assert set(proof["control"]["lanes"]) == {"legacy"}
    assert proof["control"]["source_commit"] == proof["source_commit"] == SOURCE
    assert proof["control"]["workflow_run_id"] == proof["workflow_run_id"] == 42
    assert proof["control"]["selected_ids"] == proof["selected_ids"]
    assert proof["control"]["selected_count"] == proof["selected_count"] == 15


def test_unrequested_legacy_artifact_is_not_silently_ignored(tmp_path, registry):
    for lane in (*gate.LANES, "legacy"):
        _artifact(tmp_path, registry, lane)
    with pytest.raises(gate.BrowserGateError, match="unexpected browser artifact"):
        gate.validate_evidence(tmp_path, jobs=_jobs(), source=SOURCE, run_id=42,
            current_attempt=1, qualification=False)


@pytest.mark.parametrize("failure", ["missing", "different-source", "old-attempt", "incomplete", "retry"])
def test_no_paired_proof_when_requested_legacy_execution_is_invalid(tmp_path, registry, failure):
    for lane in gate.LANES:
        _artifact(tmp_path, registry, lane, qualification=True)
    jobs = _jobs()
    if failure != "missing":
        path = _artifact(tmp_path, registry, "legacy", qualification=True,
                         retry_id="capture-replay" if failure == "retry" else None)
        if failure == "different-source":
            receipt = json.loads((path / "receipt.json").read_text())
            receipt["source_commit"] = "b" * 40
            (path / "receipt.json").write_text(json.dumps(receipt))
        elif failure == "old-attempt":
            jobs["browser-e2e-serial-control"]["run_attempt"] = 2
        elif failure == "incomplete":
            _mutate_stream(path, "execution.ndjson", lambda rows: rows[1]["tests"].pop())
    with pytest.raises(gate.BrowserGateError):
        gate.validate_evidence(tmp_path, jobs=jobs, source=SOURCE, run_id=42,
            current_attempt=2, qualification=True, serial_requested=True)


def test_required_aggregate_writes_no_legal_inputs_when_requested_control_is_missing(tmp_path, registry, monkeypatch):
    for lane in gate.LANES:
        _artifact(tmp_path / "inputs", registry, lane, qualification=True)
    monkeypatch.setattr(gate, "GitHubApi", lambda token: object())
    monkeypatch.setattr(gate, "latest_ci_jobs", lambda *args, **kwargs: _jobs())
    for key, value in {"PRODUCT_SHA": SOURCE, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1",
        "BROWSER_QUALIFICATION": "true", "BROWSER_SERIAL_CONTROL": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "CI_NEEDS_JSON": json.dumps(_needs(True))}.items():
        monkeypatch.setenv(key, value)
    assert gate.main(["--reports-dir", str(tmp_path / "inputs"),
        "--execution-root", str(tmp_path / "legal/execution"),
        "--runtime-proof", str(tmp_path / "legal/runtime.json")]) == 1
    assert not (tmp_path / "legal").exists()


def test_complete_publication_whitelists_files_only_after_success(tmp_path, registry, monkeypatch):
    path = _artifact(tmp_path / "raw", registry, "core")
    for key, value in {"PRODUCT_SHA": SOURCE, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("BROWSER_QUALIFICATION", "false")
    (path / "screenshot.png").write_bytes(b"not approved")
    destination = tmp_path / "published"
    gate.publish_complete(path, destination, registry=registry[0], digest=registry[1])
    assert not (destination / "screenshot.png").exists()
    assert (destination / "browser-e2e.txt").read_bytes() == (path / "browser-e2e.txt").read_bytes()
    bad = _artifact(tmp_path / "bad", registry, "catchup")
    _mutate_stream(bad, "execution.ndjson", lambda rows: rows[-1].update(status="failed"))
    with pytest.raises(gate.BrowserGateError):
        gate.publish_complete(bad, tmp_path / "failed-published", registry=registry[0], digest=registry[1])
    assert not (tmp_path / "failed-published").exists()


def test_legal_packager_registry_still_matches_explicit_copy_allowlist():
    import re
    source = (gate.ROOT / "tools/legal-evidence/activation-scenarios.mjs").read_text()
    paths = set(re.findall(r"'(scenario-results/[^']+\.json|evidence/[^']+\.json)'", source))
    assert {"legal/execution/" + path for path in paths} == {
        path for path in gate.LEGAL_FILES if "/scenario-results/" in path or "/evidence/" in path}


def test_registry_checksum_bytes_are_identical_across_checkout_platforms():
    import subprocess
    relative = "tools/e2e-capture/ci/registry.json"
    result = subprocess.run(["git", "check-attr", "text", "eol", "--", relative],
                            cwd=gate.ROOT, capture_output=True, text=True, check=True)
    assert f"{relative}: text: set" in result.stdout
    assert f"{relative}: eol: lf" in result.stdout
    assert b"\r\n" not in gate.REGISTRY.read_bytes()
