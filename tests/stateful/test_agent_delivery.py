"""Semantic qualification for the production durable Agent delivery path."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from hypothesis import given, settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, rule

from tests.state_models.agent_adapter import AgentHarness
from tests.state_models.agent_delivery_model import AgentDeliveryModel, ModelError


NAMESPACE = UUID("11111111-1111-1111-1111-111111111111")
SUCCESSFUL_REPLAY_TRACE = Path("tests/fixtures/agent-delivery-successful-replay-trace.json")
MINIMIZED_FRAME_FAILURE = Path("tests/fixtures/agent-delivery-minimized-frame-failure.json")


def chat(number: int = 1, *, updated: int = 1, name: str | None = None, placeholder: bool = False) -> dict:
    return {"type": "chat.upsert", "chat": {"chat_id": f"chat-{number}", "record_kind": "placeholder" if placeholder else "full", "platform_user_id": None if placeholder else f"fan-{number}", "display_name": None if placeholder else name or f"Fan {number}", "updated_at": None if placeholder else f"2026-01-01T00:00:{updated:02d}Z"}}


def message(number: int = 1, *, chat_number: int | None = None, text: str | None = None) -> dict:
    return {"type": "message.upsert", "message": {"message_id": f"message-{number}", "chat_id": f"chat-{chat_number or number}", "sender_platform_user_id": f"fan-{chat_number or number}", "text": text or f"hi {number}", "sent_at": f"2026-01-01T00:01:{number:02d}Z", "direction": "inbound"}}


def chat_delete(number: int = 1) -> dict:
    return {"type": "chat.delete", "chat_id": f"chat-{number}"}


def message_delete(number: int = 1, *, chat_number: int | None = None) -> dict:
    return {"type": "message.delete", "message_id": f"message-{number}", "chat_id": f"chat-{chat_number or number}"}


def coverage(kind: str, number: int = 1) -> dict:
    evidence = {"type": kind, "generation_id": str(uuid5(NAMESPACE, "generation-1"))}
    if kind == "generation.started":
        evidence.update(as_of="2026-01-01T00:00:00Z", authorization_revision="auth-1")
    elif kind == "inventory.ended":
        evidence["observed_at"] = "2026-01-01T00:00:00Z"
    elif kind == "generation.closed":
        evidence["closed_at"] = "2026-01-01T00:00:00Z"
    elif kind == "conversation.history_started":
        evidence.update(conversation_id=f"chat-{number}", earliest_observed_at=None, observed_at="2026-01-01T00:00:00Z")
    elif kind == "conversation.head_reconciled":
        evidence.update(conversation_id=f"chat-{number}", reconciled_through="2026-01-01T00:00:00Z")
    elif kind == "inventory.member":
        evidence["conversation_id"] = f"chat-{number}"
    return {"type": "coverage.observed", "evidence": evidence}


def snapshot_id(number: int) -> str:
    return str(uuid5(NAMESPACE, f"snapshot-{number}"))


def event_id(number: int) -> str:
    return str(uuid5(NAMESPACE, f"event-{number}"))


def normalize_state(value: dict) -> dict:
    """Compare semantic state while excluding opaque production event IDs."""
    normalized = deepcopy(value)
    for item in normalized["outbox"]:
        item.pop("event_id", None)
    normalized["outbox"].sort(key=lambda item: item["source_seq"])
    normalized["chats"].sort(key=lambda item: item["chat_id"])
    normalized["messages"].sort(key=lambda item: item["message_id"])
    normalized["coverage"].sort(key=lambda item: item["evidence_key"])
    for manifest in normalized["manifests"]:
        # Exclude storage layout while preserving snapshot semantics.
        manifest.pop("scan_kind_index", None)
        manifest.pop("scan_after_key", None)
        manifest.pop("next_chunk_index", None)
    normalized["manifests"].sort(key=lambda item: item["snapshot_id"])
    for chunk in normalized["chunks"]:
        chunk.pop("key", None)
    normalized["chunks"].sort(key=lambda item: item["chunk_index"])
    for chunk in normalized["chunks"]:
        # Record order is not part of chunk semantics.
        chunk["records"].sort(key=lambda item: json.dumps(item, sort_keys=True))
    normalized["overrides"].sort(key=lambda item: item["key"])
    transport = normalized["transport"]
    transport.pop("frames", None)
    transport.pop("connection_id", None)
    transport.pop("scheduled_callbacks", None)
    return normalized


def normalized_frames(reply: dict) -> list[dict]:
    frames = deepcopy(reply["state"]["transport"].get("frames", []))
    for frame in frames:
        frame["payload"].pop("connection_id", None)
        frame["payload"].pop("fencing_token", None)
        frame["payload"].pop("creator_account_id", None)
        frame["payload"].pop("agent_installation_id", None)
        frame["payload"].pop("agent_stream_id", None)
        if frame["payload"].get("frame_kind") == "chunk":
            frame["payload"]["records"].sort(key=lambda item: json.dumps(item, sort_keys=True))
    return frames


def model_error_code(error: ModelError) -> str:
    detail = str(error)
    if "Unknown coverage" in detail: return "invalid_evidence"
    if "Coverage evidence" in detail: return "evidence_conflict"
    if "resurrected" in detail: return "tombstone_revive"
    if "conflict" in detail: return "material_conflict"
    if "platform identity" in detail: return "identity_conflict"
    if "Unsupported" in detail or "invalid" in detail: return "invalid_entity"
    return "invariant_failed"


def model_error_expectation(error: ModelError, *, client_ack: bool = False) -> tuple[str, str]:
    """The protocol contract fixes rejection code and its stable message."""
    message = str(error)
    if client_ack:
        message = f"AgentWebSocketClient rejected ACK: {message}"
    return model_error_code(error), message


def oracle(model: AgentDeliveryModel, reply: dict, *, before: dict, expected_result: object = ... , expected_error: tuple[str, str] | None = None, expected_frames: list[dict] | None = None) -> None:
    assert reply["ok"] is (expected_error is None), reply
    expected_state = normalize_state(model.state())
    assert normalize_state(reply["state"]) == expected_state
    if expected_frames is not None:
        assert normalized_frames(reply) == expected_frames
    if expected_error is not None:
        assert normalize_state(reply["state"]) == normalize_state(before)
        assert (reply["error"]["code"], reply["error"]["message"]) == expected_error
    elif expected_result is not ...:
        actual = deepcopy(reply["result"])
        if isinstance(actual, dict) and "event_id" in actual:
            actual.pop("event_id")
        expected = deepcopy(expected_result)
        if isinstance(expected, dict) and "event_id" in expected:
            expected.pop("event_id")
        if isinstance(expected, dict) and "committedSourceSeq" in expected:
            assert actual == expected
        elif isinstance(expected, dict) and "manifest" in expected:
            for value in (actual.get("manifest"), expected.get("manifest")):
                if value is not None:
                    value.pop("scan_kind_index", None)
                    value.pop("scan_after_key", None)
                    value.pop("next_chunk_index", None)
            for value in (actual.get("chunk"), expected.get("chunk")):
                if value is not None:
                    value.pop("key", None)
                    value["records"].sort(key=lambda item: json.dumps(item, sort_keys=True))
            assert actual == expected
        elif isinstance(expected, dict) and {"snapshot_id", "through_seq", "state"} <= expected.keys():
            for value in (actual, expected):
                value.pop("scan_kind_index", None)
                value.pop("scan_after_key", None)
                value.pop("next_chunk_index", None)
            assert actual == expected
        elif isinstance(expected, dict) and {"connected", "fence", "sync_required", "replay"} <= expected.keys():
            actual = {key: actual[key] for key in ("connected", "fence", "sync_required", "replay")}
            assert actual == expected
        else:
            assert actual == expected


@contextmanager
def harness():
    value = AgentHarness()
    try:
        yield value
    finally:
        value.close()


class Driver:
    """One per-example command driver: model first, production second, oracle always."""

    def __init__(self) -> None:
        self.model = AgentDeliveryModel()
        self.harness = AgentHarness()
        self.operation_number = 0
        self.expected_frames: list[dict] = []
        self.session_action = "resume"
        self._metrics_written = False
        self.metrics = {
            "histories_executed": 1,
            "driver_transitions": 0,
            "node_processes": 1,
            "reconnect_operations": 0,
            "restart_operations": 0,
            "snapshot_operations": 0,
            "deletion_operations": 0,
            "sync_required_operations": 0,
            "same_client_reconnect_operations": 0,
            "partial_snapshot_restart_operations": 0,
        }

    def close(self) -> None:
        self.harness.close()
        metrics_path = os.environ.get("AGENT_QUALIFICATION_METRICS_PATH")
        if metrics_path and not self._metrics_written:
            with Path(metrics_path).open("a", encoding="utf-8") as output:
                output.write(f"{json.dumps(self.metrics, sort_keys=True)}\n")
            self._metrics_written = True

    def _record_trace(self, requirements: list[str]) -> None:
        self.metrics["driver_transitions"] += 1
        item = self.harness.trace[-1]
        item["model"] = {"state": self.model.state()}
        item["requirements"] = requirements
        item["context"] = {
            "stream": self.model.agent_stream_id,
            "session": {"connected": self.model.connected, "fence": self.model.fence},
        }

    def _frames(self, reply: dict) -> None:
        oracle(self.model, reply, before=self.model.state(), expected_frames=self.expected_frames)

    def _delta_frames(self, committed: int | None = None) -> list[dict]:
        checkpoint = self.model.acknowledged_source_seq if committed is None else committed
        return [{"type": "ingest.delta", "payload": {"event_id": item["event_id"], "source_seq": item["source_seq"], "acquisition_origin": item["acquisition_origin"], "change": item["change"]}} for item in self.model.outbox if item["source_seq"] > checkpoint]

    def _snapshot_begin(self) -> dict:
        assert self.model.pending_snapshot is not None
        manifest = self.model.manifests[self.model.pending_snapshot["snapshot_id"]]
        return {"type": "ingest.snapshot", "payload": {"frame_kind": "begin", "snapshot_id": manifest["snapshot_id"], "through_seq": manifest["through_seq"], "chunk_count": manifest["chunk_count"], "record_counts": manifest["record_counts"], "max_frame_bytes": 524288}}

    def capture(self, change: dict, *, origin: str = "passive") -> dict:
        if change.get("type") in {"chat.delete", "message.delete"}:
            self.metrics["deletion_operations"] += 1
        self.operation_number += 1
        before = self.model.state()
        try:
            expected = self.model.capture(change, event_id(self.operation_number), origin)
        except ModelError as error:
            reply = self.harness.command("capture", change=change, event_id=event_id(self.operation_number), origin=origin)
            oracle(self.model, reply, before=before, expected_error=model_error_expectation(error), expected_frames=self.expected_frames)
        else:
            reply = self.harness.command("capture", change=change, event_id=event_id(self.operation_number), origin=origin)
            oracle(self.model, reply, before=before, expected_result=expected, expected_frames=self.expected_frames)
        self._record_trace(["AG01", "AG02"])
        return reply

    def parent_message(self, message_change: dict, parent_change: dict) -> dict:
        self.operation_number += 1
        before = self.model.state()
        try:
            expected = self.model.capture_message_parent(message_change, parent_change, event_id(self.operation_number * 2), event_id(self.operation_number * 2 + 1))
        except ModelError as error:
            reply = self.harness.command("capture_message_parent", message=message_change, parent=parent_change, event_ids=[event_id(self.operation_number * 2), event_id(self.operation_number * 2 + 1)])
            oracle(self.model, reply, before=before, expected_error=model_error_expectation(error), expected_frames=self.expected_frames)
        else:
            reply = self.harness.command("capture_message_parent", message=message_change, parent=parent_change, event_ids=[event_id(self.operation_number * 2), event_id(self.operation_number * 2 + 1)])
            oracle(self.model, reply, before=before, expected_result=expected, expected_frames=self.expected_frames)
        self._record_trace(["AG03"])
        return reply

    def acknowledge(self, committed: int, *, snapshot: dict | None = None) -> dict:
        if snapshot is not None:
            self.metrics["snapshot_operations"] += 1
        before = self.model.state(); snapshot_id_value = snapshot["snapshot_id"] if snapshot else None
        try:
            expected = self.model.acknowledge(committed, snapshot_id_value, snapshot)
        except ModelError as error:
            reply = self.harness.command("ack", committed_source_seq=committed, snapshot_id=snapshot_id_value, snapshot_progress=snapshot)
            oracle(self.model, reply, before=before, expected_error=model_error_expectation(error, client_ack=self.model.connected), expected_frames=self.expected_frames)
        else:
            reply = self.harness.command("ack", committed_source_seq=committed, snapshot_id=snapshot_id_value, snapshot_progress=snapshot)
            # The real websocket ACK wrapper exposes committedSourceSeq; its durable
            # state above includes the snapshot acknowledgement/cleanup outcome.
            if snapshot is not None and not snapshot["committed"]:
                manifest = self.model.manifests[snapshot["snapshot_id"]]
                index = snapshot["next_expected_chunk_index"]
                if index < manifest["chunk_count"]:
                    chunk = next(chunk for chunk in self.model.chunks if chunk["chunk_index"] == index)
                    payload = {key: value for key, value in chunk.items() if key != "key"} | {"frame_kind": "chunk"}
                    payload["records"].sort(key=lambda item: json.dumps(item, sort_keys=True))
                else:
                    payload = {"frame_kind": "commit", "snapshot_id": snapshot["snapshot_id"], "chunk_count": manifest["chunk_count"]}
                self.expected_frames.append({"type": "ingest.snapshot", "payload": payload})
            oracle(self.model, reply, before=before, expected_result=expected, expected_frames=self.expected_frames)
            if snapshot is not None and not snapshot["committed"]:
                frame = normalized_frames(reply)[-1]
                manifest = self.model.manifests[snapshot["snapshot_id"]]
                expected_index = snapshot["next_expected_chunk_index"]
                assert frame["type"] == "ingest.snapshot"
                if expected_index < manifest["chunk_count"]:
                    expected_chunk = next(chunk for chunk in self.model.chunks if chunk["chunk_index"] == expected_index)
                    expected_payload = {key: value for key, value in expected_chunk.items() if key != "key"} | {"frame_kind": "chunk"}
                    expected_payload["records"].sort(key=lambda item: json.dumps(item, sort_keys=True))
                    assert frame["payload"] == expected_payload
                else:
                    assert frame["payload"] == {"frame_kind": "commit", "snapshot_id": snapshot["snapshot_id"], "chunk_count": manifest["chunk_count"]}
        self._record_trace(["AG04", "AG07"])
        return reply

    def reject_invalid_snapshot_ack(self) -> dict:
        """A wrong snapshot identity is rejected through the live client path."""
        assert self.model.pending_snapshot is not None
        before = self.model.state()
        wrong = snapshot_id(self.operation_number + 10_000)
        progress = {"snapshot_id": wrong, "next_expected_chunk_index": 0, "committed": False}
        with pytest.raises(ModelError) as failure:
            self.model.acknowledge(self.model.last_source_seq, wrong, progress)
        reply = self.harness.command("ack", committed_source_seq=self.model.last_source_seq, snapshot_id=wrong, snapshot_progress=progress)
        oracle(self.model, reply, before=before, expected_error=model_error_expectation(failure.value, client_ack=True), expected_frames=self.expected_frames)
        self._record_trace(["AG04", "AG07"])
        return reply

    def create_snapshot(self) -> dict:
        self.metrics["snapshot_operations"] += 1
        self.operation_number += 1; before = self.model.state(); identifier = snapshot_id(self.operation_number)
        expected = self.model.create_snapshot(identifier)
        reply = self.harness.command("snapshot_create", snapshot_id=identifier)
        oracle(self.model, reply, before=before, expected_result=expected, expected_frames=self.expected_frames)
        self._record_trace(["AG05", "AG06"])
        return reply

    def build_snapshot(self) -> dict:
        self.metrics["snapshot_operations"] += 1
        before = self.model.state(); expected = self.model.build_next_snapshot_chunk()
        reply = self.harness.command("snapshot_build")
        oracle(self.model, reply, before=before, expected_result=expected, expected_frames=self.expected_frames)
        self._record_trace(["AG05", "AG06"])
        return reply

    def restart(self) -> dict:
        self.metrics["restart_operations"] += 1
        before = self.model.state(); self.model.connected = False; self.model.fence = None; self.model.sync_required = False; self.model.replay = []
        reply = self.harness.command("restart")
        self.expected_frames = []
        oracle(self.model, reply, before=before, expected_result=self.model.identity_result(), expected_frames=self.expected_frames)
        self._record_trace(["AG02", "AG08"])
        return reply

    def connect(self, fence: str, *, committed: int | None = None, resume_action: str = "resume", pending_snapshot_id: str | None = None, next_expected_chunk_index: int = 0) -> dict:
        self.metrics["reconnect_operations"] += 1
        before = self.model.state(); self.model.connect(fence, committed, resume_action)
        self.session_action = resume_action
        reply = self.harness.command("connect", fence=fence, committed_source_seq=committed, resume_action=resume_action, pending_snapshot_id=pending_snapshot_id, next_expected_chunk_index=next_expected_chunk_index)
        if resume_action == "resume":
            self.expected_frames = self._delta_frames(committed)
        elif pending_snapshot_id is None:
            self.expected_frames = [self._snapshot_begin()]
        else:
            manifest = self.model.manifests[pending_snapshot_id]
            if next_expected_chunk_index < manifest["chunk_count"]:
                chunk = next(chunk for chunk in self.model.chunks if chunk["chunk_index"] == next_expected_chunk_index)
                payload = {key: value for key, value in chunk.items() if key != "key"} | {"frame_kind": "chunk"}
                payload["records"].sort(key=lambda item: json.dumps(item, sort_keys=True))
            else:
                payload = {"frame_kind": "commit", "snapshot_id": pending_snapshot_id, "chunk_count": manifest["chunk_count"]}
            self.expected_frames = [{"type": "ingest.snapshot", "payload": payload}]
        oracle(self.model, reply, before=before, expected_result=self.model.transport_result(), expected_frames=self.expected_frames)
        frames = self.expected_frames
        if resume_action == "resume":
            expected = [{"type": "ingest.delta", "payload": {"event_id": item["event_id"], "source_seq": item["source_seq"], "acquisition_origin": item["acquisition_origin"], "change": item["change"]}} for item in self.model.outbox if item["source_seq"] > (committed if committed is not None else self.model.acknowledged_source_seq)]
            assert frames == expected
        else:
            assert frames == self.expected_frames
        self._record_trace(["AG04", "AG08"])
        return reply

    def same_client_reconnect(self, fence: str, *, committed: int | None = None) -> dict:
        self.metrics["reconnect_operations"] += 1
        self.metrics["same_client_reconnect_operations"] += 1
        before = self.model.state(); self.model.connect(fence, committed)
        self.session_action = "resume"
        reply = self.harness.command("same_client_reconnect", fence=fence, committed_source_seq=committed)
        self.expected_frames = self._delta_frames(committed)
        oracle(self.model, reply, before=before, expected_result=self.model.transport_result(), expected_frames=self.expected_frames)
        self._record_trace(["AG08"])
        return reply

    def sync_required(self) -> dict:
        self.metrics["sync_required_operations"] += 1
        before = self.model.state()
        self.model.require_sync()
        pending = self.model.pending_snapshot
        assert pending is not None
        manifest = self.model.manifests[pending["snapshot_id"]]
        index = pending["next_expected_chunk_index"]
        if index < manifest["chunk_count"]:
            chunk = next(chunk for chunk in self.model.chunks if chunk["chunk_index"] == index)
            payload = {key: value for key, value in chunk.items() if key != "key"} | {"frame_kind": "chunk"}
            payload["records"].sort(key=lambda item: json.dumps(item, sort_keys=True))
        else:
            payload = {"frame_kind": "commit", "snapshot_id": pending["snapshot_id"], "chunk_count": manifest["chunk_count"]}
        self.expected_frames.append({"type": "ingest.snapshot", "payload": payload})
        reply = self.harness.command("sync_required", pending_snapshot_id=pending["snapshot_id"], next_expected_chunk_index=pending["next_expected_chunk_index"])
        oracle(self.model, reply, before=before, expected_result=self.model.transport_result(), expected_frames=self.expected_frames)
        self._record_trace(["AG07"])
        return reply

    def partial_snapshot_restart(self, prefix: str) -> None:
        """Resume a nonzero multi-chunk upload after durable reconstruction."""
        self.metrics["partial_snapshot_restart_operations"] += 1
        if self.model.pending_snapshot is None:
            self.capture(chat(20)); self.parent_message(message(20), chat(20, placeholder=True))
            build_ready_snapshot(self)
        pending = self.model.pending_snapshot
        assert pending is not None and pending["state"] == "ready"
        identifier = pending["snapshot_id"]
        manifest = self.model.manifests[identifier]
        assert manifest["chunk_count"] >= 2
        self.connect(f"{prefix}-begin", resume_action="snapshot_required")
        self.acknowledge(self.model.last_source_seq, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": 0, "committed": False})
        self.restart()
        self.connect(f"{prefix}-resume", resume_action="snapshot_required", pending_snapshot_id=identifier, next_expected_chunk_index=1)
        for index in range(1, manifest["chunk_count"]):
            self.acknowledge(self.model.last_source_seq, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": index, "committed": False})
        self.acknowledge(self.model.last_source_seq, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": manifest["chunk_count"], "committed": False})
        self.acknowledge(self.model.last_source_seq, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": manifest["chunk_count"], "committed": True})

    def disconnect(self) -> dict:
        before = self.model.state(); self.model.connected = False
        reply = self.harness.command("disconnect")
        actual = {key: reply["result"][key] for key in ("connected", "fence", "sync_required", "replay")}
        assert actual == self.model.transport_result()
        oracle(self.model, reply, before=before, expected_result=..., expected_frames=self.expected_frames)
        self._record_trace(["AG08"])
        return reply


def build_ready_snapshot(driver: Driver) -> str:
    created = driver.create_snapshot(); identifier = created["result"]["snapshot_id"]
    while driver.model.pending_snapshot and driver.model.pending_snapshot["state"] != "ready":
        driver.build_snapshot()
    return identifier


def test_ag01_to_ag04_capture_noop_atomic_ack_and_restart_replay() -> None:
    with harness() as raw:
        # Explicitly demonstrate that deterministic command traces include complete
        # command, session/stream, result/error, before/model/after data.
        driver = Driver(); driver.harness.close(); driver.harness = raw
        first = driver.capture(chat(), origin="passive")
        duplicate = driver.capture(chat(), origin="passive")
        assert duplicate["result"] is None and first["state"]["identity"]["last_source_seq"] == 1
        driver.parent_message(message(), chat(1, placeholder=True))
        connected = driver.connect("fence-a")
        assert connected["state"]["transport"]["replay"] == [1, 2]
        ack = driver.acknowledge(2)
        assert ack["state"]["identity"]["outbox_count"] == 0
        driver.restart(); replay = driver.connect("fence-b")
        assert replay["state"]["transport"]["replay"] == []
        trace = deepcopy(raw.trace)
    with harness() as replay:
        replies = replay.replay(trace)
        assert replies[-1]["state"]["identity"]["acknowledged_source_seq"] == 2
        assert all({"request_id", "command", "before", "after", "result", "error", "model", "context", "requirements"} <= item.keys() for item in trace)


def test_persisted_successful_agent_trace_replays_every_transition() -> None:
    """A checked-in successful regression trace must satisfy every transition."""
    trace = json.loads(SUCCESSFUL_REPLAY_TRACE.read_text(encoding="utf-8"))
    with harness() as replay:
        replies = replay.replay(trace)
    assert len(replies) == len(trace) == 7


def test_persisted_minimized_frame_failure_is_rejected_by_shared_oracle() -> None:
    record = json.loads(MINIMIZED_FRAME_FAILURE.read_text(encoding="utf-8"))
    assert {"commands", "stream_session", "expected_frames", "actual_frames", "expected_state", "actual_state"} <= record.keys()
    driver = Driver()
    try:
        reply: dict | None = None
        for command in record["commands"]:
            if command["operation"] == "capture":
                reply = driver.capture(command["change"], origin=command["origin"])
            elif command["operation"] == "connect":
                reply = driver.connect(command["fence"], committed=command["committed_source_seq"], resume_action=command["resume_action"])
            else:
                raise AssertionError(f"unsupported minimized failure command: {command['operation']}")
        assert reply is not None
        assert driver.harness.trace[-1]["context"] == record["stream_session"]
        assert driver.expected_frames == record["expected_frames"]
        assert normalize_state(driver.model.state()) == normalize_state(record["expected_state"])
        corrupted = deepcopy(reply)
        corrupted["state"]["transport"]["frames"] = record["actual_frames"]
        assert normalized_frames(corrupted) == normalized_frames({"state": record["actual_state"]})
        assert normalize_state(corrupted["state"]) == normalize_state(record["actual_state"])
        with pytest.raises(AssertionError):
            oracle(driver.model, corrupted, before=driver.model.state(), expected_frames=record["expected_frames"])
    finally:
        driver.close()


def test_ag05_ag06_snapshot_zero_small_multi_kind_live_override_and_cleanup() -> None:
    driver = Driver()
    try:
        # Zero snapshot creates an exact zero-chunk manifest.
        zero = build_ready_snapshot(driver)
        assert driver.harness.command("read")["state"]["manifests"][0]["chunk_count"] == 0
        driver.acknowledge(0, snapshot={"snapshot_id": zero, "next_expected_chunk_index": 0, "committed": True})
        driver.capture(chat(1, updated=1)); driver.parent_message(message(1), chat(1, placeholder=True))
        for kind in ("generation.started", "inventory.member", "inventory.ended", "conversation.history_started", "conversation.head_reconciled", "generation.closed"):
            driver.capture(coverage(kind))
        identifier = driver.create_snapshot()["result"]["snapshot_id"]
        # A newer live update must be represented by a preserved old snapshot record.
        driver.capture(chat(1, updated=2, name="Changed"))
        while driver.model.pending_snapshot and driver.model.pending_snapshot["state"] != "ready": driver.build_snapshot()
        state = driver.harness.command("read")["state"]
        assert state["manifests"][0]["snapshot_id"] == identifier
        assert {chunk["entity_kind"] for chunk in state["chunks"]} == {"chat", "message", "coverage_evidence"}
        frames = driver.connect("fence-snapshot", resume_action="snapshot_required", pending_snapshot_id=None)
        sent = normalized_frames(frames)
        assert sent[0]["type"] == "ingest.snapshot" and sent[0]["payload"]["frame_kind"] == "begin"
        # AG07: ACK progress drives real client chunk/commit sending, and final ACK cleans staging.
        count = state["manifests"][0]["chunk_count"]
        for index in range(count):
            driver.acknowledge(driver.model.last_source_seq, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": index, "committed": False})
        driver.acknowledge(driver.model.last_source_seq, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": count, "committed": True})
        assert driver.harness.command("read")["state"]["chunks"] == []
    finally:
        driver.close()


def test_ag07_sync_required_receive_path_and_ag08_reconnect_fence() -> None:
    driver = Driver()
    try:
        driver.capture(chat()); build_ready_snapshot(driver); driver.connect("fence-1")
        reply = driver.sync_required()
        assert normalized_frames(reply)[-1]["payload"]["frame_kind"] == "chunk"
        # This is the same live AgentWebSocketClient: close schedules a callback,
        # the controllable scheduler runs it, then the new fenced session replays.
        reconnect = driver.same_client_reconnect("fence-2", committed=0)
        assert reconnect["state"]["transport"]["fence"] == "fence-2"
        assert reconnect["state"]["transport"]["scheduled_callbacks"] == 0
    finally:
        driver.close()


def test_sync_required_after_snapshot_begin_emits_a_new_requested_chunk() -> None:
    driver = Driver()
    try:
        driver.capture(chat()); identifier = build_ready_snapshot(driver)
        begun = driver.connect("fence-sync-begin", resume_action="snapshot_required")
        before = normalized_frames(begun)
        reply = driver.sync_required()
        after = normalized_frames(reply)
        assert len(after) == len(before) + 1
        assert after[-1]["payload"]["frame_kind"] == "chunk"
        assert after[-1]["payload"]["snapshot_id"] == identifier
    finally:
        driver.close()


def test_partial_multichunk_snapshot_restarts_then_resumes_remaining_frames() -> None:
    driver = Driver()
    try:
        driver.partial_snapshot_restart("fence-partial")
        assert driver.model.pending_snapshot is None
    finally:
        driver.close()


def test_all_coverage_kinds_and_rejected_material_are_total_nonmutations() -> None:
    driver = Driver()
    try:
        for kind in ("generation.started", "inventory.member", "inventory.ended", "conversation.history_started", "conversation.head_reconciled", "generation.closed"):
            driver.capture(coverage(kind))
        before = driver.model.state()
        driver.capture({"type": "coverage.observed", "evidence": {"type": "generation.started", "generation_id": str(uuid5(NAMESPACE, "generation-1")), "unexpected": True}})
        assert driver.model.state() == before
        driver.capture(chat(1)); before = driver.model.state()
        driver.capture(chat(1, updated=1, name="conflict"))
        assert driver.model.state() == before
    finally:
        driver.close()


def test_snapshot_bounded_oracle_handles_zero_small_101_and_oversize_records() -> None:
    """AG05/AG06 bounds come from the published frame contract, not model internals."""
    with harness() as node:
        empty = node.command("snapshot_prepare", snapshot_id=snapshot_id(500))
        assert empty["result"]["chunk_count"] == 0
        node.command("ack", committed_source_seq=0, snapshot_id=snapshot_id(500), snapshot_progress={"snapshot_id": snapshot_id(500), "next_expected_chunk_index": 0, "committed": True})
        for number in range(101):
            reply = node.command("capture", change=chat(number + 10), event_id=event_id(10_000 + number))
            assert reply["ok"]
        ready = node.command("snapshot_prepare", snapshot_id=snapshot_id(501))
        chunks = ready["state"]["chunks"]
        chat_chunks = [chunk for chunk in chunks if chunk["entity_kind"] == "chat"]
        assert len(chat_chunks) == 2
        assert sum(len(chunk["records"]) for chunk in chat_chunks) == 101
        assert all(0 < len(chunk["records"]) <= 100 for chunk in chunks)
        assert all(len(json.dumps({"records": chunk["records"]}).encode("utf-8")) <= 524_288 for chunk in chunks)
    with harness() as node:
        # This size crosses the packing target without exceeding the frame limit.
        for number in range(51):
            reply = node.command("capture", change=message(number + 200, text="x" * 9_000), event_id=event_id(30_000 + number))
            assert reply["ok"]
        ready = node.command("snapshot_prepare", snapshot_id=snapshot_id(503))
        message_chunks = [chunk for chunk in ready["state"]["chunks"] if chunk["entity_kind"] == "message"]
        assert len(message_chunks) == 2
        assert all(len(json.dumps({"records": chunk["records"]}, separators=(",", ":")).encode("utf-8")) <= 458_752 for chunk in message_chunks)
    with harness() as node:
        oversized = message(99, text="x" * 393_217)
        assert node.command("capture", change=oversized, event_id=event_id(20_000))["ok"]
        failed = node.command("snapshot_prepare", snapshot_id=snapshot_id(502))
        assert not failed["ok"] and failed["error"]["code"] == "snapshot_record_oversize"
        assert failed["state"]["chunks"] == []


class BrokenNoopAllocatesSequence:
    def command(self, reply: dict) -> dict:
        bad = deepcopy(reply); bad["state"]["identity"]["last_source_seq"] += 1; return bad


class BrokenAckOverTrimsSuffix:
    def command(self, reply: dict) -> dict:
        bad = deepcopy(reply); bad["state"]["outbox"] = []; bad["state"]["identity"]["outbox_count"] = 0; return bad


class BrokenRestartLosesPrefix:
    def command(self, reply: dict) -> dict:
        bad = deepcopy(reply); bad["state"]["identity"]["acknowledged_source_seq"] = 0; return bad


class BrokenReplayOrderReversed:
    def command(self, reply: dict) -> dict:
        bad = deepcopy(reply); bad["state"]["transport"]["replay"].reverse(); return bad


class BrokenParentMessageAtomicity:
    def command(self, reply: dict) -> dict:
        bad = deepcopy(reply); bad["state"]["messages"] = []; return bad


class BrokenDeletionClosure:
    def command(self, reply: dict) -> dict:
        bad = deepcopy(reply)
        bad["state"]["chats"] = [{"chat_id": "chat-9", "tombstone": False}]
        return bad


class BrokenReplayFramePayload:
    def command(self, reply: dict) -> dict:
        bad = deepcopy(reply)
        bad["state"]["transport"]["frames"][0]["payload"]["source_seq"] = 999
        return bad


class BrokenUnexpectedSyncFrame:
    def command(self, reply: dict) -> dict:
        bad = deepcopy(reply)
        bad["state"]["transport"]["frames"].insert(0, {"type": "ingest.delta", "payload": {"source_seq": 999, "event_id": "corrupt", "acquisition_origin": "passive", "change": {"type": "corrupt"}}})
        return bad


def test_named_agent_semantic_falsifiers_fail_shared_oracle() -> None:
    driver = Driver()
    try:
        driver.capture(chat())
        duplicate = driver.capture(chat())
        with pytest.raises(AssertionError):
            oracle(driver.model, BrokenNoopAllocatesSequence().command(duplicate), before=driver.model.state())

        driver.capture(chat(2)); driver.capture(chat(3)); driver.connect("fence-a")
        acknowledgement = driver.acknowledge(1)
        with pytest.raises(AssertionError):
            oracle(driver.model, BrokenAckOverTrimsSuffix().command(acknowledgement), before=driver.model.state())

        restarted = driver.restart()
        with pytest.raises(AssertionError):
            oracle(driver.model, BrokenRestartLosesPrefix().command(restarted), before=driver.model.state())

        replay = driver.connect("fence-b")
        with pytest.raises(AssertionError):
            oracle(driver.model, BrokenReplayOrderReversed().command(replay), before=driver.model.state())

        parent_message = driver.parent_message(message(3), chat(3, placeholder=True))
        with pytest.raises(AssertionError):
            oracle(driver.model, BrokenParentMessageAtomicity().command(parent_message), before=driver.model.state())

        deleted = driver.capture(chat_delete(3))
        with pytest.raises(AssertionError):
            oracle(driver.model, BrokenDeletionClosure().command(deleted), before=driver.model.state())

        replay = driver.connect("fence-frame", committed=0)
        with pytest.raises(AssertionError):
            oracle(driver.model, BrokenReplayFramePayload().command(replay), before=driver.model.state(), expected_frames=driver.expected_frames)

        snapshot = build_ready_snapshot(driver)
        snapshot_reply = driver.connect("fence-frame-snapshot", resume_action="snapshot_required")
        with pytest.raises(AssertionError):
            oracle(driver.model, BrokenUnexpectedSyncFrame().command(snapshot_reply), before=driver.model.state(), expected_frames=driver.expected_frames)
    finally:
        driver.close()


@pytest.mark.skipif(os.environ.get("AGENT_FALSIFIER_PROBE") != "1", reason="run only by local evidence collection")
@settings(max_examples=4, deadline=None)
@given(number=st.integers(min_value=1, max_value=3))
def test_agent_falsifier_probe_shrinks_a_real_oracle_fault(number: int) -> None:
    """Intentionally fails under the evidence tool to prove oracle detection/shrinking."""
    driver = Driver()
    try:
        driver.capture(chat(number))
        duplicate = driver.capture(chat(number))
        oracle(driver.model, BrokenNoopAllocatesSequence().command(duplicate), before=driver.model.state())
    finally:
        driver.close()


def test_agent_model_is_independent_of_production_helpers() -> None:
    import ast
    source = Path("tests/state_models/agent_delivery_model.py").read_text(encoding="utf-8")
    imports = [node.module or "" for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
    assert all(not module.startswith(("extension", "app")) for module in imports)
    # Reject production storage details from the independent model.
    forbidden = (
        "scan_kind_index", "scan_after_key", "snapshotChunkKey", "SNAPSHOT_TARGET_BYTES",
        "SNAPSHOT_MAX_RECORD_BYTES", "SNAPSHOT_MAX_RECORDS", "mergeChat", "mergeMessage",
        "entriesPage", "getPage(",
    )
    assert not [token for token in forbidden if token in source]


@pytest.mark.stateful_agent_tier_a
class General(RuleBasedStateMachine):
    @initialize()
    def init(self) -> None:
        self.driver = Driver(); self.number = 0
        # Every generated general history crosses the real AG05→AG07 path,
        # including its zero-record boundary, before it explores additional
        # interleavings below.
        identifier = build_ready_snapshot(self.driver)
        count = self.driver.model.manifests[identifier]["chunk_count"]
        self.driver.connect("fence-generated-zero", resume_action="snapshot_required")
        self.driver.acknowledge(0, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": count, "committed": True})
        self.driver.disconnect(); self.driver.restart(); self.driver.connect("fence-generated-resume")
        # Both generated profiles exercise an inbound sync.required followed by
        # the scheduler-driven reconnect of that same live client.
        self.driver.capture(chat(9)); build_ready_snapshot(self.driver)
        self.driver.connect("fence-generated-sync", resume_action="snapshot_required")
        self.driver.sync_required()
        self.driver.same_client_reconnect("fence-generated-sync-next", committed=0)

    def teardown(self) -> None:
        self.driver.close()

    @rule(number=st.integers(min_value=1, max_value=3))
    def capture_chat_and_noop(self, number: int) -> None:
        self.driver.capture(chat(number)); self.driver.capture(chat(number))

    @rule(number=st.integers(min_value=1, max_value=3))
    def dependency_closed_message(self, number: int) -> None:
        self.driver.parent_message(message(number), chat(number, placeholder=True))

    @rule(kind=st.sampled_from(("generation.started", "inventory.member", "inventory.ended", "conversation.history_started", "conversation.head_reconciled", "generation.closed")))
    def coverage_progression(self, kind: str) -> None:
        self.driver.capture(coverage(kind))

    @rule()
    def meaningful_ack_or_lost_ack_retry(self) -> None:
        if self.driver.model.last_source_seq and self.driver.model.pending_snapshot is None:
            checkpoint = max(self.driver.model.acknowledged_source_seq, self.driver.model.last_source_seq - 1)
            self.driver.connect(f"fence-ack-{self.driver.operation_number}")
            self.driver.acknowledge(checkpoint)
            self.driver.acknowledge(checkpoint)

    @rule()
    def snapshot_live_override_and_resume(self) -> None:
        if self.driver.model.pending_snapshot is None:
            self.driver.create_snapshot()
        if self.driver.model.pending_snapshot and self.driver.model.pending_snapshot["state"] == "building":
            self.driver.capture(chat(1, updated=2, name="Live")); self.driver.build_snapshot()

    @rule()
    def snapshot_transport_progress_and_cleanup(self) -> None:
        """Drive AG05–AG07 all the way through real client framing and ACKs."""
        pending = self.driver.model.pending_snapshot
        if pending is None:
            self.driver.create_snapshot()
            pending = self.driver.model.pending_snapshot
        while pending is not None and pending["state"] != "ready":
            self.driver.build_snapshot(); pending = self.driver.model.pending_snapshot
        if pending is None:
            return
        identifier = pending["snapshot_id"]
        manifest = self.driver.model.manifests[identifier]
        self.driver.connect(f"fence-snapshot-{self.driver.operation_number}", resume_action="snapshot_required")
        for index in range(manifest["chunk_count"]):
            self.driver.acknowledge(self.driver.model.last_source_seq, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": index, "committed": False})
        self.driver.acknowledge(self.driver.model.last_source_seq, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": manifest["chunk_count"], "committed": True})
        self.driver.disconnect(); self.driver.restart(); self.driver.connect(f"fence-resume-{self.driver.operation_number}")

    @rule()
    def rejected_commands_are_total_nonmutations(self) -> None:
        self.driver.capture({"type": "coverage.observed", "evidence": {"type": "unknown", "generation_id": str(uuid5(NAMESPACE, "unknown"))}})
        self.driver.capture(coverage("generation.started"))
        self.driver.capture({"type": "coverage.observed", "evidence": {"type": "generation.started", "generation_id": str(uuid5(NAMESPACE, "generation-1")), "conflict": True}})
        self.driver.parent_message(message(1), chat(2, placeholder=True))
        self.driver.acknowledge(self.driver.model.last_source_seq + 1)
        if self.driver.model.pending_snapshot is not None and self.driver.model.pending_snapshot["state"] == "ready":
            self.driver.connect(f"fence-invalid-snapshot-{self.driver.operation_number}", resume_action="snapshot_required")
            self.driver.reject_invalid_snapshot_ack()

    @rule()
    def reconstruct_disconnect_reconnect(self) -> None:
        if self.driver.model.pending_snapshot is None:
            self.driver.disconnect(); self.driver.restart(); self.driver.connect(f"fence-r-{self.driver.operation_number}")


@pytest.mark.stateful_agent_tier_a
class Deletion(General):
    @initialize()
    def init(self) -> None:
        self.driver = Driver(); self.number = 0
        self.driver.parent_message(message(), chat(1, placeholder=True)); self.driver.capture(chat_delete()); self.driver.capture(message_delete())
        # Guaranteed deletion adversary: snapshot/transport cleanup, storage
        # reconstruction, new-fence reconnect, then stale resurrection/retry.
        identifier = build_ready_snapshot(self.driver)
        count = self.driver.model.manifests[identifier]["chunk_count"]
        self.driver.connect("fence-delete-snapshot", resume_action="snapshot_required")
        for index in range(count):
            self.driver.acknowledge(self.driver.model.last_source_seq, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": index, "committed": False})
        self.driver.acknowledge(self.driver.model.last_source_seq, snapshot={"snapshot_id": identifier, "next_expected_chunk_index": count, "committed": True})
        self.driver.disconnect(); self.driver.restart(); self.driver.connect("fence-delete-replay")
        self.driver.capture(chat()); self.driver.capture(message()); self.driver.capture(chat_delete()); self.driver.capture(message_delete())
        self.driver.partial_snapshot_restart("fence-delete-partial")
        identifier = build_ready_snapshot(self.driver)
        self.driver.connect("fence-delete-sync", resume_action="snapshot_required")
        self.driver.sync_required()
        self.driver.same_client_reconnect("fence-delete-sync-next", committed=0)

    @rule()
    def stale_reappearance_and_delete_retry(self) -> None:
        self.driver.capture(chat()); self.driver.capture(message()); self.driver.capture(chat_delete()); self.driver.capture(message_delete())


settings.register_profile("agent_tier_a_general", max_examples=5, stateful_step_count=10, deadline=None)
settings.register_profile("agent_tier_a_deletion", max_examples=4, stateful_step_count=10, deadline=None)
settings.register_profile("dev", max_examples=3, stateful_step_count=6, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
TestAgentDeliveryGeneral = General.TestCase
TestAgentDeliveryDeletion = Deletion.TestCase
