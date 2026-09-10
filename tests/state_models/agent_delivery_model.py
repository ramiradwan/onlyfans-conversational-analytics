"""Independent conceptual model for Agent durable delivery.

It predicts durable observable state but never imports Agent JavaScript, calls a
production merge helper, or opens IndexedDB. The persistent Node harness is
the implementation under qualification.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


class ModelError(ValueError):
    """A rejected local command (the harness calls it invariant_failed)."""


def clone(value: Any) -> Any:
    return deepcopy(value)


def canonical(value: Any) -> Any:
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in sorted(value.items()) if key not in {"origin", "observed_at", "source_seq", "last_source_seq", "last_origin"}}
    return value


def material_equal(left: Any, right: Any) -> bool:
    return canonical(left) == canonical(right)


def evidence_key(evidence: dict[str, Any]) -> str:
    generation = evidence.get("generation_id")
    if not isinstance(generation, str) or not generation:
        raise ModelError("Coverage generation_id is required")
    kind = evidence.get("type")
    fixed = {"generation.started": "00:started", "inventory.ended": "20:inventory-ended", "generation.closed": "50:closed"}
    if kind in fixed:
        return f"{generation}:{fixed[kind]}"
    conversation_id = evidence.get("conversation_id")
    variable = {"inventory.member": "10:member", "conversation.history_started": "30:history", "conversation.head_reconciled": "40:head"}
    if kind not in variable:
        raise ModelError("Unknown coverage evidence type")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ModelError("Coverage conversation_id is required")
    return f"{generation}:{variable[kind]}:{conversation_id}"


def decide_chat_update(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    chat_id = incoming.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        raise ModelError("Chat requires chat_id")
    incoming = clone(incoming)
    if existing is None:
        return "insert", ({"chat_id": chat_id, "tombstone": True} if incoming.get("tombstone") else incoming)
    if existing.get("chat_id") != chat_id:
        raise ModelError("Chat IDs do not match")
    if existing.get("tombstone"):
        if incoming.get("tombstone"):
            return "noop", clone(existing)
        raise ModelError(f"Chat {chat_id} cannot be resurrected")
    if incoming.get("tombstone"):
        return "replace", {"chat_id": chat_id, "tombstone": True}
    old_kind, new_kind = existing.get("record_kind", "full"), incoming.get("record_kind", "full")
    if old_kind not in {"placeholder", "full"} or new_kind not in {"placeholder", "full"}:
        raise ModelError("Chat record_kind must be placeholder or full")
    if old_kind == "full" and new_kind == "placeholder":
        return "noop", clone(existing)
    if old_kind == "placeholder" and new_kind == "full":
        return "replace", incoming
    if old_kind == "placeholder":
        if material_equal(existing, incoming):
            return "noop", clone(existing)
        raise ModelError("Placeholder chat has conflicting material")
    if existing.get("platform_user_id") != incoming.get("platform_user_id"):
        raise ModelError("Chat has conflicting platform identity")
    old_time, new_time = existing.get("updated_at"), incoming.get("updated_at")
    if not isinstance(old_time, str) or not isinstance(new_time, str):
        raise ModelError("Chat has an invalid updated_at")
    if new_time < old_time:
        return "noop", clone(existing)
    if new_time > old_time:
        return "replace", incoming
    if material_equal(existing, incoming):
        return "noop", clone(existing)
    raise ModelError(f"Chat {chat_id} conflicts at the same upstream updated_at")


def decide_message_update(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    message_id = incoming.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise ModelError("Message requires message_id")
    incoming = clone(incoming)
    if existing is None:
        if incoming.get("tombstone"):
            if not incoming.get("chat_id"):
                raise ModelError("Message tombstone requires chat_id")
            return "insert", {"message_id": message_id, "chat_id": incoming["chat_id"], "tombstone": True}
        return "insert", incoming
    if existing.get("message_id") != message_id:
        raise ModelError("Message IDs do not match")
    if existing.get("tombstone"):
        if not incoming.get("tombstone"):
            raise ModelError(f"Message {message_id} cannot be resurrected")
        if incoming.get("chat_id", existing.get("chat_id")) != existing.get("chat_id"):
            raise ModelError("Message tombstone has conflicting parent")
        return "noop", clone(existing)
    if incoming.get("tombstone"):
        if incoming.get("chat_id", existing.get("chat_id")) != existing.get("chat_id"):
            raise ModelError("Message tombstone has conflicting parent")
        return "replace", {"message_id": message_id, "chat_id": existing["chat_id"], "tombstone": True}
    if material_equal(existing, incoming):
        return "noop", clone(existing)
    raise ModelError(f"Message {message_id} has conflicting immutable material")


@dataclass
class AgentDeliveryModel:
    agent_stream_id: str = "70000000-0000-4000-8000-000000000001"
    account_epoch: int = 1
    last_source_seq: int = 0
    acknowledged_source_seq: int = 0
    chats: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    outbox: list[dict[str, Any]] = field(default_factory=list)
    pending_snapshot: dict[str, Any] | None = None
    manifests: dict[str, dict[str, Any]] = field(default_factory=dict)
    chunks: list[dict[str, Any]] = field(default_factory=list)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    connected: bool = False
    fence: str | None = None
    sync_required: bool = False
    replay: list[int] = field(default_factory=list)
    # This is deliberately a declarative build plan.  It is not a storage cursor
    # or a reproduction of the implementation's scan fields.
    snapshot_kinds_remaining: list[str] = field(default_factory=list)

    def _preserve_override(self, kind: str, entity_id: str, previous: dict[str, Any] | None) -> None:
        if self.pending_snapshot is None or self.pending_snapshot["state"] != "building" or previous is None or previous["last_source_seq"] > self.pending_snapshot["through_seq"]:
            return
        key = f"{kind}:{entity_id}"
        if key not in self.overrides:
            self.overrides[key] = {"key": key, "snapshot_id": self.pending_snapshot["snapshot_id"], "kind": kind, "entity_id": entity_id, "envelope": clone(previous)}

    def capture(self, change: dict[str, Any], event_id: str, origin: str = "passive") -> dict[str, Any] | None:
        kind, candidate = change.get("type"), self.last_source_seq + 1
        if kind == "coverage.observed":
            key = evidence_key(change["evidence"]); old = self.coverage.get(key)
            if old is not None:
                if material_equal(old["evidence"], change["evidence"]): return None
                raise ModelError(f"Coverage evidence {key} was reused with conflicting material")
            self.coverage[key] = {"evidence_key": key, "evidence": clone(change["evidence"]), "last_source_seq": candidate, "last_origin": origin}
        else:
            if kind == "chat.upsert":
                name, store, entity_id, value, merger = "chat", self.chats, change["chat"]["chat_id"], change["chat"], decide_chat_update
            elif kind == "chat.delete":
                name, store, entity_id, value, merger = "chat", self.chats, change["chat_id"], {"chat_id": change["chat_id"], "tombstone": True}, decide_chat_update
            elif kind == "message.upsert":
                name, store, entity_id, value, merger = "message", self.messages, change["message"]["message_id"], change["message"], decide_message_update
            elif kind == "message.delete":
                name, store, entity_id, value, merger = "message", self.messages, change["message_id"], {"message_id": change["message_id"], "chat_id": change["chat_id"], "tombstone": True}, decide_message_update
            else:
                raise ModelError(f"Unsupported change {kind}")
            action, merged = merger(store.get(entity_id), value)
            if action == "noop": return None
            self._preserve_override(name, entity_id, store.get(entity_id))
            store[entity_id] = {**clone(merged), "last_source_seq": candidate, "last_origin": origin}
        item = {"event_id": event_id, "source_seq": candidate, "acquisition_origin": origin, "change": clone(change)}
        self.outbox.append(item); self.last_source_seq = candidate
        return clone(item)

    def capture_message_parent(self, message: dict[str, Any], parent: dict[str, Any], parent_event_id: str, message_event_id: str, origin: str = "passive") -> dict[str, Any]:
        if message.get("type") != "message.upsert" or parent.get("type") != "chat.upsert" or message["message"].get("chat_id") != parent["chat"].get("chat_id"):
            raise ModelError("Message and placeholder parent must use the same chat_id")
        trial = clone(self)
        parent_item = trial.capture(parent, parent_event_id, origin)
        message_item = trial.capture(message, message_event_id, origin)
        self.__dict__.update(trial.__dict__)
        return {"items": [item for item in (parent_item, message_item) if item], "messageItem": message_item, "parentCreated": parent_item is not None}

    def acknowledge(self, committed_source_seq: int, snapshot_id: str | None = None, snapshot_progress: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(committed_source_seq, int) or committed_source_seq > self.last_source_seq: raise ModelError("Acknowledgment exceeds the locally recorded source sequence")
        snapshot_acknowledged = False
        if snapshot_progress is not None:
            if snapshot_id != snapshot_progress.get("snapshot_id") or self.pending_snapshot is None or snapshot_id != self.pending_snapshot["snapshot_id"]: raise ModelError("Snapshot acknowledgement identity does not match")
            self.pending_snapshot["next_expected_chunk_index"] = snapshot_progress["next_expected_chunk_index"]
            if snapshot_progress.get("committed"):
                manifest = self.manifests.get(snapshot_id)
                if manifest is None or manifest["state"] != "ready" or committed_source_seq < manifest["through_seq"]: raise ModelError("Snapshot commit acknowledgement does not cover manifest")
                self.chunks = [chunk for chunk in self.chunks if chunk["snapshot_id"] != snapshot_id]
                self.manifests.pop(snapshot_id, None); self.overrides.clear(); self.pending_snapshot = None; self.sync_required = False; snapshot_acknowledged = True
        checkpoint = max(self.acknowledged_source_seq, committed_source_seq)
        if snapshot_progress is None or snapshot_progress.get("committed"):
            self.outbox = [item for item in self.outbox if item["source_seq"] > checkpoint]; self.acknowledged_source_seq = checkpoint
        return {"snapshotAcknowledged": snapshot_acknowledged, "committedSourceSeq": self.acknowledged_source_seq}

    def create_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        if self.pending_snapshot is not None: return clone(self.manifests[self.pending_snapshot["snapshot_id"]])
        manifest = {"snapshot_id": snapshot_id, "through_seq": self.last_source_seq, "state": "building", "chunk_count": None, "record_counts": {"chats": 0, "messages": 0, "coverage_evidence": 0}}
        self.chunks.clear(); self.overrides.clear(); self.manifests[snapshot_id] = manifest
        self.pending_snapshot = {"snapshot_id": snapshot_id, "through_seq": self.last_source_seq, "state": "building", "next_expected_chunk_index": 0}
        self.snapshot_kinds_remaining = ["chat", "message", "coverage_evidence"]
        return clone(manifest)

    @staticmethod
    def _snapshot_record(kind: str, envelope: dict[str, Any]) -> dict[str, Any]:
        material = {key: clone(value) for key, value in envelope.items() if key not in {"last_source_seq", "last_origin"}}
        if kind == "coverage_evidence": return material["evidence"]
        if kind == "chat": return {"tombstone": True, "chat_id": material["chat_id"]} if material.get("tombstone") else {"tombstone": False, "chat": material}
        return {"tombstone": True, "message_id": material["message_id"], "chat_id": material["chat_id"]} if material.get("tombstone") else {"tombstone": False, "message": material}

    def build_next_snapshot_chunk(self) -> dict[str, Any]:
        if self.pending_snapshot is None: raise ModelError("No pending snapshot")
        manifest = self.manifests[self.pending_snapshot["snapshot_id"]]
        if manifest["state"] == "ready": return {"manifest": clone(manifest), "chunk": None}
        if not self.snapshot_kinds_remaining:
            raise ModelError("Snapshot build plan is exhausted")
        kind = self.snapshot_kinds_remaining.pop(0)
        store = {"chat": self.chats, "message": self.messages, "coverage_evidence": self.coverage}[kind]
        records = []
        # Contract semantics define a chunk as a bounded entity collection; the
        # storage cursor order is observed and normalized by the adapter, rather
        # than copied into this independent model.
        keys = sorted(store)
        for entity_id in keys:
            value = store[entity_id]; override = self.overrides.get(f"{kind}:{entity_id}")
            envelope = override["envelope"] if override else value if value["last_source_seq"] <= manifest["through_seq"] else None
            if envelope is not None: records.append(self._snapshot_record(kind, envelope))
        chunk = None
        if records:
            chunk = {"snapshot_id": manifest["snapshot_id"], "chunk_index": len(self.chunks), "entity_kind": kind, "records": records}
            self.chunks.append(chunk); manifest["record_counts"][{"chat": "chats", "message": "messages", "coverage_evidence": "coverage_evidence"}[kind]] += len(records)
        if not self.snapshot_kinds_remaining:
            manifest["state"] = "ready"; manifest["chunk_count"] = len(self.chunks); self.pending_snapshot["state"] = "ready"; self.overrides.clear()
        return {"manifest": clone(manifest), "chunk": clone(chunk)}

    def connect(self, fence: str, committed_source_seq: int | None = None, resume_action: str = "resume") -> None:
        committed = self.acknowledged_source_seq if committed_source_seq is None else committed_source_seq
        if committed > self.last_source_seq: raise ModelError("Session checkpoint exceeds local sequence")
        self.connected, self.fence, self.sync_required = True, fence, resume_action == "snapshot_required"
        self.replay = [] if self.sync_required else [item["source_seq"] for item in self.outbox if item["source_seq"] > committed]

    def identity_result(self) -> dict[str, Any]:
        return clone(self.state()["identity"])

    def transport_result(self) -> dict[str, Any]:
        return {"connected": self.connected, "fence": self.fence, "sync_required": self.sync_required, "replay": clone(self.replay)}

    def require_sync(self) -> None:
        if self.pending_snapshot is None or self.pending_snapshot["state"] != "ready":
            raise ModelError("sync.required requires a ready local snapshot")
        self.sync_required = True

    def state(self) -> dict[str, Any]:
        return {"identity": {"creator_account_id": "qualification-account", "agent_stream_id": self.agent_stream_id, "account_epoch": self.account_epoch, "last_source_seq": self.last_source_seq, "acknowledged_source_seq": self.acknowledged_source_seq, "applied_config_revision": None, "outbox_count": len(self.outbox), "entity_counts": {"chats": len(self.chats), "messages": len(self.messages), "coverage_evidence": len(self.coverage)}, "pending_snapshot": clone(self.pending_snapshot)}, "outbox": clone(self.outbox), "chats": sorted(clone(self.chats).values(), key=lambda item: item["chat_id"]), "messages": sorted(clone(self.messages).values(), key=lambda item: item["message_id"]), "coverage": sorted(clone(self.coverage).values(), key=lambda item: item["evidence_key"]), "manifests": sorted(clone(self.manifests).values(), key=lambda item: item["snapshot_id"]), "chunks": sorted(clone(self.chunks), key=lambda item: item["chunk_index"]), "overrides": sorted(clone(self.overrides).values(), key=lambda item: item["key"]), "transport": {"connected": self.connected, "fence": self.fence, "sync_required": self.sync_required, "replay": clone(self.replay)}}
