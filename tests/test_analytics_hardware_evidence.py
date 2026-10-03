"""Reject stale or incompletely bound virtual-profile observations."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import analytics_qualification as q
from tools import analytics_qualification_hardware_evidence as evidence
from tools import analytics_qualification_storage as storage
from tests.test_analytics_qualification_storage import PROFILE, PATHS, storage_snapshot

pytestmark = [pytest.mark.ci_tier("fast")]

PROFILE_NAME = "reference-windows-16g"
COLLECTOR = "tools/analytics_qualification_guest.ps1"
BASE = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)


def stamp(seconds):
    return {"utc": (BASE + timedelta(seconds=seconds)).isoformat(),
            "monotonic_ns": (seconds + 100) * 1_000_000_000}


def record(expected, settings, *, phase="pre", offset=0):
    snapshot = storage_snapshot()
    for side in ("Host", "Guest"):
        snapshot[side]["ObservedUtc"] = stamp(offset + 2)["utc"]
    before, after = deepcopy(snapshot["Guest"]), deepcopy(snapshot["Guest"])
    before["ObservedUtc"] = stamp(offset + 1)["utc"]
    after["ObservedUtc"] = stamp(offset + 3)["utc"]
    request = {"schema": evidence.SCHEMA, "phase": phase, "binding": deepcopy(expected),
               "nonce": ("1" if phase == "pre" else "2") * 32, "vm_id": settings["vm_id"],
               "producer_sha256": settings["producer_sha256"], "guest_collector_sha256": "c" * 64}
    response = {"schema": evidence.SCHEMA, "request_sha256": q.digest(request),
                "producer_sha256": settings["producer_sha256"], "guest_collector_sha256": "c" * 64,
                "snapshot": snapshot}
    return {"schema": evidence.SCHEMA, "request": request, "response": response,
            "started": stamp(offset), "received": stamp(offset + 3), "finished": stamp(offset + 4),
            "local_before": before, "local_after": after}


@pytest.fixture
def raw_record():
    snapshot = storage_snapshot()
    settings = {"schema": "analytics-hardware-handoff.v1", "directory": r"C:\handoff",
                "vm_id": snapshot["Transport"]["VMId"], "producer_sha256": "a" * 64}
    expected = {"attempt_id": "attempt", "start": {"started": stamp(-1)}, "paths": PATHS}
    return record(expected, settings), expected, settings


def test_raw_snapshot_establishes_the_declared_topology(raw_record):
    raw, expected, settings = raw_record
    topology = evidence.validate_record(raw, expected, settings, PROFILE)
    assert topology["schema"] == "analytics-storage-topology.v1"
    assert topology["guest"]["memory_gib"] == PROFILE["memory_gib"]


@pytest.mark.parametrize("location", ["Host", "Guest", "local_before", "local_after"])
@pytest.mark.parametrize("seconds", [-3600, 3600])
def test_raw_observation_requires_the_current_window(raw_record, location, seconds):
    raw, expected, settings = raw_record
    target = raw[location] if location.startswith("local_") else raw["response"]["snapshot"][location]
    target["ObservedUtc"] = stamp(seconds)["utc"]
    with pytest.raises(ValueError, match="stale_or_future"):
        evidence.validate_record(raw, expected, settings, PROFILE)


@pytest.mark.parametrize("field,value", [
    ("nonce", "not-a-nonce"), ("phase", "other"), ("producer_sha256", "b" * 64),
    ("vm_id", "00000000-0000-0000-0000-000000000000"),
])
def test_raw_request_identity_cannot_change(raw_record, field, value):
    raw, expected, settings = raw_record
    raw["request"][field] = value
    raw["response"]["request_sha256"] = q.digest(raw["request"])
    with pytest.raises(ValueError, match="binding_mismatch"):
        evidence.validate_record(raw, expected, settings, PROFILE)


@pytest.mark.parametrize("mutation", ["hash", "collector", "producer", "source", "deadline", "before_attempt"])
def test_raw_receipt_rejects_rebinding_and_invalid_duration(raw_record, mutation):
    raw, expected, settings = raw_record
    if mutation == "hash":
        raw["response"]["request_sha256"] = "0" * 64
    elif mutation == "collector":
        raw["response"]["guest_collector_sha256"] = "d" * 64
    elif mutation == "producer":
        raw["response"]["producer_sha256"] = "b" * 64
    elif mutation == "source":
        raw["request"]["binding"]["source_sha256"] = "0" * 64
        raw["response"]["request_sha256"] = q.digest(raw["request"])
    elif mutation == "deadline":
        raw["finished"] = stamp(evidence.WAIT_SECONDS + 61)
    else:
        raw["started"] = stamp(-2)
    with pytest.raises(ValueError):
        evidence.validate_record(raw, expected, settings, PROFILE)


def test_raw_local_topology_change_is_rejected(raw_record):
    raw, expected, settings = raw_record
    raw["local_after"]["BootUtc"] = stamp(-5)["utc"]
    with pytest.raises(ValueError, match="local_remote_guest_mismatch"):
        evidence.validate_record(raw, expected, settings, PROFILE)


@pytest.mark.parametrize("contents", [b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'{'])
def test_bounded_handoff_rejects_ambiguous_json(tmp_path, contents):
    path = tmp_path / "response.json"
    path.write_bytes(contents)
    with pytest.raises(ValueError):
        evidence.read_bounded(path)


def test_handoff_read_caps_the_opened_stream():
    class GrowingFile:
        def open(self, mode):
            assert mode == "rb"
            return BytesIO(b" " * (evidence.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="size_limit"):
        evidence.read_bounded(GrowingFile())


def test_guest_observer_isolates_windows_shell_module_path(monkeypatch):
    environment = {"PSModulePath": "another-shell-modules", "SystemRoot": "synthetic-system"}
    monkeypatch.setattr(evidence, "os", SimpleNamespace(name="nt", environ=environment))
    monkeypatch.setattr(evidence.shutil, "which", lambda name: "powershell.exe")
    def run(command, **kwargs):
        assert kwargs["env"] == {"SystemRoot": "synthetic-system"}
        assert kwargs["creationflags"] == getattr(evidence.subprocess, "CREATE_NO_WINDOW", 0)
        assert kwargs["timeout"] == 30 and "-NoProfile" in command
        return SimpleNamespace(returncode=0, stdout=q.encoded({"probe": "synthetic"}))
    monkeypatch.setattr(evidence.subprocess, "run", run)
    assert evidence.observe_guest(PATHS) == {"probe": "synthetic"}
    assert environment["PSModulePath"] == "another-shell-modules"


def test_handoff_timeout_never_creates_a_successful_observation(tmp_path, monkeypatch):
    observer = object.__new__(evidence.HardwareEvidence)
    observer.expected = {"paths": PATHS}
    observer.settings = {"vm_id": "test", "producer_sha256": "a" * 64}
    observer.collector_sha256 = q.file_digest(Path(evidence.__file__).with_name("analytics_qualification_guest.ps1"))
    observer.directory = tmp_path
    observer.references = []
    observer.profile = PROFILE
    monkeypatch.setattr(evidence, "observe_guest", lambda paths: {})
    ticks = iter([0, evidence.WAIT_SECONDS + 1])
    monkeypatch.setattr(evidence, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(ValueError, match="handoff_timeout"):
        observer._observe("pre")
    assert (tmp_path / "pre.request.json").is_file()
    assert not observer.references


def test_unjoined_worker_cannot_request_post_observation(tmp_path, monkeypatch):
    observer = object.__new__(evidence.HardwareEvidence)
    observer.worker_started = stamp(5)
    observer.attempt, observer.references = tmp_path, []
    monkeypatch.setattr(observer, "_observe", lambda phase: pytest.fail("post observation must not start"))
    with pytest.raises(ValueError, match="requires_joined_worker"):
        observer.after_worker({"worker_joined": False, "process_instance": "worker"})
    assert [item["name"] for item in observer.references] == ["hardware/worker.json"]


@pytest.fixture
def archive(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    q.write_once(attempt / "start.json", {"started": stamp(-1)})
    context = {"source": {"files": {COLLECTOR: "c" * 64}},
               "runtime": {"executable": r"C:\runtime\python.exe"}}
    manifest = {"profiles": {PROFILE_NAME: PROFILE}, "hardware_evidence": {"schema": evidence.SCHEMA}}
    snapshot = storage_snapshot()
    settings = {"schema": "analytics-hardware-handoff.v1", "directory": r"C:\handoff",
                "vm_id": snapshot["Transport"]["VMId"], "producer_sha256": "a" * 64}
    config = {"root": r"C:\source", "output": r"C:\results\attempt\collector",
              "hardware_runtime": {"temporary": r"C:\temporary", "node": r"C:\node\node.exe"},
              "observed": {"browser_executable": r"C:\browser\chrome.exe"},
              "inputs": {"runtime_directory": r"C:\runtime", "agent_directory": r"C:\runtime\Agent",
                         "data_directory": r"C:\data", "browser_profile": r"C:\profile",
                         "artifacts": {"runtime": {"path": r"C:\inputs\runtime.zip"},
                                       "installer": {"path": r"C:\inputs\installer.exe"}}}}
    paths = evidence.expected_paths(config, context, settings, attempt.name)
    expected = evidence.binding(attempt, context, manifest, PROFILE_NAME, paths)
    pre, post = record(expected, settings), record(expected, settings, phase="post", offset=10)
    topology = {"topology": "fixture"}
    monkeypatch.setattr(storage, "validate_snapshot", lambda *args: deepcopy(topology))
    hardware = evidence.hardware_summary(pre, topology)
    config["observed"]["hardware"] = hardware
    result = {"job": "package/" + PROFILE_NAME, "process_instance": "worker",
              "payload": {"hardware": hardware, "ended_ns": stamp(8)["monotonic_ns"]},
              "seconds": 2, "finished": stamp(15), "attachments": []}
    records = {"hardware/settings.json": settings, "worker-input.json": config,
               "hardware/pre.json": pre, "hardware/post.json": post,
               "hardware/worker.json": {"started": stamp(5), "joined": stamp(9),
                                        "worker_joined": True, "process_instance": "worker"},
               "process.json": {"started_ns": stamp(6)["monotonic_ns"]}}
    def verify():
        result["attachments"] = []
        for name, value in records.items():
            file = attempt / "inputs" / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(q.encoded(value))
            result["attachments"].append(dict(q.attach(attempt, file), name=name))
        return evidence.check_evidence(attempt, result, manifest, context)
    return records, result, verify


def test_archive_verifier_accepts_complete_bound_observations(archive):
    _, _, verify = archive
    assert verify() == []


@pytest.mark.parametrize("role", ["temporary", "node", "worker_temporary", "hardware_handoff"])
def test_archive_verifier_requires_every_workload_role(archive, role):
    records, _, verify = archive
    for phase in ("pre", "post"):
        raw = records["hardware/" + phase + ".json"]
        raw["request"]["binding"]["paths"].pop(role)
        raw["response"]["request_sha256"] = q.digest(raw["request"])
    assert "hardware_observation_binding_mismatch" in verify()


@pytest.mark.parametrize("mutation", ["phase", "nonce", "worker", "joined", "timing", "collector", "hardware", "stale_post"])
def test_archive_verifier_rejects_rebound_or_stale_records(archive, mutation):
    records, result, verify = archive
    pre, post = records["hardware/pre.json"], records["hardware/post.json"]
    if mutation == "phase":
        post["request"]["phase"] = "pre"
    elif mutation == "nonce":
        post["request"]["nonce"] = pre["request"]["nonce"]
    elif mutation == "worker":
        records["hardware/worker.json"]["process_instance"] = "another-worker"
    elif mutation == "joined":
        records["hardware/worker.json"]["worker_joined"] = False
    elif mutation == "timing":
        records["hardware/worker.json"]["joined"] = stamp(15)
    elif mutation == "collector":
        pre["request"]["guest_collector_sha256"] = "d" * 64
    elif mutation == "hardware":
        result["payload"]["hardware"] = {"disk": "SSD"}
    else:
        for kind in ("Host", "Guest"):
            post["response"]["snapshot"][kind]["ObservedUtc"] = pre["response"]["snapshot"][kind]["ObservedUtc"]
    for raw in (pre, post):
        raw["response"]["request_sha256"] = q.digest(raw["request"])
    assert verify()


def test_archive_verifier_rejects_missing_raw_post(archive):
    records, _, verify = archive
    records.pop("hardware/post.json")
    assert "hardware_raw_evidence_missing_or_invalid" in verify()


@pytest.mark.parametrize("mutation", ["settings", "missing_process", "process_clock", "duration", "result_finish", "package_end"])
def test_archive_verifier_requires_settings_and_actual_worker_clocks(archive, mutation):
    records, result, verify = archive
    if mutation == "settings":
        records["hardware/settings.json"]["schema"] = "unknown"
    elif mutation == "missing_process":
        records.pop("process.json")
    elif mutation == "process_clock":
        records["process.json"]["started_ns"] = stamp(1)["monotonic_ns"]
    elif mutation == "duration":
        result["seconds"] = 8
    elif mutation == "result_finish":
        result["finished"] = stamp(12)
    else:
        result["payload"]["ended_ns"] = stamp(12)["monotonic_ns"]
    assert verify()
