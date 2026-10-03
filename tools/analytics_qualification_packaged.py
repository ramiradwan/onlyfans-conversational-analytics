"""Collect and verify qualification against immutable packaged inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import zipfile

from tools import analytics_qualification as q
from tools.analytics_qualification_tracks import check_profile, check_unknown_question


def check_cancellation(cancellation, streams):
    events = {event["sequence"]: event for event in streams.get(cancellation.get("process_instance"), [])}
    started = events.get(cancellation.get("started_sequence"), {})
    cancelled = events.get(cancellation.get("cancelled_sequence"), {})
    identity = cancellation.get("attempt_id")
    if (not isinstance(identity, str) or not identity
            or started.get("event_type") != "build_started" or started.get("full_rebuild") is not True
            or cancelled.get("event_type") != "build_cancelled"
            or started.get("attempt_id") != identity or cancelled.get("attempt_id") != identity
            or started.get("account_ref") != cancelled.get("account_ref")
            or started.get("canonical_revision") != cancelled.get("canonical_revision")
            or not cancellation.get("request_ns", -1) <= started.get("monotonic_ns", -2)
            <= cancelled.get("monotonic_ns", -3)):
        return ["packaged_analytics_cancellation_not_observed"]
    return []


def artifact_context(inputs):
    if inputs.get("schema") != "analytics-package-inputs.v1":
        raise ValueError("package_inputs_schema_invalid")
    result = {}
    for name in ("installer", "runtime"):
        item = inputs["artifacts"][name]
        path = Path(item["path"]).resolve(strict=True)
        actual = q.file_digest(path)
        if actual != item["sha256"]:
            raise ValueError("package_artifact_hash_mismatch:" + name)
        result[name] = {"path": str(path), "sha256": actual}
    return result


def extracted_files(archive, directory):
    """Verify every extracted byte against the immutable runtime archive."""
    directory = Path(directory).resolve(strict=True)
    expected = {}
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            if member.is_dir():
                continue
            path = PurePosixPath(member.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in member.filename
                    or not path.parts or ":" in path.parts[0] or str(path) in expected):
                raise ValueError("runtime_archive_path_invalid")
            target = directory.joinpath(*path.parts)
            if not target.is_file() or not target.resolve(strict=True).is_relative_to(directory) or target.is_symlink():
                raise ValueError("runtime_extraction_path_invalid")
            digest = hashlib.sha256()
            with package.open(member) as stream:
                while data := stream.read(1024 * 1024):
                    digest.update(data)
            expected[str(path)] = digest.hexdigest()
            if q.file_digest(target) != expected[str(path)]:
                raise ValueError("runtime_extraction_hash_mismatch")
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    if actual != set(expected) or "Brain.exe" not in expected:
        raise ValueError("runtime_extraction_file_set_mismatch")
    return expected


def runtime_source(archive, revision):
    """Bind the frozen release manifest to the reviewed source revision."""
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("package_release_manifest_invalid")
        return value

    with zipfile.ZipFile(archive) as package:
        manifests = [item for item in package.infolist() if item.filename == "release-manifest.json"]
        if len(manifests) != 1 or not 0 < manifests[0].file_size <= 65536:
            raise ValueError("package_release_manifest_missing_or_invalid")
        raw = package.read(manifests[0])
    value = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(value, dict) or value.get("schema") != "ofca-release-manifest/v1"
            or value.get("architecture") != "x64"):
        raise ValueError("package_release_manifest_invalid")
    if value.get("source_commit") != revision:
        raise ValueError("package_release_source_revision_mismatch")
    return {"source_revision": revision, "manifest_sha256": hashlib.sha256(raw).hexdigest()}


def prerequisites(inputs, manifest, source, profile):
    from tools.analytics_qualification_hardware import observe, observe_network_isolation
    artifacts = artifact_context(inputs)
    for field in ("runtime_directory", "agent_directory", "data_directory", "browser_profile"):
        if not Path(inputs[field]).is_absolute():
            raise ValueError("package_path_must_be_absolute:" + field)
    if inputs.get("source_revision") != source["revision"]:
        raise ValueError("package_source_revision_mismatch")
    if source.get("working_tree") or source.get("signature_valid") is not True:
        raise ValueError("package_source_not_clean_and_signed")
    release_source = runtime_source(artifacts["runtime"]["path"], source["revision"])
    hardware = observe()
    errors = check_profile(manifest, profile, hardware)
    if errors:
        raise ValueError(";".join(errors))
    files = extracted_files(artifacts["runtime"]["path"], inputs["runtime_directory"])
    root = Path(inputs["runtime_directory"]).resolve()
    agent = Path(inputs["agent_directory"]).resolve(strict=True)
    if not agent.is_relative_to(root) or not (agent / "manifest.json").is_file():
        raise ValueError("agent_not_bound_to_runtime_archive")
    data, browser = (Path(inputs[key]).resolve(strict=True) for key in ("data_directory", "browser_profile"))
    if (data.is_relative_to(root) or browser.is_relative_to(root)
            or data.is_relative_to(browser) or browser.is_relative_to(data)):
        raise ValueError("package_runtime_and_profile_directories_must_be_separate")
    for field in ("data_directory", "browser_profile"):
        if not Path(inputs[field]).is_dir():
            raise ValueError("provisioned_package_prerequisite_missing:" + field)
    if not (Path(inputs["data_directory"]) / "runtime.env").is_file():
        raise ValueError("provisioned_runtime_configuration_missing")
    if inputs.get("synthetic_account_id") != "synthetic-continuous-owner":
        raise ValueError("dedicated_synthetic_account_required")
    if shutil.which("node") is None:
        raise ValueError("package_browser_runtime_unavailable")
    harness = Path(__file__).resolve().parent / "e2e-capture"
    resolved_browser = subprocess.check_output(["node", "--input-type=module", "-e",
        "import { chromium } from '@playwright/test'; process.stdout.write(chromium.executablePath());"],
        cwd=harness, text=True, timeout=30)
    if not Path(inputs.get("browser_executable") or resolved_browser).is_file():
        raise ValueError("package_browser_executable_unavailable")
    allowed = {"schema", "artifacts", "source_revision", "runtime_directory", "agent_directory",
        "data_directory", "browser_profile", "synthetic_account_id", "browser_executable",
        "platform_origin", "bridge_origin", "bridge_path", "identity_path", "conversations_path",
        "messages_path_prefix"}
    if not inputs.keys() <= allowed:
        raise ValueError("package_inputs_contain_unsupported_fields")
    return {"hardware": hardware, "artifacts": artifacts, "runtime_files": files,
            "runtime_source": release_source,
            "network_isolation": observe_network_isolation()}


def run(root, directory, context, session, manifest, args):
    from tools.analytics_qualification_process import supervise
    from tools.analytics_qualification_runner import finish
    kind, profile = args.run_package, args.profile
    job = (f"package/{profile}" if kind == "package" else
           f"mutation/{profile}/{args.messages}" if kind == "matrix" else
           f"visibility/{profile}/{args.repeat}")
    attempt = q.begin_attempt(directory, context, session, job)
    try:
        inputs = q.read_json(args.package_inputs)
        observed = prerequisites(inputs, manifest, context["source"], profile)
        if observed["artifacts"] != context.get("artifacts"):
            raise ValueError("package_inputs_differ_from_campaign")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        finish(attempt, context, job, {"status": "BLOCKED", "worker_started": False,
            "exit_code": None, "reason": str(error)})
        return
    config = {"root": str(root), "manifest": manifest, "subject": context["source"],
        "subject_sha256": q.digest(context["source"]), "job": job, "mode": kind,
        "profile": profile, "messages": args.messages, "repeat": args.repeat,
        "inputs": inputs, "observed": observed, "output": str(attempt / "collector")}
    schedule = None
    limit = manifest["limits"]["whole_worker_seconds"]
    if kind == "visibility":
        schedule = dict(manifest["visibility_execution"], directory=str(attempt / "collector/execution"))
        config["execution_schedule"] = schedule
        limit = schedule["maximum_worker_seconds"]
    q.write_once(attempt / "worker-input.json", config)
    result = {}
    try:
        result = supervise([sys.executable, "-m", "tools.analytics_qualification_packaged",
            "--worker", str(attempt / "worker-input.json")], root, attempt, limit,
            limits=manifest["limits"], execution_schedule=schedule)
        if result["worker_started"]:
            file = attempt / "collector/payload.json"
            payload = q.read_json(file) if file.exists() else {"complete": False}
            result.update(payload=payload, payload_sha256=q.digest(payload),
                complete=payload.get("complete") is True, subject=context["source"],
                source_after_sha256=q.digest(q.source_context(root)),
                subject_after_sha256=q.digest(q.source_context(root)), attachments=[])
            for file in sorted((attempt / "collector").rglob("*")):
                relative = file.relative_to(attempt / "collector")
                if (file.is_file() and file.suffix in {".json", ".ndjson"}
                        and "temporary" not in relative.parts):
                    result["attachments"].append(dict(q.attach(attempt, file),
                        name=file.relative_to(attempt / "collector").as_posix()))
            result["attachments"].append(dict(q.attach(attempt, attempt / "worker-input.json"), name="worker-input.json"))
            result["diagnostic_errors"] = q.check_payload(manifest, context, job, payload)
            if not result["complete"] or result["diagnostic_errors"]:
                result["status"] = "FAIL"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        result.update(status="FAIL", complete=False, reason=type(error).__name__)
    finish(attempt, context, job, result)


def check_evidence(attempt, result, manifest, context):
    from tools.analytics_qualification_package_process import ReceiptStream, receipt_account
    from tools.analytics_qualification_tracks import check_admitted_mutation
    references = result.get("attachments", [])
    names = {item.get("name"): item for item in references}
    required = {"worker-input.json", "payload.json", "hardware.json", "artifact-check.json", "process.json",
                "network-isolation-before.json", "network-isolation-after.json"}
    if len(names) != len(references) or not required <= names.keys():
        return ["packaged_raw_evidence_missing"]
    def read(name):
        return q.read_json(attempt / names[name]["path"])
    payload, config = result["payload"], read("worker-input.json")
    if (read("payload.json") != payload or config["manifest"] != manifest
            or config["subject"] != context["source"]
            or payload.get("subject_sha256") != q.digest(context["source"])
            or payload.get("supervisor_instance") != result.get("process_instance")
            or read("hardware.json") != payload.get("hardware")):
        return ["packaged_raw_binding_mismatch"]
    try:
        release_source = runtime_source(context["artifacts"]["runtime"]["path"], context["source"]["revision"])
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return ["packaged_source_binding_invalid"]
    if config.get("observed", {}).get("runtime_source") != release_source:
        return ["packaged_source_binding_mismatch"]
    artifacts = {key: value["sha256"] for key, value in context["artifacts"].items()}
    for name in ("network-isolation-before.json", "network-isolation-after.json"):
        if read(name) != {"active_adapters": 0, "default_routes": 0}:
            return ["packaged_offline_boundary_not_established"]
    check = read("artifact-check.json")
    if check.get("before") != artifacts or check.get("after") != artifacts:
        return ["package_artifacts_changed"]
    streams, processes = {}, {}
    for name in sorted(names):
        if not name.endswith("/lifecycle.ndjson"):
            continue
        process_name = name.rsplit("/", 1)[0] + "/process.json"
        if process_name not in names:
            return ["packaged_process_identity_missing"]
        process = read(process_name)
        if (process.get("supervisor_instance") != result.get("process_instance")
                or process.get("executable_sha256") != config["observed"]["runtime_files"].get("Brain.exe")
                or process.get("instance") != f"{process.get('pid')}:{process.get('run_id')}"):
            return ["packaged_process_source_or_owner_mismatch"]
        stream = ReceiptStream(Path(), process["run_id"], process["pid"], process["started_ns"])
        try:
            with (attempt / names[name]["path"]).open("rb") as raw:
                for line in raw:
                    stream.append(line, result["payload"]["ended_ns"])
            stream.check_complete()
        except (ValueError, OSError, TypeError, KeyError):
            return ["packaged_lifecycle_invalid"]
        if process["instance"] in streams:
            return ["packaged_process_identity_reused"]
        streams[process["instance"]] = stream.records
        processes[process["instance"]] = process
    if not streams:
        return ["packaged_lifecycle_missing"]
    errors = []
    phase_records = [read(name) for name in names if name.endswith("-phase.json")]
    observations = [read(name) for name in names if name.startswith("observations/current-")]
    from tools.analytics_qualification_package_fixture import Fixture
    from tools.analytics_qualification_package_oracle import message_digest
    job = result["job"]
    size = (int(job.rsplit("/", 1)[-1]) if job.startswith("mutation/") else
            manifest["fixture"]["sizes"][-1] if job.startswith("package/") else manifest["visibility"]["messages"])
    fixture = Fixture(size, manifest["fixture"]["evaluation_clock"])
    mutation_index = 0
    for phase in payload.get("phases", []) + payload.get("probes", []):
        identity = phase.get("process_instance")
        events = streams.get(identity, [])
        by_sequence = {event["sequence"]: event for event in events}
        binding = phase.get("lifecycle_sequences", {})
        account_ref = receipt_account(processes.get(identity, {}).get("run_id", ""),
                                      config["inputs"]["synthetic_account_id"])
        if sum(value == phase for value in phase_records) != 1:
            errors.append("packaged_durable_phase_missing_or_duplicate")
        errors.extend(check_admitted_mutation(manifest, phase))
        for event_type, field in (("activation_commit", "activation"),
                                  ("cleanup_complete", "required_cleanup_complete"),
                                  ("scheduler_drained", "backlog_drained")):
            event = by_sequence.get(binding.get(event_type), {})
            if (event.get("event_type") != event_type
                    or event.get("account_ref") != account_ref
                    or event.get("generation_id") != phase.get("generation_id")
                    or event.get("canonical_revision") != phase.get("source_after", {}).get("revision")
                    or event.get("monotonic_ns", -1) / 1_000_000_000 != phase.get("clocks", {}).get(field)):
                errors.append("packaged_operation_lifecycle_mismatch:" + event_type)
            if event_type == "scheduler_drained" and any(event.get(key) != 0 for key in
                    ("pending_accounts", "recovery_requests", "active_publications")):
                errors.append("packaged_backlog_not_drained")
        for commit in phase.get("admitted_commits", []):
            event = by_sequence.get(commit.get("sequence"), {})
            if (event.get("event_type") != "canonical_commit"
                    or event.get("account_ref") != account_ref
                    or any(event.get(field) != commit.get(field) for field in
                           ("canonical_revision", "origin", "monotonic_ns"))
                    or event.get("admitted_events") != commit.get("operation_count")):
                errors.append("packaged_admitted_commit_mismatch")
        observation = phase.get("observation", {})
        if sum(value == observation for value in observations) != 1:
            errors.append("packaged_durable_observation_missing_or_duplicate")
        snapshot = observation.get("result", {}).get("question", {}).get("snapshot", {})
        if (not check_unknown_question(observation.get("result", {}))
                or snapshot.get("generation_id") != phase.get("generation_id")
                or snapshot.get("source_revision") != phase.get("source_after", {}).get("revision")
                or snapshot.get("source_message_count") != phase.get("source_after", {}).get("messages")
                or observation.get("ui", {}).get("visible") is not True
                or observation.get("ui", {}).get("uncertainty_visible") is not True
                or observation.get("ui", {}).get("uncertainty_guidance_present") is not True
                or observation.get("ui", {}).get("source_links") != 0
                or observation.get("ui", {}).get("positive_rows") != 0
                or observation.get("ui", {}).get("body") != observation.get("result")
                or observation.get("received_ns", -1) / 1_000_000_000
                != phase.get("clocks", {}).get("first_valid_visible_result")):
            errors.append("packaged_visible_question_invalid")
        verification_name = phase.get("verification_record")
        name = phase.get("phase")
        if name == "one_committed_message":
            if job.startswith("visibility/"):
                fixture.append(f"visibility-{mutation_index}", 0 if phase["case"].endswith("/dominant") else 1)
                mutation_index += 1
            else:
                fixture.append("package-resumed-current" if job.startswith("package/") else "matrix-current", 1)
        elif name == "100_edits":
            fixture.edit()
        elif name == "100_creator_deletions":
            fixture.delete()
        elif name == "10000_historical_interleaved_with_100_live":
            for batch in range(manifest["history"]["batches"]):
                fixture.history_batch(batch)
        expected_count, expected_hash = message_digest(fixture.rows())
        if verification_name not in names:
            errors.append("packaged_independent_verification_missing")
        else:
            verification = read(verification_name)
            predecessors = [value for value in observations
                if value.get("received_ns", 0) / 1_000_000_000 <= phase["clocks"]["operation_started"]
                and value.get("result", {}).get("question", {}).get("snapshot", {}).get("source_revision")
                == phase["source_before"]["revision"]]
            predecessor = max(predecessors, key=lambda value: value["received_ns"]) if predecessors else {}
            previous_generation = predecessor.get("result", {}).get("question", {}).get("snapshot", {}).get("generation_id")
            if (verification.get("generation_id") != phase.get("generation_id")
                    or previous_generation is None
                    or verification.get("previous_generation_id") != previous_generation
                    or verification.get("source_revision") != phase.get("source_after", {}).get("revision")
                    or verification.get("expected") != phase.get("expected")
                    or verification.get("actual") != phase.get("actual")
                    or verification.get("expected") != verification.get("actual")
                    or verification.get("canonical_unchanged") is not True
                    or verification.get("fixture_content_equal") is not True
                    or verification.get("expected_message_count") != expected_count
                    or verification.get("expected_messages_sha256") != expected_hash
                    or verification.get("actual_messages_sha256") != expected_hash
                    or verification.get("stale_reference_rejected") != phase.get("stale_reference_rejected")
                    or verification.get("no_unpermitted_orphans") != phase.get("no_unpermitted_orphans")
                    or verification.get("independent_rebuild_equal") is not True
                    or verification.get("persisted_content_revalidated") is not True):
                errors.append("packaged_independent_verification_invalid")
            if any(snapshot.get(key) != verification.get("actual", {}).get(key)
                   for key in ("projection_digest", "canonical_content_digest")):
                errors.append("packaged_result_differs_from_persisted_verifier")
            activation = by_sequence.get(binding.get("activation_commit"), {})
            if any(activation.get(key) != verification.get("actual", {}).get(key)
                   for key in ("projection_digest", "graph_digest", "canonical_content_digest")):
                errors.append("packaged_committed_publication_differs_from_verifier")
    if "probes" in payload:
        from tools.analytics_qualification_execution import check_evidence as check_execution
        events = [{"value": phase, "observed": {"monotonic_ns": phase["verified_at_ns"]}}
                  for phase in phase_records]
        errors.extend(check_execution(manifest, config, result, names, read, events))
        cold_name = payload.get("cold_verification_record")
        cold = read(cold_name) if cold_name in names else {}
        cold_count, cold_hash = message_digest(Fixture(size, manifest["fixture"]["evaluation_clock"]).rows())
        if (cold.get("independent_rebuild_equal") is not True or cold.get("canonical_unchanged") is not True
                or cold.get("fixture_content_equal") is not True
                or cold.get("expected_message_count") != cold_count
                or cold.get("expected_messages_sha256") != cold_hash
                or cold.get("actual_messages_sha256") != cold_hash
                or cold.get("persisted_content_revalidated") is not True
                or cold.get("source_revision") != payload["probes"][0]["source_before"]["revision"]):
            errors.append("packaged_cold_verification_missing_or_invalid")
        observed = payload.get("runtime_processes", [])
        if (len(observed) != 2 or set(observed) != set(streams)
                or any(p["process_instance"] != observed[1 if p["case"].startswith("restarted/") else 0]
                       for p in payload["probes"])):
            errors.append("packaged_restart_process_set_invalid")
        restart = read("restart-reference.json") if "restart-reference.json" in names else {}
        last = payload["probes"][-1]
        if (restart.get("process_instance") != last.get("process_instance")
                or restart.get("result", {}).get("question", {}).get("snapshot", {}).get("source_revision")
                != last.get("source_before", {}).get("revision")):
            errors.append("packaged_restart_reference_missing_or_invalid")
    if result["job"].startswith("package/"):
        if "production-question.json" not in names or not check_unknown_question(read("production-question.json")):
            errors.append("packaged_unknown_question_evidence_missing")
        if ("production-ui.json" not in names or "recovery-ui.json" not in names
                or "capture-paused.json" not in names
                or read("capture-paused.json").get("state", {}).get("mode") != "paused"):
            errors.append("packaged_behavior_evidence_missing")
        recovery = read("recovery-ui.json") if "recovery-ui.json" in names else {}
        recovered = recovery.get("body", {}).get("question", {}).get("snapshot", {})
        verified = read("recovery-verification.json") if "recovery-verification.json" in names else {}
        count, content_hash = message_digest(fixture.rows())
        if (recovery.get("visible") is not True or not check_unknown_question(recovery.get("body"))
                or verified.get("generation_id") != recovered.get("generation_id")
                or verified.get("source_revision") != recovered.get("source_revision")
                or verified.get("fixture_content_equal") is not True
                or verified.get("canonical_unchanged") is not True
                or verified.get("persisted_content_revalidated") is not True
                or verified.get("independent_rebuild_equal") is not True
                or verified.get("expected_message_count") != count
                or verified.get("expected_messages_sha256") != content_hash
                or verified.get("actual_messages_sha256") != content_hash
                or any(verified.get("actual", {}).get(key) != recovered.get(key)
                       for key in ("projection_digest", "canonical_content_digest"))):
            errors.append("packaged_recovery_verification_invalid")
        if "resources.json" not in names:
            errors.append("packaged_resource_samples_missing")
        else:
            resources = read("resources.json")
            if (type(resources.get("samples")) is not int or resources["samples"] < 1
                    or any(payload.get(key) != resources.get(key) for key in
                           ("process_tree_peak_bytes", "temporary_disk_peak_bytes"))):
                errors.append("packaged_resource_samples_mismatch")
        cancellation = read("cancellation.json") if "cancellation.json" in names else {}
        errors.extend(check_cancellation(cancellation, streams))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("OFCA_QUALIFICATION_PROCESS"):
        raise ValueError("packaged_worker_requires_owned_supervisor")
    from tools.analytics_qualification_package_workload import collect
    payload = collect(q.read_json(args.worker))
    return 0 if payload.get("complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
