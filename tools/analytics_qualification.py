"""Immutable A07 records and fail-closed verification for the baseline runner."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import re
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from uuid import uuid4


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path):
    def invalid(value):
        raise ValueError("non_finite_json_number:" + value)
    def unique(pairs):
        values = dict(pairs)
        if len(values) != len(pairs):
            raise ValueError("duplicate_json_key")
        return values
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid, object_pairs_hook=unique)


def write_once(path: Path, value: object) -> None:
    """A torn write is invalid evidence, never a replaceable successful record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def stamp() -> dict:
    return {"utc": datetime.now(timezone.utc).isoformat(), "monotonic_ns": time.monotonic_ns()}


def combine(statuses) -> str:
    values = list(statuses)
    return "FAIL" if "FAIL" in values or any(x not in {"PASS", "FAIL", "BLOCKED"} for x in values) else "BLOCKED" if not values or "BLOCKED" in values else "PASS"


def runtime_context() -> dict:
    return {"python": sys.version, "executable": str(Path(sys.executable).resolve()),
            "executable_sha256": file_digest(Path(sys.executable)),
            "platform": platform.platform(), "architecture": platform.machine(),
            "dependencies": sorted((d.metadata.get("Name", "unknown"), d.version)
                                   for d in importlib.metadata.distributions()),
            "clock": {"name": "monotonic_ns", "resolution": time.get_clock_info("monotonic").resolution,
                      "adjustable": time.get_clock_info("monotonic").adjustable}}


def source_context(root: Path) -> dict:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
    revision = git("rev-parse", "HEAD")
    files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root).decode().split("\0")
    hashes = {name: file_digest(root / name) for name in files if name}
    signature = subprocess.run(["git", "verify-commit", revision], cwd=root,
                               capture_output=True, text=True, check=False)
    return {"revision": revision, "tree": git("rev-parse", "HEAD^{tree}"),
            "working_tree": git("status", "--porcelain"), "files": hashes,
            "signature_valid": signature.returncode == 0,
            "signer": git("show", "-s", "--format=%GS", revision)}


def count(value) -> bool:
    return type(value) is int and value >= 0


def finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


@contextmanager
def owner_lock(path: Path):
    """An OS lock is released on death; a leftover PID file is not authority."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def initialize(directory: Path, manifest: dict, source: dict, runtime: dict, artifacts: dict | None = None) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    context = {"schema": "analytics-evidence.v1", "run_id": uuid4().hex,
               "created": stamp(), "manifest_sha256": digest(manifest),
               "source": source, "runtime": runtime, "artifacts": artifacts or {}}
    write_once(directory / "manifest.json", manifest)
    write_once(directory / "context.json", context)
    return context


def session(directory: Path, context: dict) -> str:
    identity = uuid4().hex
    write_once(directory / "sessions" / (identity + ".json"),
               {"session_id": identity, "started": stamp(), "pid": os.getpid(),
                "context_sha256": digest(context)})
    return identity


def begin_attempt(directory: Path, context: dict, session_id: str, job: str) -> Path:
    path = directory / "attempts" / uuid4().hex
    write_once(path / "start.json", {"job": job, "session_id": session_id,
               "started": stamp(), "context_sha256": digest(context)})
    return path


def required_jobs(manifest: dict) -> dict[str, str]:
    jobs = {"source-ci": "source_ci", "regression": "semantic_safety"}
    for profile in manifest["profiles"]:
        jobs["package/" + profile] = "hardware_package"
        for size in manifest["fixture"]["sizes"]:
            jobs[f"mutation/{profile}/{size}"] = "mutation_backlog"
        for repeat in range(manifest["visibility"]["fresh_processes"]):
            jobs[f"visibility/{profile}/{repeat}"] = "single_message_visibility"
        for case in manifest["questions"]["cases"]:
            for state in manifest["questions"]["states"]:
                jobs[f"questions/{profile}/{case}/{state}"] = "approved_questions"
    return jobs


def timestamps(item: dict, *, mutation: bool) -> list[str]:
    names = ["operation_started", "activation", "first_valid_visible_result",
             "required_cleanup_complete", "backlog_drained", "operation_finished"]
    if mutation:
        names.append("durable_canonical_commit")
    marks = item.get("clocks", {})
    if any(not finite(marks.get(name)) for name in names):
        return ["missing_or_invalid_operation_clock"]
    start = marks["durable_canonical_commit"] if mutation else marks["operation_started"]
    finish = max(marks[name] for name in ("first_valid_visible_result", "required_cleanup_complete", "backlog_drained"))
    if not (marks["operation_started"] <= start <= marks["activation"] <= marks["first_valid_visible_result"]
            and marks["activation"] <= marks["required_cleanup_complete"]
            and marks["required_cleanup_complete"] <= marks["backlog_drained"]
            and finish <= marks["operation_finished"]):
        return ["operation_clock_order_invalid"]
    return []


def attach(attempt: Path, source: Path) -> dict:
    data = source.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    path = attempt / "blobs" / sha
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return {"path": "blobs/" + sha, "sha256": sha, "bytes": len(data)}


def check_matrix(manifest: dict, job: str, data: dict) -> list[str]:
    errors = []
    size = int(job.split("/")[-1])
    phases = data.get("phases", [])
    if [x.get("phase") for x in phases] != manifest["phases"]:
        return ["missing_duplicate_or_reordered_matrix_phase"]
    packaged = (manifest.get("protocol") == "analytics-closure.v2"
                and data.get("execution") == "packaged")
    count = size
    revision = phases[0].get("source_before", {}).get("revision")
    if type(revision) is not int or revision < 1:
        return ["invalid_initial_revision"]
    history = manifest["history"]
    deltas = [(0, 0), (0, 0), (1, 1), (0, 1), (-100, 100),
              (history["batches"] * (history["messages_per_batch"] + 1), history["batches"])]
    for phase, (delta, revisions) in zip(phases, deltas):
        errors.extend(timestamps(phase, mutation=revisions != 0))
        if phase.get("source_before") != {"messages": count, "revision": revision}:
            errors.append("matrix_source_before_mismatch:" + phase["phase"])
        count += delta
        if packaged:
            from tools.analytics_qualification_tracks import check_admitted_mutation
            errors.extend(check_admitted_mutation(manifest, phase))
            after_revision = phase.get("source_after", {}).get("revision")
            if type(after_revision) is not int or after_revision < revision:
                errors.append("matrix_revision_invalid:" + phase["phase"])
            revision = after_revision
        else:
            revision += revisions
        if phase.get("source_after") != {"messages": count, "revision": revision}:
            errors.append("matrix_source_after_mismatch:" + phase["phase"])
        for check in ("independent_rebuild_equal", "persisted_content_revalidated"):
            if phase.get(check) is not True:
                errors.append(check + ":" + phase["phase"])
        if any(type(value) is not int for record in (phase.get("source_before", {}), phase.get("source_after", {})) for value in record.values()):
            errors.append("invalid_matrix_count_type:" + phase["phase"])
        if revisions and phase.get("stale_reference_rejected") is not True:
            errors.append("stale_reference_not_rejected:" + phase["phase"])
    if (type(data.get("backlog")) is not int or data["backlog"] != 0
            or type(data.get("detached_workers")) is not int or data["detached_workers"] != 0
            or data.get("scheduler_closed") is not True or data.get("no_unpermitted_orphans") is not True):
        errors.append("matrix_cleanup_or_backlog_incomplete")
    return errors


def check_visibility_probe(manifest: dict, profile: str, probe: dict) -> list[str]:
    """One gate definition shared by early stopping and final verification."""
    errors = []
    clock_errors = timestamps(probe, mutation=True)
    errors.extend(clock_errors)
    if (type(probe.get("backlog_before")) is not int or probe["backlog_before"] != 0
            or type(probe.get("backlog_after")) is not int or probe["backlog_after"] != 0
            or probe.get("valid_current_result") is not True or probe.get("cleanup_complete") is not True):
        errors.append("visibility_not_valid_current_and_drained")
    if probe.get("stale_reference_rejected") is not True:
        errors.append("visibility_stale_reference_not_rejected:" + str(probe.get("case")))
    expected_digests = probe.get("expected")
    if (probe.get("independent_rebuild_equal") is not True
            or probe.get("persisted_content_revalidated") is not True
            or not isinstance(expected_digests, dict)
            or set(expected_digests) != {"projection_digest", "graph_digest", "canonical_content_digest"}
            or expected_digests != probe.get("actual")
            or any(not isinstance(v, str) or re.fullmatch(r'sha256:[0-9a-f]{64}', v) is None
                   for v in expected_digests.values())):
        errors.append("visibility_independent_verification_missing_or_invalid:" + str(probe.get("case")))
    marks = probe.get("clocks", {})
    if not clock_errors and manifest["profiles"][profile]["numeric_latency_gates"]:
        elapsed = max(marks[k] for k in ("first_valid_visible_result", "required_cleanup_complete", "backlog_drained")) - marks["durable_canonical_commit"]
        if elapsed > manifest["limits"]["visibility_seconds"]:
            errors.append("visibility_over_ten_seconds:" + probe["case"])
    return errors


def check_visibility(manifest: dict, job: str, data: dict) -> list[str]:
    _, profile, repeat = job.split("/")
    probes = data.get("probes", [])
    if data.get("initial_messages") != manifest["visibility"]["messages"]:
        return ["visibility_dataset_size_mismatch"]
    expected = manifest["visibility"]["process_cases"][int(repeat)]
    errors = []
    if [p.get("case") for p in probes] != expected:
        errors.append("visibility_case_set_mismatch")
    if data.get("complete") is False:
        errors.append("visibility_incomplete")
    stop = data.get("stopped_after_verified_failure")
    if stop is not None:
        errors.append("visibility_stopped_after_verified_failure:" + str(stop.get("case")))
    if (data.get("scheduler_closed") is not True or data.get("restart_scheduler_closed") is not True
            or type(data.get("detached_workers")) is not int or data["detached_workers"] != 0
            or type(data.get("restart_detached_workers")) is not int or data["restart_detached_workers"] != 0
            or type(data.get("backlog")) is not int or data["backlog"] != 0
            or type(data.get("restart_backlog")) is not int or data["restart_backlog"] != 0):
        errors.append("visibility_workers_or_backlog_not_closed")
    for probe in probes:
        errors.extend(check_visibility_probe(manifest, profile, probe))
    return errors


def question_statistics(calls: list[dict], warmups: int) -> dict:
    from collections import Counter
    warm = calls[warmups:]
    errors = Counter(call["error"] for call in warm if call.get("error") is not None)
    wrong = sum(call.get("correct") is not True for call in warm)
    valid = bool(warm) and not errors and not wrong and all(finite(c.get("seconds")) for c in warm)
    seconds = sorted(c["seconds"] for c in warm) if valid else []
    records = [c["records_examined"] for c in warm if count(c.get("records_examined"))]
    return {"samples": len(warm), "errors": dict(errors), "incorrect_or_failed_answers": wrong,
            "warmup_errors": dict(Counter(c["error"] for c in calls[:warmups] if c.get("error") is not None)),
            "p95_seconds": seconds[math.ceil(0.95 * len(seconds)) - 1] if valid else None,
            "maximum_seconds": max(seconds) if valid else None,
            "latency_unqualified_reason": None if valid else "failed_incorrect_or_missing_answers",
            "maximum_records_examined": max(records) if records else None,
            "truncated_calls": sum(c.get("truncated") is True for c in warm), "percentile": "nearest_rank"}


def check_questions(manifest: dict, job: str, data: dict) -> list[str]:
    _, profile, case, state = job.split("/")
    rules, calls = manifest["questions"], data.get("calls", [])
    if (data.get("initial_messages") != rules["messages"] or data.get("case") != case or data.get("state") != state
            or data.get("page_size") != rules["page_size"]
            or data.get("question") not in rules["enabled"]
            or not isinstance(data.get("filters"), dict)):
        return ["question_case_or_filters_mismatch"]
    if len(calls) != rules["warmups"] + rules["samples"]:
        return ["question_sample_count_mismatch"]
    from datetime import datetime
    from types import SimpleNamespace
    from tools.analytics_qualification_fixture import question_plan
    from tools.analytics_qualification_questions import expected_rows
    expected = expected_rows(SimpleNamespace(size=rules["messages"], account="synthetic-continuous-owner",
        clock=datetime.fromisoformat(manifest["fixture"]["evaluation_clock"])), case)
    errors = []
    if (data.get("plan") != question_plan(manifest, case) or data.get("expected") != expected
            or data.get("filters") != question_plan(manifest, case)["filters"]):
        errors.append("question_plan_or_literal_expectation_mismatch")
    if data.get("statistics") != question_statistics(calls, rules["warmups"]):
        errors.append("question_statistics_do_not_match_calls")
    identities = {c.get("process_instance") for c in calls}
    if identities != {data.get("process_instance")} or not data.get("process_instance"):
        errors.append("restarted_process_used_as_warm_evidence")
    for index, call in enumerate(calls):
        if (call.get("index") != index or call.get("phase") != ("warmup" if index < rules["warmups"] else "measured")
                or call.get("error") is not None or call.get("correct") is not True
                or not finite(call.get("seconds")) or not count(call.get("records_examined"))
                or call["records_examined"] > manifest["limits"]["request_max_records"]
                or call.get("truncated") is not False or call.get("actual") != expected[:50]
                or call.get("rows") != len(expected[:50])
                or call.get("undetermined_conversations") != (1 if case == "tied_time" else 0)
                or call.get("has_more") is not (len(expected) > 50)):
            errors.append("invalid_question_call:" + str(index))
    first = data.get("first_query", {})
    if (not finite(data.get("cold_readiness_seconds")) or not isinstance(first, dict)
            or first.get("error") is not None or first.get("correct") is not True
            or not finite(first.get("seconds")) or first.get("actual") != expected[:50]
            or first.get("process_instance") != data.get("process_instance")
            or not count(first.get("records_examined"))
            or first["records_examined"] > manifest["limits"]["request_max_records"]
            or first.get("truncated") is not False):
        errors.append("readiness_or_first_query_missing_or_failed")
    if (data.get("scheduler_closed") is not True or type(data.get("detached_workers")) is not int
            or data["detached_workers"] != 0 or type(data.get("backlog")) is not int or data["backlog"] != 0
            or data.get("pricing_disabled") is not True):
        errors.append("question_cleanup_or_pricing_gate_missing")
    if state == "idle" and (not finite(data.get("idle_seconds")) or data["idle_seconds"] < rules["idle_seconds"]):
        errors.append("idle_expiry_not_exercised")
    if state == "mutated":
        mutation = data.get("mutation", {})
        before, after = mutation.get("before", {}), mutation.get("after", {})
        if (not count(before.get("revision")) or after.get("revision") != before["revision"] + 1
                or before.get("messages") != rules["messages"] or after.get("messages") != rules["messages"]
                or mutation.get("ready") is not True):
            errors.append("question_mutation_not_verified")
    if case == "empty" and any(c.get("rows") != 0 for c in calls):
        errors.append("empty_answer_not_empty")
    if case != "empty" and any(not finite(c.get("rows")) or c["rows"] == 0 for c in calls):
        errors.append("populated_answer_missing")
    if case == "generation_bound_pagination" and (data.get("stale_cursor_rejected") is not True
            or data.get("pagination_complete") is not True or data.get("second_page") != expected[50:]):
        errors.append("stale_cursor_not_rejected")
    if not errors and manifest["profiles"][profile]["numeric_latency_gates"]:
        values = sorted(c["seconds"] for c in calls[rules["warmups"]:])
        if values[math.ceil(0.95 * len(values)) - 1] > manifest["limits"]["warm_p95_seconds"]:
            errors.append("warm_query_p95_over_one_second")
    return errors


def check_package(manifest: dict, job: str, data: dict) -> list[str]:
    profile = manifest["profiles"][job.split("/")[1]]
    errors = []
    hardware = data.get("hardware", {})
    for field in ("os", "memory_gib", "cores", "disk"):
        if hardware.get(field) != profile[field]:
            errors.append("hardware_profile_mismatch:" + field)
    for field in ("cpu_model", "power_mode", "instruction_requirements"):
        if not hardware.get(field):
            errors.append("hardware_measurement_missing:" + field)
    for field in manifest["package_measurements"]:
        if not finite(data.get(field)):
            errors.append("package_measurement_missing:" + field)
    for field in manifest["package_behaviors"]:
        if data.get(field) is not True:
            errors.append("package_behavior_missing:" + field)
    if data.get("ingestion_path") != "authorized_agent" or data.get("memory_scope") != "process_tree":
        errors.append("package_ingestion_or_memory_scope_invalid")
    if not data.get("dependencies") or data.get("artifact_hashes_verified") is not True:
        errors.append("package_inputs_not_verified")
    return errors


def check_payload(manifest: dict, context: dict, job: str, data: dict) -> list[str]:
    if data.get("profiling") is True:
        return ["instrumented_run_is_diagnostic_only"]
    if data.get("continue_after_visibility_failure") is True:
        return ["continue_after_failure_is_diagnostic_only"]
    if job == "source-ci":
        jobs = data.get("checks", [])
        expected = set(manifest["ci_jobs"])
        if len(jobs) != len(expected) or {x.get("name") for x in jobs} != expected:
            return ["missing_or_duplicate_ci_job"]
        if any(x.get("head_sha") != context["source"]["revision"] or x.get("status") != "completed"
               or x.get("conclusion") != "success" for x in jobs):
            return ["ci_not_successful_on_exact_source"]
        if data.get("reviewed_source_sha") != context["source"]["revision"]:
            return ["source_review_missing"]
        return []
    if job == "regression":
        from tools.qualify_analytics_baseline import RUNS, baseline_passed
        runs = data.get("runs", [])
        if [x.get("name") for x in runs] != [x[0] for x in RUNS]:
            return ["regression_selection_incomplete"]
        if not baseline_passed(runs, len(RUNS)):
            return ["regression_failed"]
        if any(x.get("tests") != spec[2] or x.get("profile") != spec[1]
               for x, spec in zip(runs, RUNS)):
            return ["regression_selection_mismatch"]
        return []
    if job.startswith("mutation/"):
        return check_matrix(manifest, job, data)
    if job.startswith("visibility/"):
        return check_visibility(manifest, job, data)
    if job.startswith("questions/"):
        return check_questions(manifest, job, data)
    if job.startswith("package/"):
        return check_package(manifest, job, data)
    return ["unknown_job"]


def verdict(status: str, *reasons: str) -> dict:
    return {"status": status, "reasons": list(reasons)}


def inspect_attempt(path: Path, manifest: dict, context: dict) -> tuple[str, dict, dict]:
    start = read_json(path / "start.json")
    job = start["job"]
    if start.get("context_sha256") != digest(context):
        return job, verdict("FAIL", "attempt_context_mismatch"), {}
    session_record = read_json(path.parent.parent / "sessions" / (start["session_id"] + ".json"))
    if session_record.get("context_sha256") != digest(context):
        return job, verdict("FAIL", "session_context_mismatch"), {}
    if not (path / "result.json").exists():
        return job, verdict("FAIL", "started_worker_has_no_durable_result"), {}
    result = read_json(path / "result.json")
    if result.get("context_sha256") != digest(context) or result.get("job") != job:
        return job, verdict("FAIL", "result_context_mismatch"), result
    if result.get("status") == "BLOCKED" and result.get("reason"):
        if result.get("worker_started") is not False or result.get("exit_code") is not None:
            return job, verdict("FAIL", "executed_worker_cannot_be_relabelled_blocked"), result
        return job, verdict("BLOCKED", result["reason"]), result
    if (result.get("status") != "PASS" or result.get("exit_code") != 0 or result.get("timed_out") is not False
            or result.get("worker_started") is not True or result.get("worker_joined") is not True
            or result.get("complete") is not True):
        reason = result.get("reason") or "worker_failed_killed_or_incomplete"
        return job, verdict("FAIL", str(reason), *result.get("diagnostic_errors", [])), result
    worker_limit = (manifest["visibility_execution"]["maximum_worker_seconds"]
                    if job.startswith("visibility/") and "visibility_execution" in manifest
                    else manifest["limits"]["whole_worker_seconds"])
    if (not finite(result.get("seconds")) or result["seconds"] > worker_limit
            or result.get("maximum_seconds") != worker_limit):
        return job, verdict("FAIL", "worker_guard_not_satisfied"), result
    if result.get("source_after_sha256") != digest(context["source"]):
        return job, verdict("FAIL", "source_changed_during_worker"), result
    payload = result["payload"]
    if result.get("payload_sha256") != digest(payload) or not result.get("process_instance"):
        return job, verdict("FAIL", "payload_or_process_identity_invalid"), result
    for reference in result.get("attachments", []):
        relative = reference["path"]
        if relative != "blobs/" + reference["sha256"]:
            return job, verdict("FAIL", "invalid_evidence_path"), result
        file = path / relative
        if (file.resolve().parent != (path / "blobs").resolve()
                or file_digest(file) != reference["sha256"] or file.stat().st_size != reference["bytes"]):
            return job, verdict("FAIL", "evidence_hash_mismatch"), result
    errors = check_payload(manifest, context, job, payload)
    from tools.analytics_qualification_hardware_evidence import check_evidence as check_hardware_evidence
    errors.extend(check_hardware_evidence(path, result, manifest, context))
    if job == "source-ci":
        errors.extend(check_ci_evidence(path, result, context, manifest))
    elif job == "regression":
        errors.extend(check_regression_evidence(path, result, context))
    elif job != "source-ci":
        if manifest.get("protocol") == "analytics-closure.v2" and payload.get("execution") == "packaged":
            from tools.analytics_qualification_packaged import check_evidence
            errors.extend(check_evidence(path, result, manifest, context))
        else:
            errors.extend(check_collector_evidence(path, result, manifest))
    if errors:
        return job, verdict("FAIL", *errors), result
    from tools.analytics_qualification_tracks import check_track
    track_errors = check_track(manifest, context, job, payload, result.get("subject"))
    if track_errors is not None:
        if track_errors:
            status = "BLOCKED" if track_errors == ["exact_package_execution_not_established"] else "FAIL"
            return job, verdict(status, *track_errors), result
        return job, verdict("PASS"), result
    if job not in ("source-ci", "regression"):
        artifacts = {k: v["sha256"] for k, v in context.get("artifacts", {}).items()}
        if (set(artifacts) != {"installer", "runtime"} or payload.get("execution") != "packaged"
                or payload.get("artifact_hashes") != artifacts
                or result.get("subject") != context["source"]
                or payload.get("profile") != job.split("/")[1]
                or payload.get("ingestion_path") != "authorized_agent"):
            return job, verdict("BLOCKED", "exact_package_execution_not_established"), result
    return job, verdict("PASS"), result


def verify(directory: Path, frozen_manifest: dict, *, current_source: dict | None = None) -> dict:
    """Recompute every gate; no stored overall verdict is authoritative."""
    jobs = required_jobs(frozen_manifest)
    checks = {name: [] for name in jobs}
    evidence_errors, results = [], []
    try:
        context = read_json(directory / "context.json")
        manifest = read_json(directory / "manifest.json")
        if digest(manifest) != digest(frozen_manifest) or context.get("manifest_sha256") != digest(manifest):
            raise ValueError("frozen_manifest_mismatch")
        if current_source is not None and digest(current_source) != digest(context["source"]):
            evidence_errors.append("source_does_not_match_run")
        for name, item in context.get("artifacts", {}).items():
            artifact = Path(item["path"])
            if not artifact.is_file() or file_digest(artifact) != item["sha256"]:
                evidence_errors.append("artifact_missing_or_changed:" + name)
        for attempt in sorted((directory / "attempts").glob("*")):
            try:
                job, result, raw = inspect_attempt(attempt, manifest, context)
                if job not in checks:
                    raise ValueError("unknown_attempt_job:" + job)
                checks[job].append(result)
                results.append((job, raw))
            except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
                evidence_errors.append("invalid_attempt:" + attempt.name + ":" + type(error).__name__)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        context = {}
        evidence_errors.append("invalid_run:" + str(error))
    source = context.get("source", {})
    if (source.get("signature_valid") is not True or source.get("working_tree") != ""
            or frozen_manifest["source"]["signer_email"] not in source.get("signer", "")):
        checks["source-ci"].append(verdict("FAIL", "source_not_clean_and_validly_signed"))
    for profile in frozen_manifest["profiles"]:
        instances = [raw.get("process_instance") for job, raw in results
                     if job.startswith("visibility/" + profile + "/") and raw.get("status") == "PASS"]
        if len(instances) != len(set(instances)) or (instances and None in instances):
            evidence_errors.append("visibility_repetitions_not_fresh_processes:" + profile)
    job_verdicts = {}
    for name, values in checks.items():
        job_verdicts[name] = {"status": combine(x["status"] for x in values),
            "reasons": [reason for x in values for reason in x["reasons"]] if values else ["mandatory_evidence_missing"],
            "attempts": len(values)}
    gates = {gate: verdict(combine(result["status"] for name, result in job_verdicts.items()
                                  if jobs[name] == gate)) for gate in frozen_manifest["gates"]}
    gates["evidence_validity"] = verdict("FAIL", *evidence_errors) if evidence_errors else verdict(
        "PASS" if all(checks.values()) else "BLOCKED", *([] if all(checks.values()) else ["mandatory_evidence_missing"]))
    if frozen_manifest.get("status") != "frozen" and gates["evidence_validity"]["status"] != "FAIL":
        gates["evidence_validity"] = verdict("BLOCKED", "acceptance_manifest_not_frozen")
    return {"schema": "analytics-verdict.v1", "source_revision": source.get("revision"),
            "manifest_sha256": digest(frozen_manifest), "status": combine(x["status"] for x in gates.values()),
            "gates": gates, "jobs": job_verdicts}


def check_regression_evidence(attempt: Path, result: dict, context: dict) -> list[str]:
    """Re-read durable phase receipts and JUnit, not only summary counts."""
    from tools.qualify_analytics_baseline import counts

    references = result.get("attachments", [])
    names = {item.get("name"): item for item in references}
    if len(names) != len(references) or "report.json" not in names:
        return ["missing_or_duplicate_regression_evidence"]
    payload = result["payload"]
    raw = read_json(attempt / names["report.json"]["path"])
    if (digest(raw) != digest(payload) or raw.get("source_revision") != context["source"]["revision"]
            or raw.get("working_tree") != context["source"]["working_tree"]):
        return ["regression_report_source_mismatch"]
    errors = []
    for run in payload.get("runs", []):
        receipt, junit = run["name"] + ".result.json", run["name"] + ".xml"
        if receipt not in names or junit not in names:
            errors.append("regression_phase_evidence_missing:" + run["name"])
            continue
        if digest(read_json(attempt / names[receipt]["path"])) != digest(run):
            errors.append("regression_phase_receipt_mismatch:" + run["name"])
        if counts(attempt / names[junit]["path"]) != run.get("counts"):
            errors.append("regression_junit_mismatch:" + run["name"])
    return errors


def check_collector_evidence(attempt: Path, result: dict, manifest: dict) -> list[str]:
    """Bind raw phase and process records to the source collector's claimed result."""
    references = result.get("attachments", [])
    names = {item.get("name"): item for item in references}
    if len(names) != len(references) or not {"worker-input.json", "payload.json", "process.json", "fixture.json"} <= names.keys():
        return ["collector_raw_inputs_or_results_missing"]
    def read(name):
        return read_json(attempt / names[name]["path"])
    config, payload = read("worker-input.json"), result["payload"]
    if manifest.get("protocol") == "analytics-closure.v2" and payload.get("evidence_track") == "semantic_questions":
        if (config.get("mode") != "questions" or config.get("known_kinds") is not True
                or config.get("semantic_questions") is not True
                or config.get("profile") != payload.get("profile")
                or config.get("hardware") != payload.get("hardware")):
            return ["semantic_question_collector_binding_mismatch"]
    profiled = config.get("profile_updates", False)
    claimed = payload.get("profiling", False)
    if type(profiled) is not bool or type(claimed) is not bool or profiled != claimed:
        return ["collector_profiling_flag_mismatch"]
    if profiled:
        return ["instrumented_run_is_diagnostic_only"]
    continuation = config.get("continue_after_visibility_failure", False)
    if (type(continuation) is not bool
            or continuation != payload.get("continue_after_visibility_failure", False)):
        return ["collector_continuation_flag_mismatch"]
    if continuation:
        return ["continue_after_failure_is_diagnostic_only"]
    if digest(read("payload.json")) != digest(payload):
        return ["collector_payload_differs_from_raw_record"]
    if (digest(config["manifest"]) != digest(manifest) or config["subject_sha256"] != digest(result["subject"])
            or result.get("subject_after_sha256") != digest(result["subject"])
            or payload.get("subject_sha256") != config["subject_sha256"]
            or payload.get("subject_unchanged") is not True
            or payload.get("supervisor_instance") != result.get("process_instance")):
        return ["collector_source_manifest_or_owner_mismatch"]
    processes = [read(name) for name in names if name == "process.json" or name.endswith("/process.json")]
    instances = {item["instance"] for item in processes}
    if len(instances) != len(processes) or any(
            str(item["pid"]) != item["instance"].split(":")[0]
            or item.get("supervisor_instance") != result["process_instance"] for item in processes):
        return ["collector_process_identity_invalid"]
    if payload.get("complete") is not True or read("fixture.json").get("manifest_sha256") != digest(manifest):
        return ["collector_incomplete_or_fixture_manifest_mismatch"]
    events = [read(name) for name in names if "/events/" in "/" + name]
    completed_phases = [read(name) for name in names if name.endswith("-phase.json")]
    phase_values = [event["value"] for name in names if name.endswith("-phase.json") for event in [read(name)]]
    from tools.analytics_qualification_execution import check_evidence
    errors = check_evidence(manifest, config, result, names, read, events)
    for phase in payload.get("phases", []):
        if sum(digest(value) == digest(phase) for value in phase_values) != 1:
            errors.append("durable_phase_record_missing_or_duplicate")
        expected, actual = phase.get("expected"), phase.get("actual")
        if (not isinstance(expected, dict) or set(expected) != {"projection_digest", "graph_digest", "canonical_content_digest"}
                or expected != actual or any(not isinstance(v, str) or not v.startswith("sha256:") or len(v) != 71 for v in expected.values())):
            errors.append("independent_phase_digests_missing_or_different")
    for probe in payload.get("probes", []):
        if probe.get("process_instance") not in instances:
            errors.append("visibility_runtime_process_not_observed")
        matching = [event for event in completed_phases if event["value"].get("case") == probe.get("case")
                    and digest(dict(event["value"], process_instance=event["process_instance"])) == digest(probe)]
        if len(matching) != 1:
            errors.append("durable_visibility_phase_missing_or_duplicate")
    if "probes" in payload:
        observed = payload.get("runtime_processes", [])
        if len(observed) != 2 or set(observed) != instances:
            errors.append("visibility_restart_process_set_invalid")
        elif any(p.get("process_instance") != observed[1 if p.get("case", "").startswith("restarted/") else 0] for p in payload["probes"]):
            errors.append("visibility_restart_case_used_wrong_process")
        restart_references = [read(name) for name in names if name.endswith("-restart-reference.json")]
        restarted = [probe for probe in payload["probes"] if probe.get("case", "").startswith("restarted/")]
        if (len(restart_references) != 1 or len(restarted) != 1
                or restart_references[0]["process_instance"] != restarted[0].get("process_instance")
                or restart_references[0]["value"].get("checked_current") is not True
                or restart_references[0]["value"].get("source_revision") != restarted[0].get("source_before", {}).get("revision")):
            errors.append("restart_current_reference_not_observed")
    if "calls" in payload:
        if payload.get("process_instance") not in instances or payload["process_instance"] == read("process.json")["instance"]:
            errors.append("question_runtime_not_a_fresh_observed_process")
        raw_calls = [read(name)["value"] for name in sorted(names) if name.endswith("-question.json")]
        if digest(raw_calls) != digest(payload["calls"]):
            errors.append("durable_question_calls_mismatch")
        first = [read(name)["value"] for name in names if name.endswith("-first-query.json")]
        if len(first) != 1 or first[0] != payload.get("first_query"):
            errors.append("durable_first_query_missing_or_changed")
        if payload.get("verification", {}).get("independent_rebuild_equal") is not True:
            errors.append("question_independent_verification_missing")
    return errors


def check_ci_evidence(attempt: Path, result: dict, context: dict, manifest: dict) -> list[str]:
    from tools.analytics_qualification_ci import select_checks
    names = {item.get("name"): item for item in result.get("attachments", [])}
    if "ci.json" not in names:
        return ["raw_ci_evidence_missing"]
    raw = read_json(attempt / names["ci.json"]["path"])
    source = context["source"]
    if (digest(raw) != digest(result["payload"]) or raw.get("requested_sha") != source["revision"]
            or raw.get("source_unchanged") is not True
            or raw.get("review", {}).get("source_sha256") != digest(source)):
        return ["ci_source_or_review_binding_invalid"]
    if select_checks(raw["api_responses"], manifest["ci_jobs"], source["revision"]) != raw["checks"]:
        return ["ci_summary_differs_from_raw_response"]
    return []
