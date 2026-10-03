"""Qualification rejects evidence with the wrong source, authority or clock."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest

from tools import analytics_qualification as q
from tools.analytics_qualification_packaged import artifact_context, extracted_files, check_cancellation
from tools.analytics_qualification_package_process import (
    PackagedProcess, ReceiptStream, receipt_account, settings_environment_names,
)
from tools.analytics_qualification_tracks import check_admitted_mutation, check_track, check_unknown_question

pytestmark = [pytest.mark.ci_tier("fast")]
MANIFEST = q.read_json(Path(__file__).resolve().parents[1] / "docs/analytics/acceptance-manifest.json")
PROFILE = "reference-windows-16g"


def hardware():
    return dict(MANIFEST["profiles"][PROFILE], cpu_model="Synthetic CPU", power_mode="Synthetic mode",
                instruction_requirements="AMD64")


def semantic():
    policy = MANIFEST["evidence_tracks"]["semantic_questions"]
    return {key: policy[key] for key in ("execution", "ingestion_path", "fixture_mode")} | {
        "evidence_track": "semantic_questions", "profile": PROFILE, "hardware": hardware()}


def test_only_semantic_questions_accept_the_source_exception():
    context = {"source": {"revision": "a" * 40}, "artifacts": {}}
    payload = semantic()
    assert check_track(MANIFEST, context, f"questions/{PROFILE}/populated/fresh", payload, context["source"]) == []
    for job in (f"package/{PROFILE}", f"mutation/{PROFILE}/100000", f"visibility/{PROFILE}/0"):
        assert check_track(MANIFEST, context, job, payload, context["source"]) == ["exact_package_execution_not_established"]


@pytest.mark.parametrize("field,value", [("execution", "source_diagnostic"),
    ("ingestion_path", "authorized_agent"), ("fixture_mode", "production_unknown_kinds"),
    ("evidence_track", "packaged"), ("profile", "constrained-windows-8g")])
def test_semantic_track_cannot_relabel_other_evidence(field, value):
    context, payload = {"source": {}}, semantic()
    payload[field] = value
    assert check_track(MANIFEST, context, f"questions/{PROFILE}/empty/fresh", payload, {})


def test_semantic_hardware_is_measured_and_source_is_exact():
    payload, context = semantic(), {"source": {"revision": "a"}}
    assert check_track(MANIFEST, context, f"questions/{PROFILE}/empty/fresh", payload, {"revision": "b"})
    payload["hardware"]["memory_gib"] = 8
    assert check_track(MANIFEST, context, f"questions/{PROFILE}/empty/fresh", payload, context["source"])


def test_older_protocol_records_do_not_gain_the_source_exception():
    previous = dict(MANIFEST, protocol="a07-closure.v1")
    assert check_track(previous, {"source": {}}, f"questions/{PROFILE}/empty/fresh", semantic(), {}) is None
    assert len(q.required_jobs(MANIFEST)) == 38
    assert MANIFEST["limits"]["visibility_seconds"] == 10
    assert MANIFEST["limits"]["warm_p95_seconds"] == 1


def mutation():
    return {"phase": "100_edits", "source_before": {"messages": 100000, "revision": 101},
        "source_after": {"messages": 100000, "revision": 301},
        "clocks": {"durable_canonical_commit": 1.0},
        "admitted_commits": [{"receipt_id": str(index), "origin": "authorized_agent",
            "canonical_revision": 103 + 2 * index, "operation_count": 1,
            "monotonic_ns": 1_000_000_000 + index} for index in range(100)]}


def test_admitted_commits_preserve_operation_counts_without_fixture_revision_deltas():
    assert check_admitted_mutation(MANIFEST, mutation()) == []


@pytest.mark.parametrize("fault", ["missing", "duplicate", "origin", "revision", "count", "clock", "polling"])
def test_admitted_commit_count_authority_and_durable_clock_are_required(fault):
    phase = mutation()
    if fault == "missing":
        phase["admitted_commits"].pop()
    elif fault == "duplicate":
        phase["admitted_commits"][-1]["receipt_id"] = "0"
    elif fault == "origin":
        phase["admitted_commits"][0]["origin"] = "creator_vault"
    elif fault == "revision":
        phase["admitted_commits"][-1]["canonical_revision"] = 101
    elif fault == "count":
        phase["admitted_commits"][0]["operation_count"] = 2
    elif fault == "clock":
        phase["admitted_commits"][-1]["monotonic_ns"] = 1
    else:
        phase["clocks"]["durable_canonical_commit"] = 1.001
    assert check_admitted_mutation(MANIFEST, phase)


def test_creator_deletions_require_the_authenticated_vault_origin():
    phase = mutation()
    phase["phase"] = "100_creator_deletions"
    assert check_admitted_mutation(MANIFEST, phase)
    for receipt in phase["admitted_commits"]:
        receipt["origin"] = "creator_vault"
    assert check_admitted_mutation(MANIFEST, phase) == []


def test_unchanged_build_cannot_hide_a_revision_change():
    phase = {"phase": "unchanged_rebuild", "admitted_commits": [],
             "source_before": {"revision": 1}, "source_after": {"revision": 2}}
    assert check_admitted_mutation(MANIFEST, phase)


def event(index, kind, **fields):
    return dict(schema="analytics-lifecycle.v1", sequence=index, run_id="a" * 64,
                pid=42, event_type=kind, monotonic_ns=100 + index, **fields)


def stream(tmp_path):
    value = ReceiptStream(tmp_path / "receipt.ndjson", "a" * 64, 42, 100)
    value.append(json.dumps(event(1, "startup", clock_resolution_ns=1,
                                 clock_implementation="synthetic")).encode(), 200)
    return value


@pytest.mark.parametrize("fault", ["sequence", "pid", "run_id", "clock", "future", "duplicate_field", "footer"])
def test_receipt_loss_foreign_process_and_invalid_clocks_fail(tmp_path, fault):
    value = stream(tmp_path)
    item = event(2, "canonical_commit")
    if fault == "sequence":
        item["sequence"] = 3
    elif fault == "pid":
        item["pid"] = 43
    elif fault == "run_id":
        item["run_id"] = "b" * 64
    elif fault == "clock":
        item["monotonic_ns"] = 99
    elif fault == "future":
        item["monotonic_ns"] = 201
    if fault == "footer":
        with pytest.raises(ValueError, match="incomplete"):
            value.check_complete()
    else:
        encoded = json.dumps(item).encode()
        if fault == "duplicate_field":
            encoded = encoded[:-1] + b', "sequence": 2}'
        with pytest.raises(ValueError):
            value.append(encoded, 200)


def test_receipt_footer_requires_joined_workers(tmp_path):
    value = stream(tmp_path)
    value.append(json.dumps(event(2, "shutdown", workers_joined=True)).encode(), 200)
    value.append(json.dumps(event(3, "footer", complete=True)).encode(), 200)
    value.check_complete()
    value.records[-1]["complete"] = False
    with pytest.raises(ValueError, match="incomplete"):
        value.check_complete()
    assert receipt_account("a" * 64, "synthetic") != receipt_account("b" * 64, "synthetic")


def package(tmp_path, extra=None):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "Brain.exe").write_bytes(b"synthetic executable bytes")
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.write(root / "Brain.exe", "Brain.exe")
        if extra:
            output.writestr(extra, "synthetic")
    return root, archive


@pytest.mark.parametrize("fault", ["changed", "extra", "parent", "absolute", "backslash"])
def test_extracted_package_must_match_every_immutable_archive_byte(tmp_path, fault):
    path = {"parent": "../escape", "absolute": "/escape", "backslash": "a\\b"}.get(fault)
    root, archive = package(tmp_path, path)
    if fault == "changed":
        (root / "Brain.exe").write_bytes(b"changed")
    elif fault == "extra":
        (root / "extra.txt").write_text("extra")
    with pytest.raises(ValueError):
        extracted_files(archive, root)


def test_exact_artifact_and_archive_hashes_are_rechecked(tmp_path):
    root, archive = package(tmp_path)
    assert extracted_files(archive, root) == {"Brain.exe": q.file_digest(root / "Brain.exe")}
    values = {"schema": "analytics-package-inputs.v1", "artifacts": {
        name: {"path": str(archive), "sha256": q.file_digest(archive)} for name in ("installer", "runtime")}}
    artifact_context(values)
    values["artifacts"]["installer"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash_mismatch"):
        artifact_context(values)


@pytest.mark.parametrize("coverage", ["unknown", "partial", "complete"])
def test_production_unknown_is_not_a_positive_match_and_coverage_is_independent(coverage):
    value = {"page": {"rows": [], "total_matching_conversations": 0,
        "evaluated_conversation_count": 101, "undetermined_conversation_count": 101,
        "coverage": {"history": coverage, "ordering": "inferred"}},
        "question": {"snapshot": {"source_revision": 42, "source_message_count": 100000}}}
    assert check_unknown_question(value)
    changed = deepcopy(value)
    changed["page"]["rows"] = [{"reason": "no_later_creator_reply"}]
    assert not check_unknown_question(changed)
    changed = deepcopy(value)
    changed["page"]["undetermined_conversation_count"] = 0
    assert not check_unknown_question(changed)


@pytest.mark.parametrize("value", [None, [], {}, {"page": None}, {"question": None}, {"page": {"coverage": []}}])
def test_malformed_question_evidence_is_rejected(value):
    assert not check_unknown_question(value)


def test_independent_fixture_preserves_capture_fields_and_mutation_sets():
    from tools.analytics_qualification_package_fixture import Fixture
    fixture = Fixture(1000, "2026-09-27T08:00:00+00:00")
    rows = fixture.messages
    assert rows["matrix-input-0"] == ["matrix-input-0", "chat-0", "chat-0",
        "Synthetic support message 0", "2026-09-25T08:00:00.000Z", "inbound"]
    assert rows["matrix-input-1"][2] == "synthetic-continuous-owner"
    assert rows["matrix-input-500"][1:3] == ["chat-1", "chat-1"]
    assert rows["matrix-input-600"][-1] == "outbound"
    fixture.append("matrix-current", 1)
    fixture.edit()
    fixture.delete()
    fixture.history_batch(0)
    assert len(fixture.rows()) == 1002
    assert fixture.messages["matrix-input-0"][3] == "Synthetic changed support 0"
    assert "matrix-input-100" not in fixture.messages
    assert fixture.messages["matrix-history-0"][4] == "2026-08-28T08:00:00.000Z"


def test_browser_fixture_survives_production_capture_and_matches_independent_fields():
    """Every offered fixture shape retains its fields through the production normalizer."""
    import shutil

    from app.protocol.common import RawMessage
    from tools.analytics_qualification_package_fixture import Fixture

    node = shutil.which("node")
    assert node is not None, "Node.js is required for the capture fixture contract"
    root = Path(__file__).resolve().parents[1]
    config = {"size": 1000, "evaluation_clock": MANIFEST["fixture"]["evaluation_clock"],
              "synthetic_account_id": "synthetic-continuous-owner"}
    script = r'''
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { seedMessage, appendedMessage, editedMessages, historicalMessages, interleavedMessage }
  from './tools/e2e-capture/lib/packaged-analytics-fixture.mjs';
import { normalizeMessageRecord } from './extension/capture/normalization.mjs';
import { mapPlatformObservation } from './extension/transport/read-only-capture-ingestion.mjs';

const input = JSON.parse(readFileSync(0, 'utf8'));
const offered = new Map(), canonical = new Map();
function capture(records) {
  for (const raw of records) {
    const record = normalizeMessageRecord(raw, { contextChatId: raw.chat_id });
    assert.notEqual(record, null, 'The offered record must survive the page-hook normalizer');
    const mapped = mapPlatformObservation({ event_type: 'message.observed',
      observed_at: input.evaluation_clock,
      source_path: `/api2/v2/chats/${encodeURIComponent(raw.chat_id)}/messages`,
      creator_platform_user_id: input.synthetic_account_id, context_chat_id: raw.chat_id, record });
    assert.equal(mapped.ok, true);
    assert.equal(mapped.change.type, 'message.upsert');
    offered.set(raw.id, raw);
    canonical.set(raw.id, mapped.change.message);
  }
}
capture(Array.from({ length: input.size }, (_, index) => seedMessage(index, input.size, input)));
capture([appendedMessage('matrix-current', 1, input)]);
capture(editedMessages(offered));
for (const batch of [0, 99]) {
  capture(historicalMessages(batch, input));
  capture([interleavedMessage(batch, input)]);
}
const missingCounterparty = { ...seedMessage(0, input.size, input) };
delete missingCounterparty.chatUserId;
assert.equal(normalizeMessageRecord(missingCounterparty, { contextChatId: missingCounterparty.chat_id }), null);
process.stdout.write(JSON.stringify([...canonical.values()]));
'''
    result = subprocess.run([node, "--input-type=module", "-e", script], cwd=root,
                            input=json.dumps(config), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    records = json.loads(result.stdout)
    for record in records:
        RawMessage.model_validate_json(json.dumps(record))
    actual = sorted([
        [record[key] for key in ("message_id", "chat_id", "sender_platform_user_id",
                                "text", "sent_at", "direction")]
        for record in records
    ])
    expected = Fixture(config["size"], config["evaluation_clock"])
    expected.append("matrix-current", 1)
    expected.edit()
    for batch in (0, 99):
        expected.history_batch(batch)
    assert actual == expected.rows()


@pytest.mark.parametrize("fault", [None, "completed", "different_attempt", "before_request", "not_full", "missing"])
def test_cancellation_requires_the_same_running_full_build(fault):
    record = {"process_instance": "42:synthetic", "attempt_id": "build-1", "request_ns": 100,
              "started_sequence": 2, "cancelled_sequence": 3}
    start = event(2, "build_started", attempt_id="build-1", full_rebuild=True,
                  account_ref="a" * 64, canonical_revision=42)
    cancel = event(3, "build_cancelled", attempt_id="build-1", full_rebuild=True,
                   account_ref="a" * 64, canonical_revision=42)
    if fault == "completed":
        cancel["event_type"] = "build_completed"
    elif fault == "different_attempt":
        cancel["attempt_id"] = "build-2"
    elif fault == "before_request":
        record["request_ns"] = 200
    elif fault == "not_full":
        start["full_rebuild"] = False
    streams = {} if fault == "missing" else {record["process_instance"]: [start, cancel]}
    assert bool(check_cancellation(record, streams)) is (fault is not None)


def test_launch_failure_closes_both_private_pipes(tmp_path, monkeypatch):
    descriptors, real_pipe = [], os.pipe

    def pipe():
        result = real_pipe()
        descriptors.extend(result)
        return result

    def fail(*args, **kwargs):
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(os, "pipe", pipe)
    monkeypatch.setattr(subprocess, "Popen", fail)
    process = PackagedProcess(Path(sys.executable), tmp_path, tmp_path / "launch", "a" * 64)
    with pytest.raises(OSError, match="synthetic launch failure"):
        process.start()
    assert len(descriptors) == 4
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    process.stop()


def test_settings_discovery_does_not_instantiate_application_configuration(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def reject_settings(name, *args, **kwargs):
        if name == "app.core.config":
            raise AssertionError("controller must not instantiate application settings")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_settings)
    assert {"AUTH_DATABASE_PATH", "SECURITY_SIGNING_SECRET", "WEBSOCKET_AUTH_MODE"} <= settings_environment_names()


def test_windowed_supervisor_closes_parent_pipe_and_retains_complete_receipts(tmp_path, monkeypatch):
    executable = Path(sys._base_executable)
    if os.name == "nt":
        executable = executable.with_name("pythonw.exe")
        assert executable.is_file()
    source = str(Path(__file__).resolve().parents[1])
    marker = tmp_path / "ready"
    script = """
import sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.core import lifecycle_receipts
from app.core.process_lifetime import parent_lifetime
stopped = threading.Event()
lifecycle_receipts.start()
with parent_lifetime(stopped.set):
    Path(sys.argv[2]).write_text('ready')
    observed = stopped.wait(10)
lifecycle_receipts.finish(observed)
raise SystemExit(0 if observed else 1)
"""
    real_popen = subprocess.Popen

    def launch(command, **options):
        assert command == [str(executable), "--brain"]
        assert "creationflags" not in options
        assert options["env"]["BRAIN_PARENT_LIFETIME_HANDLE"] != "invalid"
        return real_popen([str(executable), "-c", script, source, str(marker)], **options)

    monkeypatch.setenv("BRAIN_PARENT_LIFETIME_HANDLE", "invalid")
    monkeypatch.setattr(subprocess, "Popen", launch)
    process = PackagedProcess(executable, tmp_path, tmp_path / "windowed", "a" * 64).start()
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline and process.process.poll() is None:
            time.sleep(.01)
        assert marker.exists()
    finally:
        process.stop()
    process.stop()
    assert process.lifetime_write_fd is None
    assert process.process.returncode == 0
    assert [item["event_type"] for item in process.receipts.snapshot()] == ["startup", "shutdown", "footer"]
