"""Bind host storage observations to one isolated guest workload."""
from __future__ import annotations

import os
from datetime import datetime
import json
import ntpath
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4

from tools import analytics_qualification as q


SCHEMA = "analytics-hardware-evidence.v1"
MAX_BYTES = 2 * 1024 * 1024
WAIT_SECONDS = 180
MAX_CLOCK_SKEW_SECONDS = 5


def read_bounded(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("hardware_evidence_size_limit")
    def unique(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("hardware_evidence_duplicate_key")
        return value
    def invalid(_):
        raise ValueError("hardware_evidence_nonfinite_number")
    value = json.loads(data, object_pairs_hook=unique, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("hardware_evidence_object_required")
    return value


def utc(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError
        return stamp.timestamp()
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("hardware_observation_utc_required") from error


def check_settings(settings):
    try:
        valid = (set(settings) == {"schema", "directory", "vm_id", "producer_sha256"}
            and settings["schema"] == "analytics-hardware-handoff.v1"
            and str(UUID(settings["vm_id"])) == settings["vm_id"].lower()
            and isinstance(settings["directory"], str)
            and ntpath.isabs(settings["directory"])
            and re.fullmatch(r"[0-9a-f]{64}", settings["producer_sha256"]))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("hardware_handoff_invalid")


def observe_guest(paths):
    if os.name != "nt":
        raise ValueError("declared_windows_profile_unavailable")
    shell = shutil.which("powershell.exe")
    if not shell:
        raise ValueError("windows_profile_observer_unavailable")
    with tempfile.TemporaryDirectory(prefix="analytics-hardware-") as temporary:
        inputs = Path(temporary) / "paths.json"
        q.write_once(inputs, paths)
        environment = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
        completed = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(Path(__file__).with_name("analytics_qualification_guest.ps1")),
            "-PathsFile", str(inputs)], capture_output=True, timeout=30, check=False, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode != 0 or len(completed.stdout) > MAX_BYTES:
            raise ValueError("guest_hardware_observation_failed")
        output = Path(temporary) / "guest.json"
        output.write_bytes(completed.stdout)
        return read_bounded(output)


def workload_paths(root, attempt, *, data=None, inputs=None, browser=None):
    paths = {"source": root, "output": attempt, "interpreter": Path(sys.executable),
             "temporary": Path(tempfile.gettempdir()), "collector": attempt / "collector"}
    if inputs is None:
        paths["data"] = data
        paths["collector_entry_point"] = root / "tools/qualify_analytics_baseline.py"
    else:
        paths.update({key: inputs[key] for key in
                      ("runtime_directory", "agent_directory", "data_directory", "browser_profile")})
        paths.update({key + "_archive": item["path"] for key, item in inputs["artifacts"].items()})
        paths.update(browser=browser, node=shutil.which("node"),
                     worker_temporary=attempt / "collector/temporary")
    if any(value is None for value in paths.values()):
        raise ValueError("hardware_workload_path_missing")
    return {key: str(Path(value).resolve()) for key, value in sorted(paths.items())}


def binding(attempt, context, manifest, profile, paths):
    return {"attempt_id": attempt.name, "start": q.read_json(attempt / "start.json"),
            "context_sha256": q.digest(context), "source_sha256": q.digest(context["source"]),
            "manifest_sha256": q.digest(manifest), "profile": profile, "paths": paths}


def expected_paths(config, context, settings, attempt_id):
    parent = ntpath if ntpath.isabs(config["output"]) else os.path
    paths = {"source": config.get("root", config.get("subject_directory")),
             "output": parent.dirname(config["output"]), "collector": config["output"],
             "interpreter": context["runtime"]["executable"],
             "temporary": config["hardware_runtime"]["temporary"],
             "hardware_handoff": parent.join(settings["directory"], attempt_id)}
    if "inputs" in config:
        paths.update({key: config["inputs"][key] for key in
                      ("runtime_directory", "agent_directory", "data_directory", "browser_profile")})
        paths.update({key + "_archive": item["path"] for key, item in config["inputs"]["artifacts"].items()})
        paths.update(browser=config["observed"]["browser_executable"],
                     node=config["hardware_runtime"]["node"],
                     worker_temporary=parent.join(config["output"], "temporary"))
    else:
        paths["data"] = config["data"]
        paths["collector_entry_point"] = config["entry_point"]
    return paths


def validate_record(record, expected, settings, profile):
    from tools.analytics_qualification_storage import validate_snapshot
    request, response = record["request"], record["response"]
    if (record.get("schema") != SCHEMA or request.get("schema") != SCHEMA
            or request.get("binding") != expected or request.get("phase") not in {"pre", "post"}
            or not re.fullmatch(r"[0-9a-f]{32}", request.get("nonce", ""))
            or response.get("schema") != SCHEMA
            or response.get("request_sha256") != q.digest(request)
            or response.get("producer_sha256") != settings["producer_sha256"]
            or request.get("vm_id") != settings["vm_id"]
            or request.get("producer_sha256") != settings["producer_sha256"]
            or response.get("guest_collector_sha256") != request.get("guest_collector_sha256")):
        raise ValueError("hardware_observation_binding_mismatch")
    times = [record[key]["monotonic_ns"] for key in ("started", "received", "finished")]
    if (any(type(value) is not int for value in times) or times != sorted(times)
            or times[0] < expected["start"]["started"]["monotonic_ns"]
            or times[-1] - times[0] > (WAIT_SECONDS + 60) * 1_000_000_000):
        raise ValueError("hardware_observation_deadline_invalid")
    snapshot = response["snapshot"]
    start, finish = utc(record["started"]["utc"]), utc(record["finished"]["utc"])
    if not 0 <= finish - start <= WAIT_SECONDS + 60:
        raise ValueError("hardware_observation_wall_clock_invalid")
    for raw in (snapshot["Host"], snapshot["Guest"], record["local_before"], record["local_after"]):
        if not start - MAX_CLOCK_SKEW_SECONDS <= utc(raw["ObservedUtc"]) <= finish + MAX_CLOCK_SKEW_SECONDS:
            raise ValueError("hardware_observation_stale_or_future")
    if snapshot["Transport"]["VMId"].casefold() != settings["vm_id"].casefold():
        raise ValueError("hardware_observation_vm_mismatch")
    before = validate_snapshot(snapshot, record["local_before"], expected["paths"], profile)
    after = validate_snapshot(snapshot, record["local_after"], expected["paths"], profile)
    if before != after:
        raise ValueError("guest_storage_changed_during_observation")
    return before


def hardware_summary(record, topology):
    guest, host = record["local_before"], record["response"]["snapshot"]["Host"]
    return {"os": "Windows", "memory_gib": guest["MemoryGiB"], "cores": guest["Cores"],
            "disk": "SSD", "cpu_model": guest["CpuModel"], "power_mode": guest["PowerMode"],
            "instruction_requirements": guest["InstructionRequirements"],
            "available_memory_bytes": guest["AvailableMemoryBytes"],
            "virtualization": "hyper-v-single-boot-disk", "host_cpu_model": host["CpuModel"],
            "host_power_mode": host["PowerMode"], "guest_media_type": guest["PhysicalDisks"][0]["MediaType"],
            "storage_evidence": {"schema": SCHEMA, "pre_sha256": q.digest(record),
                                 "topology_sha256": q.digest(topology)}}


class HardwareEvidence:
    def __init__(self, attempt, context, manifest, profile, paths, handoff):
        if handoff is None:
            raise ValueError("hardware_handoff_not_configured")
        settings = read_bounded(Path(handoff))
        check_settings(settings)
        directory = Path(settings["directory"])
        if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
            raise ValueError("hardware_handoff_directory_invalid")
        settings["directory"] = str(directory.resolve())
        self.attempt, self.settings, self.profile = attempt, settings, manifest["profiles"][profile]
        self.directory = directory / attempt.name
        self.directory.mkdir(exist_ok=False)
        paths = dict(paths, hardware_handoff=str(self.directory.resolve()))
        self.expected = binding(attempt, context, manifest, profile, paths)
        self.runtime_paths = {key: paths[key] for key in ("temporary", "node") if key in paths}
        self.collector_sha256 = q.file_digest(Path(__file__).with_name("analytics_qualification_guest.ps1"))
        self.references = []
        self._save("settings", settings)
        self.pre, self.topology = self._observe("pre")
        self.hardware = hardware_summary(self.pre, self.topology)

    def _save(self, name, value):
        path = self.attempt / "hardware" / (name + ".json")
        q.write_once(path, value)
        self.references.append(dict(q.attach(self.attempt, path), name="hardware/" + name + ".json"))

    def _observe(self, phase):
        collector = Path(__file__).with_name("analytics_qualification_guest.ps1")
        if q.file_digest(collector) != self.collector_sha256:
            raise ValueError("hardware_collector_changed")
        started = q.stamp()
        before = observe_guest(self.expected["paths"])
        request = {"schema": SCHEMA, "phase": phase, "nonce": uuid4().hex,
                   "binding": self.expected, "vm_id": self.settings["vm_id"],
                   "producer_sha256": self.settings["producer_sha256"],
                   "guest_collector_sha256": self.collector_sha256}
        q.write_once(self.directory / (phase + ".request.json"), request)
        response_path = self.directory / (phase + ".response.json")
        deadline = time.monotonic() + WAIT_SECONDS
        while not response_path.exists():
            if time.monotonic() >= deadline:
                raise ValueError("hardware_handoff_timeout")
            time.sleep(0.1)
        response, received = read_bounded(response_path), q.stamp()
        after = observe_guest(self.expected["paths"])
        if q.file_digest(collector) != self.collector_sha256:
            raise ValueError("hardware_collector_changed")
        record = {"schema": SCHEMA, "request": request, "response": response,
                  "started": started, "received": received, "finished": q.stamp(),
                  "local_before": before, "local_after": after}
        self._save(phase, record)
        try:
            return record, validate_record(record, self.expected, self.settings, self.profile)
        except (KeyError, TypeError, AttributeError, IndexError) as error:
            raise ValueError("hardware_observation_malformed") from error

    def before_worker(self):
        self.worker_started = q.stamp()

    def after_worker(self, result):
        self._save("worker", {"started": self.worker_started, "joined": q.stamp(),
            "process_instance": result.get("process_instance"), "worker_joined": result.get("worker_joined")})
        if result.get("worker_joined") is not True:
            raise ValueError("hardware_post_requires_joined_worker")
        post, topology = self._observe("post")
        if topology != self.topology or post["request"]["nonce"] == self.pre["request"]["nonce"]:
            raise ValueError("hardware_topology_changed_during_worker")


def check_evidence(attempt, result, manifest, context):
    if "hardware_evidence" not in manifest or result["job"] in {"source-ci", "regression"}:
        return []
    try:
        references = result.get("attachments", [])
        names = {item["name"]: item for item in references}
        if len(names) != len(references):
            raise ValueError("hardware_evidence_duplicate")
        def read(name):
            return read_bounded(attempt / names[name]["path"])
        settings = read("hardware/settings.json")
        check_settings(settings)
        config, pre, post = read("worker-input.json"), read("hardware/pre.json"), read("hardware/post.json")
        profile = result["job"].split("/")[1]
        paths = expected_paths(config, context, settings, attempt.name)
        expected = binding(attempt, context, manifest, profile, paths)
        collector = "tools/analytics_qualification_guest.ps1"
        if pre["request"]["guest_collector_sha256"] != context["source"]["files"][collector]:
            raise ValueError("hardware_collector_source_mismatch")
        if post["request"]["guest_collector_sha256"] != pre["request"]["guest_collector_sha256"]:
            raise ValueError("hardware_collector_changed")
        before = validate_record(pre, expected, settings, manifest["profiles"][profile])
        after = validate_record(post, expected, settings, manifest["profiles"][profile])
        worker = read("hardware/worker.json")
        started, joined = worker["started"]["monotonic_ns"], worker["joined"]["monotonic_ns"]
        if (pre["request"]["phase"] != "pre" or post["request"]["phase"] != "post"
                or pre["request"]["nonce"] == post["request"]["nonce"] or before != after
                or worker.get("process_instance") != result["process_instance"]
                or worker.get("worker_joined") is not True
                or not pre["finished"]["monotonic_ns"] <= worker["started"]["monotonic_ns"]
                <= worker["joined"]["monotonic_ns"] <= post["started"]["monotonic_ns"]):
            raise ValueError("hardware_worker_boundary_invalid")
        if (not q.finite(result.get("seconds")) or result["seconds"] > (joined - started) / 1_000_000_000
                or post["finished"]["monotonic_ns"] > result["finished"]["monotonic_ns"]):
            raise ValueError("hardware_worker_duration_binding_invalid")
        process_starts = []
        for name in names:
            if name == "process.json" or name.endswith("/process.json"):
                process = read(name)
                observed = process.get("started_ns", process.get("started", {}).get("monotonic_ns"))
                if observed is not None:
                    process_starts.append(observed)
        if not process_starts or any(type(value) is not int or not started <= value <= joined for value in process_starts):
            raise ValueError("hardware_process_clock_binding_invalid")
        if "inputs" in config and not started <= result["payload"].get("ended_ns", -1) <= joined:
            raise ValueError("hardware_package_end_binding_invalid")
        for kind in ("Host", "Guest"):
            if utc(post["response"]["snapshot"][kind]["ObservedUtc"]) <= utc(pre["response"]["snapshot"][kind]["ObservedUtc"]):
                raise ValueError("hardware_post_observation_not_fresh")
        hardware = hardware_summary(pre, before)
        configured = config.get("observed", {}).get("hardware") if "inputs" in config else config.get("hardware")
        if configured != hardware or result["payload"].get("hardware") != hardware:
            raise ValueError("hardware_payload_binding_mismatch")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        return [str(error) if isinstance(error, ValueError) else "hardware_raw_evidence_missing_or_invalid"]
    return []
