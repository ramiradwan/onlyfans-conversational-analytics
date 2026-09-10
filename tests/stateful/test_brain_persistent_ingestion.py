"""Tier B file-backed restart qualification for the HistoryRepository seam."""

from __future__ import annotations

import json
import os
import platform
from dataclasses import fields, is_dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from hypothesis import HealthCheck, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, precondition, rule

from tests.state_models.brain_ingestion_model import (
    ModelChatDeleteCommand, ModelChatUpsertCommand, ModelCoverageObservedCommand,
    ModelMessageDeleteCommand, ModelMessageUpsertCommand,
    ModelSnapshotBeginCommand, ModelSnapshotChunkCommand, ModelSnapshotCommitCommand,
    ModelStreamKey, PureBrainIngestionModel,
)
from tests.state_models.sqlite_brain_adapter import BrokenReopenAdapter, SQLiteBrainAdapter
from tests.state_models.transition_oracle import OracleMismatchError, assert_transition_oracle


for name, examples, steps in (
    ("tier_b_general", 15, 25), ("tier_b_deletion", 10, 20),
    ("windows_persistence_smoke", 5, 12),
):
    settings.register_profile(name, max_examples=examples, stateful_step_count=steps,
                              deadline=None, suppress_health_check=[HealthCheck.too_slow])
if os.environ.get("HYPOTHESIS_PROFILE", "").startswith(("tier_b_", "windows_persistence")):
    settings.load_profile(os.environ["HYPOTHESIS_PROFILE"])


def stable_uuid(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ofca-persistent_ingestion/{label}")


def chat_record(chat_id: str) -> dict[str, Any]:
    return {"tombstone": False, "chat": {"record_kind": "full", "chat_id": chat_id,
            "platform_user_id": f"fan-{chat_id}", "display_name": chat_id,
            "updated_at": "2026-09-10T10:00:00+00:00"}}


def message_record(message_id: str, chat_id: str) -> dict[str, Any]:
    return {"tombstone": False, "message": {"message_id": message_id, "chat_id": chat_id,
            "sender_platform_user_id": f"fan-{chat_id}", "text": message_id,
            "sent_at": "2026-09-10T10:01:00+00:00", "direction": "inbound"}}


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID): return str(value)
    if is_dataclass(value): return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict): return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_json_value(v) for v in value]
    return value


def _key_json(key: ModelStreamKey) -> dict[str, str]:
    return {"creator_account_id": key.creator_account_id, "agent_installation_id": str(key.agent_installation_id), "agent_stream_id": str(key.agent_stream_id)}


def _key_from_json(value: dict[str, str]) -> ModelStreamKey:
    return ModelStreamKey(value["creator_account_id"], UUID(value["agent_installation_id"]), UUID(value["agent_stream_id"]))


def _record_metric(name: str) -> None:
    path = os.environ.get("PERSISTENT_INGESTION_METRICS_PATH")
    if not path:
        return
    target = Path(path)
    values = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"histories": 0, "transitions": 0, "reopens": 0}
    values[name] = int(values.get(name, 0)) + 1
    target.write_text(json.dumps(values), encoding="utf-8")


def _command_from_trace(operation: str, value: dict[str, Any]) -> Any:
    if operation == "commit_delta":
        event_id = UUID(value["event_id"])
        if "coverage_type" in value:
            payload = dict(value["evidence_payload"])
            if "generation_id" in payload:
                payload["generation_id"] = UUID(payload["generation_id"])
            return ModelCoverageObservedCommand(event_id, value["source_seq"], value["coverage_type"], payload, value.get("acquisition_origin", "passive"))
        if "record_kind" not in value and "message_id" in value and "text" not in value:
            return ModelMessageDeleteCommand(event_id, value["source_seq"], value["message_id"], value["chat_id"], value.get("acquisition_origin", "passive"))
        if "record_kind" not in value and "message_id" not in value:
            return ModelChatDeleteCommand(event_id, value["source_seq"], value["chat_id"], value.get("acquisition_origin", "passive"))
        if "message_id" in value:
            return ModelMessageUpsertCommand(event_id, value["source_seq"], value["message_id"], value["chat_id"], value["sender_platform_user_id"], value["text"], value["sent_at"], value["direction"], value.get("acquisition_origin", "passive"))
        return ModelChatUpsertCommand(event_id, value["source_seq"], value["chat_id"], value["record_kind"], value["platform_user_id"], value["display_name"], value["updated_at"], value.get("acquisition_origin", "passive"))
    if operation == "begin_snapshot":
        return ModelSnapshotBeginCommand(UUID(value["snapshot_id"]), value["through_seq"], value["chunk_count"], value["expected_chats"], value["expected_messages"], value["expected_coverage"], value["max_frame_bytes"])
    if operation == "add_snapshot_chunk":
        return ModelSnapshotChunkCommand(UUID(value["snapshot_id"]), value["chunk_index"], value["entity_kind"], value["records"], value["estimated_bytes"])
    if operation == "commit_snapshot":
        return ModelSnapshotCommitCommand(UUID(value["snapshot_id"]), value["chunk_count"])
    raise AssertionError(f"unsupported replay operation {operation}")


class PersistentHarness:
    """Model/production harness which can close and reopen its canonical file."""

    def __init__(self, root: Path, label: str, adapter_type: type[SQLiteBrainAdapter] = SQLiteBrainAdapter) -> None:
        self.label, self.root = label, root
        self.account = f"persistent_ingestion-{label}"
        self.key = self.stream("main")
        self.model = PureBrainIngestionModel(self.account)
        self.adapter = adapter_type(root / "canonical.sqlite3", connection_id=stable_uuid(f"{label}/connection"))
        self.counter = 0
        self.trace: list[dict[str, Any]] = []

    def stream(self, name: str) -> ModelStreamKey:
        return ModelStreamKey(self.account, stable_uuid(f"{self.label}/{name}/installation"), stable_uuid(f"{self.label}/{name}/stream"))

    def new_id(self, purpose: str) -> UUID:
        self.counter += 1
        return stable_uuid(f"{self.label}/{purpose}/{self.counter}")

    def next_seq(self, key: ModelStreamKey | None = None) -> int:
        checkpoint = self.model.checkpoint(key or self.key)
        return (checkpoint or 0) + 1

    def frame(self, command: Any, operation: str, *, key: ModelStreamKey | None = None) -> Any:
        target = key or self.key
        before = self.adapter.observe_state(target)
        model_outcome = getattr(self.model, operation)(target, command)
        production_outcome = getattr(self.adapter, operation)(target, command)
        after = self.adapter.observe_state(target)
        self.trace.append({"operation": operation, "stream_key": _key_json(target), "command": _json_value(command), "requirements": []})
        _record_metric("transitions")
        assert_transition_oracle(model=self.model, adapter=self.adapter, key=target, command=command,
                                 model_outcome=model_outcome, prod_outcome=production_outcome,
                                 snapshot_before=before, snapshot_after=after, command_history=self.trace)
        return production_outcome

    def seed(self) -> None:
        snapshot = self.new_id("seed")
        self.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 1, 0, 0), "begin_snapshot")
        self.frame(ModelSnapshotChunkCommand(snapshot, 0, "chat", [chat_record("base")]), "add_snapshot_chunk")
        self.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot")

    def reopen(self) -> None:
        before = self.adapter.observe_state(self.key)
        self.adapter.close_and_reopen()
        _record_metric("reopens")
        assert self.adapter.observe_state(self.key) == before
        self.trace.append({"operation": "close_and_reopen", "stream_key": _key_json(self.key), "command": {}, "requirements": []})

    def configure_coverage(self) -> None:
        values = {"consent_policy_version": "history-consent-v1", "consent_revision": "consent-1",
                  "authorized_platform_creator_id": "creator", "desired_state": "running",
                  "recent_window_days": 30, "page_size": 100, "pages_per_wake": 2,
                  "request_interval_ms": 500, "retry_limit": 3}
        saved = self.adapter.history.update_history_settings(self.account, expected_revision=0, values=values)
        self.adapter.history.bind_history_config(self.account, settings_revision=int(saved["settings_revision"]), config_revision="persistent_ingestion")
        self.adapter.history.mark_history_config_applied(self.account, "persistent_ingestion")
        self.trace.append({"operation": "configure_coverage", "stream_key": _key_json(self.key), "command": values, "requirements": []})


def replay_json_trace(trace: list[dict[str, Any]], harness: PersistentHarness) -> None:
    """Replay the complete logical ingestion command vocabulary from JSON data."""
    for entry in json.loads(json.dumps(trace)):
        operation = entry["operation"]
        if operation == "close_and_reopen":
            harness.reopen()
        elif operation == "configure_coverage":
            harness.configure_coverage()
        else:
            harness.frame(_command_from_trace(operation, entry["command"]), operation,
                          key=_key_from_json(entry["stream_key"]))


def test_file_backed_reopen_preserves_duplicate_gap_recovery_and_trace_replay(tmp_path: Path) -> None:
    harness = PersistentHarness(tmp_path / "primary", "reopen")
    harness.seed()
    accepted = ModelChatUpsertCommand(harness.new_id("event"), 2, "after-reopen", "full", "fan-after", "after", "2026-09-10T10:02:00+00:00")
    harness.frame(accepted, "commit_delta")
    harness.reopen()
    assert harness.frame(accepted, "commit_delta").disposition == "duplicate"
    gap = ModelChatUpsertCommand(harness.new_id("event"), 4, "gap", "full", "fan-gap", "gap", "2026-09-10T10:03:00+00:00")
    assert harness.frame(gap, "commit_delta").disposition == "gap"
    recovered = ModelChatUpsertCommand(harness.new_id("event"), 3, "recovered", "full", "fan-recovered", "recovered", "2026-09-10T10:04:00+00:00")
    assert harness.frame(recovered, "commit_delta").disposition == "accepted"
    harness.reopen()
    assert json.loads(json.dumps(harness.trace)) == harness.trace

    replay = PersistentHarness(tmp_path / "replay", "reopen")
    replay_json_trace(harness.trace, replay)
    assert replay.adapter.observe_state(replay.key) == harness.adapter.observe_state(harness.key)


def test_full_brain_ingestion_vocabulary_json_trace_round_trips_to_new_persistent_database(tmp_path: Path) -> None:
    source = PersistentHarness(tmp_path / "source", "full-trace")
    source.seed()
    source.frame(ModelChatUpsertCommand(source.new_id("event"), 2, "parent", "full", "fan-parent", "parent", "2026-09-10T10:00:00+00:00"), "commit_delta")
    source.frame(ModelMessageUpsertCommand(source.new_id("event"), 3, "message", "parent", "fan-parent", "message", "2026-09-10T10:01:00+00:00", "inbound"), "commit_delta")
    source.frame(ModelMessageDeleteCommand(source.new_id("event"), 4, "message", "parent"), "commit_delta")
    source.configure_coverage()
    source.frame(ModelCoverageObservedCommand(source.new_id("event"), 5, "generation.started", {"generation_id": source.new_id("generation"), "as_of": "2026-09-10T11:00:00Z", "authorization_revision": "consent-1"}), "commit_delta")
    source.frame(ModelChatDeleteCommand(source.new_id("event"), 6, "parent"), "commit_delta")
    source.reopen()
    target = PersistentHarness(tmp_path / "target", "full-trace")
    replay_json_trace(source.trace, target)
    assert target.adapter.observe_state(target.key) == source.adapter.observe_state(source.key)


def test_file_backed_staging_and_deletion_tombstone_survive_reopen(tmp_path: Path) -> None:
    harness = PersistentHarness(tmp_path, "staging")
    harness.seed()
    message = ModelMessageUpsertCommand(harness.new_id("event"), 2, "child", "base", "fan-base", "child", "2026-09-10T10:01:00+00:00", "inbound")
    harness.frame(message, "commit_delta")
    staged = harness.new_id("staged")
    harness.frame(ModelSnapshotBeginCommand(staged, 3, 2, 1, 1, 0), "begin_snapshot")
    harness.frame(ModelSnapshotChunkCommand(staged, 0, "chat", [chat_record("staged-chat")]), "add_snapshot_chunk")
    harness.reopen()
    harness.frame(ModelSnapshotChunkCommand(staged, 0, "chat", [chat_record("staged-chat")]), "add_snapshot_chunk")
    harness.frame(ModelSnapshotChunkCommand(staged, 1, "message", [message_record("staged-message", "staged-chat")]), "add_snapshot_chunk")
    harness.frame(ModelSnapshotCommitCommand(staged, 2), "commit_snapshot")
    delete = ModelChatDeleteCommand(harness.new_id("event"), 4, "base")
    harness.frame(delete, "commit_delta")
    harness.reopen()
    observed = harness.adapter.observe_state(harness.key)
    assert ("chat", "base") in observed.tombstones and "child" not in observed.active_messages


def test_persistent_broken_reopen_adapter_is_rejected_by_shared_oracle(tmp_path: Path) -> None:
    harness = PersistentHarness(tmp_path, "broken", BrokenReopenAdapter)
    harness.seed()
    before = harness.adapter.observe_state(harness.key)
    harness.adapter.close_and_reopen()
    after = harness.adapter.observe_state(harness.key)
    duplicate = ModelSnapshotBeginCommand(harness.trace[0]["command"] and UUID(harness.trace[0]["command"]["snapshot_id"]), 1, 1, 1, 0, 0)
    model_outcome = harness.model.begin_snapshot(harness.key, duplicate)
    production_outcome = harness.adapter.begin_snapshot(harness.key, duplicate)
    with pytest.raises(OracleMismatchError):
        assert_transition_oracle(model=harness.model, adapter=harness.adapter, key=harness.key, command=duplicate,
                                 model_outcome=model_outcome, prod_outcome=production_outcome,
                                 snapshot_before=before, snapshot_after=after, command_history=harness.trace)


class PersistentGeneralMachine(RuleBasedStateMachine):
    @initialize()
    def start(self) -> None:
        self.directory = TemporaryDirectory(prefix="persistent_ingestion-general-")
        self.harness = PersistentHarness(Path(self.directory.name), "tier-b-general")
        self.harness.seed()
        _record_metric("histories")

    def teardown(self) -> None:
        self.harness.adapter.close()
        self.directory.cleanup()

    @rule(value=st.integers(min_value=0, max_value=10000))
    def contiguous_or_duplicate_chat(self, value: int) -> None:
        command = ModelChatUpsertCommand(self.harness.new_id("event"), self.harness.next_seq(), f"chat-{value}-{self.harness.counter}", "full", f"fan-{value}", str(value), "2026-09-10T10:00:00+00:00")
        self.harness.frame(command, "commit_delta")
        self.harness.frame(command, "commit_delta")

    @rule()
    def reopen_cut_point(self) -> None:
        self.harness.reopen()

    @rule(value=st.integers(min_value=0, max_value=10000))
    def gap_then_recovery_after_reopen(self, value: int) -> None:
        gap = ModelChatUpsertCommand(self.harness.new_id("event"), self.harness.next_seq() + 1, f"gap-{value}", "full", f"fan-gap-{value}", "gap", "2026-09-10T10:00:00+00:00")
        assert self.harness.frame(gap, "commit_delta").disposition == "gap"
        self.harness.reopen()
        recovery = ModelChatUpsertCommand(self.harness.new_id("event"), self.harness.next_seq(), f"recovery-{value}", "full", f"fan-recovery-{value}", "recovery", "2026-09-10T10:00:00+00:00")
        assert self.harness.frame(recovery, "commit_delta").disposition == "accepted"

    @rule(value=st.integers(min_value=0, max_value=10000))
    def staged_snapshot_reopens_before_commit(self, value: int) -> None:
        key, snapshot = self.harness.stream(f"staged-{value}-{self.harness.counter}"), self.harness.new_id("staged")
        self.harness.frame(ModelSnapshotBeginCommand(snapshot, 1, 1, 1, 0, 0), "begin_snapshot", key=key)
        chunk = ModelSnapshotChunkCommand(snapshot, 0, "chat", [chat_record(f"staged-{value}")])
        self.harness.frame(chunk, "add_snapshot_chunk", key=key)
        self.harness.reopen()
        self.harness.frame(chunk, "add_snapshot_chunk", key=key)
        self.harness.frame(ModelSnapshotCommitCommand(snapshot, 1), "commit_snapshot", key=key)

    @rule(value=st.integers(min_value=0, max_value=10000))
    def accepted_message_delete_then_lost_ack_retry(self, value: int) -> None:
        chat_id, message_id = f"parent-{value}-{self.harness.counter}", f"message-{value}-{self.harness.counter}"
        self.harness.frame(ModelChatUpsertCommand(self.harness.new_id("event"), self.harness.next_seq(), chat_id, "full", f"fan-{chat_id}", chat_id, "2026-09-10T10:00:00+00:00"), "commit_delta")
        message = ModelMessageUpsertCommand(self.harness.new_id("event"), self.harness.next_seq(), message_id, chat_id, f"fan-{chat_id}", message_id, "2026-09-10T10:01:00+00:00", "inbound")
        self.harness.frame(message, "commit_delta")
        self.harness.reopen()
        self.harness.frame(message, "commit_delta")
        self.harness.frame(ModelMessageDeleteCommand(self.harness.new_id("event"), self.harness.next_seq(), message_id, chat_id), "commit_delta")


class PersistentDeletionMachine(RuleBasedStateMachine):
    @initialize()
    def start(self) -> None:
        self.directory = TemporaryDirectory(prefix="persistent_ingestion-deletion-")
        self.harness = PersistentHarness(Path(self.directory.name), "tier-b-deletion")
        self.harness.seed()
        self.message = ModelMessageUpsertCommand(self.harness.new_id("event"), 2, "deleted-message", "base", "fan-base", "deleted-message", "2026-09-10T10:01:00+00:00", "inbound")
        self.harness.frame(self.message, "commit_delta")
        self.message_delete = ModelMessageDeleteCommand(self.harness.new_id("event"), 3, "deleted-message", "base")
        self.harness.frame(self.message_delete, "commit_delta")
        self.delete = ModelChatDeleteCommand(self.harness.new_id("event"), 4, "base")
        self.harness.frame(self.delete, "commit_delta")
        _record_metric("histories")

    def teardown(self) -> None:
        self.harness.adapter.close()
        self.directory.cleanup()

    @rule()
    def reopen_then_retry_delete(self) -> None:
        self.harness.reopen()
        self.harness.frame(self.delete, "commit_delta")

    @rule()
    def stale_reappearance_after_reopen(self) -> None:
        self.harness.reopen()
        stale = ModelChatUpsertCommand(self.harness.new_id("event"), 5, "base", "full", "fan-base", "stale", "2026-09-10T10:00:00+00:00")
        self.harness.frame(stale, "commit_delta")

    @rule()
    def stale_message_reappearance_after_reopen(self) -> None:
        self.harness.reopen()
        stale = ModelMessageUpsertCommand(self.harness.new_id("event"), 5, "deleted-message", "base", "fan-base", "stale", "2026-09-10T10:01:00+00:00", "inbound")
        self.harness.frame(stale, "commit_delta")

    @rule()
    def fresh_stream_and_committed_snapshot_replay_after_reopen(self) -> None:
        key, snapshot = self.harness.stream(f"fresh-{self.harness.counter}"), self.harness.new_id("fresh-snapshot")
        begin = ModelSnapshotBeginCommand(snapshot, 1, 1, 1, 0, 0)
        chunk = ModelSnapshotChunkCommand(snapshot, 0, "chat", [chat_record("base")])
        commit = ModelSnapshotCommitCommand(snapshot, 1)
        self.harness.frame(begin, "begin_snapshot", key=key)
        self.harness.frame(chunk, "add_snapshot_chunk", key=key)
        self.harness.frame(commit, "commit_snapshot", key=key)
        self.harness.reopen()
        self.harness.frame(begin, "begin_snapshot", key=key)
        self.harness.frame(chunk, "add_snapshot_chunk", key=key)
        self.harness.frame(commit, "commit_snapshot", key=key)


@pytest.mark.stateful_tier_b
class TestPersistentGeneral(PersistentGeneralMachine.TestCase):
    pass


@pytest.mark.stateful_tier_b
class TestPersistentDeletion(PersistentDeletionMachine.TestCase):
    pass


@pytest.mark.windows_production
@pytest.mark.stateful_tier_b
class TestWindowsProductionPersistenceSmoke(PersistentGeneralMachine.TestCase):
    pass


def test_file_backed_runtime_smoke(tmp_path: Path) -> None:
    harness = PersistentHarness(tmp_path, "windows-smoke")
    harness.seed(); harness.reopen()
    assert harness.adapter.reopen_count == 2
    assert platform.system()
