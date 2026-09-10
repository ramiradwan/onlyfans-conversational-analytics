"""Independent pure model of canonical ingestion state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID


def _model_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _model_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_model_json(value).encode("utf-8")).hexdigest()


def _canonical_time(value: str | None) -> str | None:
    """Normalize observable timestamps without using production helpers."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


@dataclass(frozen=True, slots=True)
class ModelStreamKey:
    creator_account_id: str
    agent_installation_id: UUID
    agent_stream_id: UUID


@dataclass(slots=True)
class ModelChat:
    chat_id: str
    record_kind: str
    platform_user_id: str | None
    display_name: str | None
    updated_at: str | None
    content_fingerprint: str
    is_deleted: bool = False


@dataclass(slots=True)
class ModelMessage:
    message_id: str
    chat_id: str
    sender_platform_user_id: str
    text: str
    sent_at: str
    direction: str
    content_fingerprint: str
    is_deleted: bool = False


@dataclass(slots=True)
class ModelStagedSnapshot:
    snapshot_id: UUID
    starting_checkpoint: int | None
    through_seq: int
    chunk_count: int
    expected_chats: int
    expected_messages: int
    expected_coverage: int
    begin_fingerprint: str
    next_chunk_index: int = 0
    received_chats: int = 0
    received_messages: int = 0
    received_coverage: int = 0
    last_entity_kind: str | None = None
    chunks: dict[int, tuple[str, str, list[dict[str, Any]]]] = field(default_factory=dict)
    staged_chat_ids: set[str] = field(default_factory=set)
    staged_message_ids: set[str] = field(default_factory=set)
    staged_coverage_hashes: set[str] = field(default_factory=set)
    state: Literal["staging", "committed"] = "staging"


@dataclass(frozen=True, slots=True)
class ModelChatUpsertCommand:
    event_id: UUID
    source_seq: int
    chat_id: str
    record_kind: Literal["placeholder", "full"]
    platform_user_id: str | None
    display_name: str | None
    updated_at: str | None
    acquisition_origin: str = "passive"

    def chat_content_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "display_name": self.display_name,
            "platform_user_id": self.platform_user_id,
            "record_kind": self.record_kind,
            "updated_at": self.updated_at,
        }

    def to_change_dict(self) -> dict[str, Any]:
        return {
            "type": "chat.upsert",
            "chat": self.chat_content_dict(),
        }


@dataclass(frozen=True, slots=True)
class ModelMessageUpsertCommand:
    event_id: UUID
    source_seq: int
    message_id: str
    chat_id: str
    sender_platform_user_id: str
    text: str
    sent_at: str
    direction: Literal["inbound", "outbound"]
    acquisition_origin: str = "passive"

    def message_content_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "direction": self.direction,
            "message_id": self.message_id,
            "sender_platform_user_id": self.sender_platform_user_id,
            "sent_at": self.sent_at,
            "text": self.text,
        }

    def to_change_dict(self) -> dict[str, Any]:
        return {
            "type": "message.upsert",
            "message": self.message_content_dict(),
        }


@dataclass(frozen=True, slots=True)
class ModelChatDeleteCommand:
    event_id: UUID
    source_seq: int
    chat_id: str
    acquisition_origin: str = "passive"

    def to_change_dict(self) -> dict[str, Any]:
        return {
            "type": "chat.delete",
            "chat_id": self.chat_id,
        }


@dataclass(frozen=True, slots=True)
class ModelMessageDeleteCommand:
    event_id: UUID
    source_seq: int
    message_id: str
    chat_id: str
    acquisition_origin: str = "passive"

    def to_change_dict(self) -> dict[str, Any]:
        return {
            "type": "message.delete",
            "chat_id": self.chat_id,
            "message_id": self.message_id,
        }


@dataclass(frozen=True, slots=True)
class ModelCoverageObservedCommand:
    event_id: UUID
    source_seq: int
    coverage_type: str
    evidence_payload: dict[str, Any]
    acquisition_origin: str = "passive"

    def to_change_dict(self) -> dict[str, Any]:
        return {
            "type": "coverage.observed",
            "evidence": {
                "type": self.coverage_type,
                **self.evidence_payload,
            },
        }


@dataclass(frozen=True, slots=True)
class ModelSnapshotBeginCommand:
    snapshot_id: UUID
    through_seq: int
    chunk_count: int
    expected_chats: int
    expected_messages: int
    expected_coverage: int
    max_frame_bytes: int = 524288


@dataclass(frozen=True, slots=True)
class ModelSnapshotChunkCommand:
    snapshot_id: UUID
    chunk_index: int
    entity_kind: Literal["chat", "message", "coverage_evidence"]
    records: list[dict[str, Any]]
    estimated_bytes: int = 1024


@dataclass(frozen=True, slots=True)
class ModelSnapshotCommitCommand:
    snapshot_id: UUID
    chunk_count: int


@dataclass(frozen=True, slots=True)
class ModelTransitionOutcome:
    disposition: Literal["accepted", "duplicate", "gap", "rejected"]
    committed_source_seq: int
    code: str | None = None
    retryable: bool = False
    snapshot_id: UUID | None = None
    next_expected_chunk_index: int | None = None
    snapshot_committed: bool = False
    canonical_revision: int | None = None
    error_message: str | None = None


class PureBrainIngestionModel:
    """Reference model for canonical ingestion."""

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.canonical_revision: int = 0
        self.checkpoints: dict[ModelStreamKey, int] = {}
        self.chats: dict[str, ModelChat] = {}
        self.messages: dict[str, ModelMessage] = {}
        self._tombstones: set[tuple[str, str]] = set()
        self._message_tombstone_chats: dict[str, str] = {}
        self.committed_events: dict[UUID, tuple[int, str, ModelStreamKey]] = {}
        self.sequence_owners: dict[tuple[ModelStreamKey, int], UUID] = {}
        self.snapshot_uploads: dict[tuple[ModelStreamKey, UUID], ModelStagedSnapshot] = {}
        self.pending_snapshots: dict[ModelStreamKey, UUID] = {}
        self.coverage: dict[str, Any] = {
            "status": "unknown", "phase": "not_started", "generation_id": None,
            "as_of": None, "discovered_conversations": None,
            "complete_conversations": 0, "complete_as_of": None, "reason": None,
        }
        self._coverage_generations: dict[str, dict[str, Any]] = {}
        self._coverage_members: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_complete_as_of: str | None = None

    def checkpoint(self, key: ModelStreamKey) -> int | None:
        return self.checkpoints.get(key)

    def pending_snapshot(self, key: ModelStreamKey) -> dict[str, Any] | None:
        snapshot_id = self.pending_snapshots.get(key)
        if snapshot_id is None:
            return None
        upload = self.snapshot_uploads.get((key, snapshot_id))
        if upload is None or upload.state != "staging":
            return None
        return {"snapshot_id": snapshot_id, "starting_checkpoint": upload.starting_checkpoint,
                "through_seq": upload.through_seq, "chunk_count": upload.chunk_count,
                "expected": (upload.expected_chats, upload.expected_messages, upload.expected_coverage),
                "next_chunk_index": upload.next_chunk_index,
                "chunks": tuple((index, kind, _model_hash({"entity_kind": kind, "records": json.loads(_model_json(records).replace("+00:00", "Z"))}) ) for index, (kind, _, records) in sorted(upload.chunks.items()))}

    def staged_record_counts(self, key: ModelStreamKey) -> dict[str, int]:
        upload_id = self.pending_snapshots.get(key)
        upload = None if upload_id is None else self.snapshot_uploads.get((key, upload_id))
        return {
            "chats": 0 if upload is None else upload.received_chats,
            "messages": 0 if upload is None else upload.received_messages,
            "coverage_evidence": 0 if upload is None else upload.received_coverage,
        }

    def _apply_coverage(self, evidence: dict[str, Any]) -> bool:
        """Independent projection of the six admissible coverage evidence records."""
        kind, generation_id = evidence["type"], str(evidence["generation_id"])
        if kind == "generation.started":
            if generation_id in self._coverage_generations:
                return False
            self._last_complete_as_of = self.coverage["complete_as_of"] or self._last_complete_as_of
            self._coverage_generations[generation_id] = {"as_of": evidence["as_of"], "state": "discovering", "inventory_ended": None, "closed": None, "reason": None}
            self.coverage = {"status":"partial", "phase":"discovering", "generation_id":generation_id, "as_of":evidence["as_of"], "discovered_conversations":0, "complete_conversations":0, "complete_as_of":self._last_complete_as_of, "reason":None}
            return True
        generation = self._coverage_generations.get(generation_id)
        if generation is None:
            raise ValueError("coverage evidence references an unknown generation")
        conversation = evidence.get("conversation_id")
        member_key = (generation_id, str(conversation))
        if kind == "inventory.member":
            if member_key in self._coverage_members:
                return False
            if conversation not in self.active_chats():
                raise ValueError("inventory member references an unknown conversation")
            self._coverage_members[member_key] = {"history": None, "head": None, "earliest": None}
        elif kind == "inventory.ended":
            if generation["inventory_ended"] is not None:
                return False
            generation["inventory_ended"] = evidence["observed_at"]; generation["state"] = "backfilling"
        elif kind == "conversation.history_started":
            member = self._coverage_members.get(member_key)
            if member is None or member["history"] is not None:
                return False
            member["history"] = evidence["observed_at"]; member["earliest"] = evidence.get("earliest_observed_at")
        elif kind == "conversation.head_reconciled":
            member = self._coverage_members.get(member_key)
            if member is None or member["head"] is not None:
                return False
            member["head"] = evidence["reconciled_through"]
        elif kind == "generation.closed":
            if generation["closed"] is not None:
                return False
            complete = generation["inventory_ended"] is not None and all(m["history"] is not None and m["head"] is not None for (g, _), m in self._coverage_members.items() if g == generation_id)
            generation["closed"] = evidence["closed_at"]; generation["state"] = "complete" if complete else "partial"; generation["reason"] = None if complete else ("inventory_not_frozen" if generation["inventory_ended"] is None else "conversation_evidence_missing")
            if complete:
                self._last_complete_as_of = generation["as_of"]
        else:
            raise ValueError(f"unsupported coverage evidence {kind}")
        members = [m for (g, _), m in self._coverage_members.items() if g == generation_id]
        complete_count = sum(m["history"] is not None and m["head"] is not None for m in members)
        state = generation["state"]
        self.coverage = {"status":"complete" if state == "complete" else "partial", "phase":{"discovering":"discovering","backfilling":"backfilling","complete":"complete","partial":"blocked"}[state], "generation_id":generation_id, "as_of":generation["as_of"], "discovered_conversations":len(members), "complete_conversations":complete_count, "complete_as_of":self._last_complete_as_of, "reason":generation["reason"]}
        return True

    def _invalidate_complete_coverage_for_chat(self, chat_id: str) -> None:
        generation_id = self.coverage["generation_id"]
        generation = None if generation_id is None else self._coverage_generations.get(generation_id)
        if generation is None or generation["state"] != "complete" or (str(generation_id), chat_id) in self._coverage_members:
            return
        generation["state"] = "superseded"; generation["reason"] = "new_conversation_discovered"
        self.coverage["status"] = "partial"; self.coverage["phase"] = "repairing"; self.coverage["reason"] = "new_conversation_discovered"

    def active_chats(self) -> dict[str, ModelChat]:
        return {k: v for k, v in self.chats.items() if not v.is_deleted}

    def active_messages(self) -> dict[str, ModelMessage]:
        return {k: v for k, v in self.messages.items() if not v.is_deleted}

    def tombstones(self) -> set[tuple[str, str]]:
        return set(self._tombstones)

    def is_tombstoned(self, entity_kind: str, entity_id: str) -> bool:
        return (entity_kind, entity_id) in self._tombstones

    def _record_event(
        self,
        key: ModelStreamKey,
        event_id: UUID,
        source_seq: int,
        fingerprint: str,
    ) -> None:
        self.committed_events[event_id] = (source_seq, fingerprint, key)
        self.sequence_owners[(key, source_seq)] = event_id

    def _snapshot_validation_error(self, upload: ModelStagedSnapshot) -> str | None:
        """Validate staged rows against canonical state before snapshot mutation."""
        live_snapshot_chats: set[str] = set()
        tombstoned_snapshot_chats: set[str] = set()
        for kind, _, records in upload.chunks.values():
            if kind != "chat":
                continue
            for record in records:
                if record.get("tombstone"):
                    tombstoned_snapshot_chats.add(record["chat_id"])
                    continue
                chat = record["chat"]
                chat_id = chat["chat_id"]
                live_snapshot_chats.add(chat_id)
                existing = self.chats.get(chat_id)
                if existing is None or existing.is_deleted:
                    continue
                if (existing.platform_user_id is not None and chat.get("platform_user_id") is not None
                        and existing.platform_user_id != chat["platform_user_id"]):
                    return "chat platform identity conflicts with canonical identity"
                if (existing.record_kind == "full" and chat["record_kind"] == "full"
                        and existing.updated_at == _canonical_time(chat.get("updated_at"))
                        and existing.content_fingerprint != _model_hash(chat)):
                    return "chat version has conflicting content"

        for kind, _, records in upload.chunks.values():
            if kind != "message":
                continue
            for record in records:
                if record.get("tombstone"):
                    message_id, chat_id = record["message_id"], record["chat_id"]
                    existing = self.messages.get(message_id)
                    prior_chat_id = self._message_tombstone_chats.get(message_id)
                    if ((existing is not None and existing.chat_id != chat_id)
                            or (prior_chat_id is not None and prior_chat_id != chat_id)):
                        return "message tombstone conflicts with canonical conversation"
                    continue
                message = record["message"]
                message_id, chat_id = message["message_id"], message["chat_id"]
                existing_chat = self.chats.get(chat_id)
                parent_is_live = chat_id not in tombstoned_snapshot_chats and (
                    (existing_chat is not None and not existing_chat.is_deleted)
                    or (chat_id in live_snapshot_chats
                        and existing_chat is None
                        and ("chat", chat_id) not in self._tombstones)
                )
                if ("message", message_id) not in self._tombstones and not parent_is_live:
                    return "message references an unknown or deleted chat"
                existing = self.messages.get(message_id)
                if existing is not None and not existing.is_deleted and (
                    existing.chat_id != chat_id
                    or existing.sender_platform_user_id != message["sender_platform_user_id"]
                    or existing.text != message["text"]
                    or existing.sent_at != (_canonical_time(message["sent_at"]) or message["sent_at"])
                    or existing.direction != message["direction"]
                ):
                    return "immutable message identifier has conflicting content"
        return None

    def begin_snapshot(
        self,
        key: ModelStreamKey,
        cmd: ModelSnapshotBeginCommand,
    ) -> ModelTransitionOutcome:
        current_checkpoint = self.checkpoints.get(key)
        checkpoint_val = 0 if current_checkpoint is None else current_checkpoint
        fingerprint = _model_hash(
            {
                "through_seq": cmd.through_seq,
                "chunk_count": cmd.chunk_count,
                "record_counts": {
                    "chats": cmd.expected_chats,
                    "messages": cmd.expected_messages,
                    "coverage_evidence": cmd.expected_coverage,
                },
                "max_frame_bytes": cmd.max_frame_bytes,
            }
        )

        existing = self.snapshot_uploads.get((key, cmd.snapshot_id))
        if existing is not None:
            if existing.begin_fingerprint != fingerprint:
                return ModelTransitionOutcome(
                    disposition="rejected",
                    committed_source_seq=checkpoint_val,
                    snapshot_id=cmd.snapshot_id,
                    code="chunk_conflict",
                )
            return ModelTransitionOutcome(
                disposition="duplicate",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                next_expected_chunk_index=existing.next_chunk_index,
                snapshot_committed=(existing.state == "committed"),
            )

        active_pending_id = self.pending_snapshots.get(key)
        if active_pending_id is not None:
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                code="invariant_failed",
            )

        upload = ModelStagedSnapshot(
            snapshot_id=cmd.snapshot_id,
            starting_checkpoint=current_checkpoint,
            through_seq=cmd.through_seq,
            chunk_count=cmd.chunk_count,
            expected_chats=cmd.expected_chats,
            expected_messages=cmd.expected_messages,
            expected_coverage=cmd.expected_coverage,
            begin_fingerprint=fingerprint,
            next_chunk_index=0,
            state="staging",
        )
        self.snapshot_uploads[(key, cmd.snapshot_id)] = upload
        self.pending_snapshots[key] = cmd.snapshot_id

        return ModelTransitionOutcome(
            disposition="accepted",
            committed_source_seq=checkpoint_val,
            snapshot_id=cmd.snapshot_id,
            next_expected_chunk_index=0,
        )

    def add_snapshot_chunk(
        self,
        key: ModelStreamKey,
        cmd: ModelSnapshotChunkCommand,
    ) -> ModelTransitionOutcome:
        current_checkpoint = self.checkpoints.get(key)
        checkpoint_val = 0 if current_checkpoint is None else current_checkpoint

        if cmd.estimated_bytes > 512 * 1024:
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                code="invariant_failed",
                error_message="snapshot frame exceeds 512 KiB",
            )

        upload = self.snapshot_uploads.get((key, cmd.snapshot_id))
        if upload is None:
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                code="snapshot_incomplete",
            )

        chunk_fingerprint = _model_hash({"entity_kind": cmd.entity_kind, "records": cmd.records})
        if cmd.chunk_index in upload.chunks:
            existing_kind, existing_fp, _ = upload.chunks[cmd.chunk_index]
            if existing_fp != chunk_fingerprint:
                return ModelTransitionOutcome(
                    disposition="rejected",
                    committed_source_seq=checkpoint_val,
                    snapshot_id=cmd.snapshot_id,
                    code="chunk_conflict",
                )
            return ModelTransitionOutcome(
                disposition="duplicate",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                next_expected_chunk_index=upload.next_chunk_index,
                snapshot_committed=(upload.state == "committed"),
            )

        if upload.state == "committed":
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                next_expected_chunk_index=upload.next_chunk_index,
                snapshot_committed=True,
                code="chunk_conflict",
            )

        if cmd.chunk_index != upload.next_chunk_index or cmd.chunk_index >= upload.chunk_count:
            return ModelTransitionOutcome(
                disposition="gap",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                next_expected_chunk_index=upload.next_chunk_index,
                code="sequence_gap",
                retryable=True,
            )

        if cmd.entity_kind == "chat":
            identifiers = [
                record["chat_id"] if record.get("tombstone") else record["chat"]["chat_id"]
                for record in cmd.records
            ]
            staged_identifiers = upload.staged_chat_ids
        elif cmd.entity_kind == "message":
            identifiers = [
                record["message_id"] if record.get("tombstone") else record["message"]["message_id"]
                for record in cmd.records
            ]
            staged_identifiers = upload.staged_message_ids
        else:
            identifiers = [_model_hash(record) for record in cmd.records]
            staged_identifiers = upload.staged_coverage_hashes
        if len(set(identifiers)) != len(identifiers) or any(value in staged_identifiers for value in identifiers):
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                code="invariant_failed",
            )

        kind_order = {"chat": 0, "message": 1, "coverage_evidence": 2}
        if upload.last_entity_kind is not None and kind_order[cmd.entity_kind] < kind_order[upload.last_entity_kind]:
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                next_expected_chunk_index=upload.next_chunk_index,
                code="invariant_failed",
            )

        upload.chunks[cmd.chunk_index] = (cmd.entity_kind, chunk_fingerprint, cmd.records)
        staged_identifiers.update(identifiers)
        upload.next_chunk_index += 1
        upload.last_entity_kind = cmd.entity_kind

        if cmd.entity_kind == "chat":
            upload.received_chats += len(cmd.records)
        elif cmd.entity_kind == "message":
            upload.received_messages += len(cmd.records)
        else:
            upload.received_coverage += len(cmd.records)

        return ModelTransitionOutcome(
            disposition="accepted",
            committed_source_seq=checkpoint_val,
            snapshot_id=cmd.snapshot_id,
            next_expected_chunk_index=upload.next_chunk_index,
        )

    def commit_snapshot(
        self,
        key: ModelStreamKey,
        cmd: ModelSnapshotCommitCommand,
    ) -> ModelTransitionOutcome:
        current_checkpoint = self.checkpoints.get(key)
        checkpoint_val = 0 if current_checkpoint is None else current_checkpoint

        upload = self.snapshot_uploads.get((key, cmd.snapshot_id))
        if upload is None:
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                code="snapshot_incomplete",
            )

        if cmd.chunk_count != upload.chunk_count:
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                next_expected_chunk_index=upload.next_chunk_index,
                code="chunk_conflict",
            )

        if upload.state == "committed":
            return ModelTransitionOutcome(
                disposition="duplicate",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                next_expected_chunk_index=upload.next_chunk_index,
                snapshot_committed=True,
            )

        if current_checkpoint != upload.starting_checkpoint:
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                next_expected_chunk_index=upload.next_chunk_index,
                code="invariant_failed",
            )

        if (
            upload.chunk_count != upload.next_chunk_index
            or upload.expected_chats != upload.received_chats
            or upload.expected_messages != upload.received_messages
            or upload.expected_coverage != upload.received_coverage
        ):
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                next_expected_chunk_index=upload.next_chunk_index,
                code="snapshot_incomplete",
            )

        if upload.through_seq < checkpoint_val:
            return ModelTransitionOutcome(
                disposition="rejected",
                committed_source_seq=checkpoint_val,
                snapshot_id=cmd.snapshot_id,
                code="invariant_failed",
            )

        validation_error = self._snapshot_validation_error(upload)
        if validation_error is not None:
            return ModelTransitionOutcome(
                "rejected", checkpoint_val, code="invariant_failed", error_message=validation_error,
            )

        changed = False
        for chunk_idx in range(upload.chunk_count):
            kind, _, records = upload.chunks[chunk_idx]
            if kind == "chat":
                for rec in records:
                    if rec.get("tombstone"):
                        chat_id = rec["chat_id"]
                        self._tombstones.add(("chat", chat_id))
                        if chat_id in self.chats:
                            self.chats[chat_id].is_deleted = True
                        for message in self.messages.values():
                            if message.chat_id == chat_id:
                                message.is_deleted = True
                        changed = True
                    else:
                        c = rec["chat"]
                        chat_id = c["chat_id"]
                        if ("chat", chat_id) in self._tombstones:
                            continue
                        existing = self.chats.get(chat_id)
                        fp = _model_hash(c)
                        if existing is None:
                            self.chats[chat_id] = ModelChat(
                                chat_id=chat_id,
                                record_kind=c["record_kind"],
                                platform_user_id=c.get("platform_user_id"),
                                display_name=c.get("display_name"),
                                updated_at=_canonical_time(c.get("updated_at")),
                                content_fingerprint=fp,
                                is_deleted=False,
                            )
                            self._invalidate_complete_coverage_for_chat(chat_id)
                            changed = True
                        elif not existing.is_deleted:
                            incoming_at = _canonical_time(c.get("updated_at"))
                            # Snapshot chat rows use the same mutable-version rule
                            # as deltas: an older or equal version cannot replace
                            # the existing canonical value.
                            if existing.record_kind == "full" and c["record_kind"] == "placeholder":
                                continue
                            if (existing.updated_at is not None and incoming_at is not None
                                    and incoming_at <= existing.updated_at):
                                continue
                            existing.record_kind = c["record_kind"]
                            existing.platform_user_id = c.get("platform_user_id") or existing.platform_user_id
                            existing.display_name = c.get("display_name")
                            existing.updated_at = incoming_at
                            existing.content_fingerprint = fp
                            changed = True
            elif kind == "message":
                for rec in records:
                    if rec.get("tombstone"):
                        message_id = rec["message_id"]
                        self._tombstones.add(("message", message_id))
                        self._message_tombstone_chats[message_id] = rec["chat_id"]
                        if message_id in self.messages:
                            self.messages[message_id].is_deleted = True
                        changed = True
                    else:
                        m = rec["message"]
                        message_id = m["message_id"]
                        if ("message", message_id) in self._tombstones:
                            continue
                        existing = self.messages.get(message_id)
                        fp = _model_hash(m)
                        if existing is None:
                            self.messages[message_id] = ModelMessage(
                                message_id=message_id,
                                chat_id=m["chat_id"],
                                sender_platform_user_id=m["sender_platform_user_id"],
                                text=m["text"],
                                sent_at=_canonical_time(m["sent_at"]) or m["sent_at"],
                                direction=m["direction"],
                                content_fingerprint=fp,
                                is_deleted=False,
                            )
                            changed = True
            elif kind == "coverage_evidence":
                for rec in records:
                    changed |= self._apply_coverage(rec)

        upload.state = "committed"
        self.pending_snapshots.pop(key, None)
        self.checkpoints[key] = upload.through_seq
        if changed:
            self.canonical_revision += 1

        return ModelTransitionOutcome(
            disposition="accepted",
            committed_source_seq=upload.through_seq,
            snapshot_id=cmd.snapshot_id,
            next_expected_chunk_index=upload.next_chunk_index,
            snapshot_committed=True,
            canonical_revision=self.canonical_revision if changed else None,
        )

    def commit_delta(
        self,
        key: ModelStreamKey,
        cmd: Any,
    ) -> ModelTransitionOutcome:
        checkpoint = self.checkpoints.get(key)
        if checkpoint is None:
            return ModelTransitionOutcome(
                disposition="gap",
                committed_source_seq=0,
                code="sequence_gap",
                retryable=True,
            )

        change_dict = cmd.to_change_dict()
        fingerprint = _model_hash(
            {
                "source_seq": cmd.source_seq,
                "change": change_dict,
            }
        )

        prior = self.committed_events.get(cmd.event_id)
        if prior is not None:
            prior_seq, prior_fp, prior_key = prior
            if prior_seq != cmd.source_seq or prior_fp != fingerprint:
                return ModelTransitionOutcome(
                    disposition="rejected",
                    committed_source_seq=checkpoint,
                    code="invariant_failed",
                )
            return ModelTransitionOutcome(
                disposition="duplicate",
                committed_source_seq=checkpoint,
            )

        if cmd.source_seq <= checkpoint:
            sequence_owner = self.sequence_owners.get((key, cmd.source_seq))
            if sequence_owner is not None and sequence_owner != cmd.event_id:
                return ModelTransitionOutcome(
                    disposition="rejected",
                    committed_source_seq=checkpoint,
                    code="invariant_failed",
                )
            return ModelTransitionOutcome(
                disposition="duplicate",
                committed_source_seq=checkpoint,
            )

        if cmd.source_seq != checkpoint + 1:
            return ModelTransitionOutcome(
                disposition="gap",
                committed_source_seq=checkpoint,
                code="sequence_gap",
                retryable=True,
            )

        # Contiguous delta: evaluate change semantics
        if isinstance(cmd, ModelChatUpsertCommand):
            cmd_updated_at = _canonical_time(cmd.updated_at)
            if ("chat", cmd.chat_id) in self._tombstones:
                self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
                self.checkpoints[key] = cmd.source_seq
                return ModelTransitionOutcome(
                    disposition="accepted",
                    committed_source_seq=cmd.source_seq,
                    canonical_revision=None,
                )

            existing = self.chats.get(cmd.chat_id)
            if existing is not None and not existing.is_deleted:
                if (
                    existing.platform_user_id is not None
                    and cmd.platform_user_id is not None
                    and existing.platform_user_id != cmd.platform_user_id
                ):
                    return ModelTransitionOutcome(
                        disposition="rejected",
                        committed_source_seq=checkpoint,
                        code="invariant_failed",
                        error_message="chat platform identity conflicts with canonical identity",
                    )

                if existing.record_kind == "full" and cmd.record_kind == "placeholder":
                    self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
                    self.checkpoints[key] = cmd.source_seq
                    return ModelTransitionOutcome(
                        disposition="accepted",
                        committed_source_seq=cmd.source_seq,
                        canonical_revision=None,
                    )

                if (
                    existing.updated_at is not None
                    and cmd_updated_at is not None
                    and existing.updated_at == cmd_updated_at
                ):
                    new_fp = _model_hash(cmd.chat_content_dict())
                    if new_fp != existing.content_fingerprint:
                        return ModelTransitionOutcome(
                            disposition="rejected",
                            committed_source_seq=checkpoint,
                            code="invariant_failed",
                            error_message="chat version has conflicting content",
                        )
                    self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
                    self.checkpoints[key] = cmd.source_seq
                    return ModelTransitionOutcome(
                        disposition="accepted",
                        committed_source_seq=cmd.source_seq,
                        canonical_revision=None,
                    )

                if (
                    existing.updated_at is not None
                    and cmd_updated_at is not None
                    and cmd_updated_at < existing.updated_at
                ):
                    self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
                    self.checkpoints[key] = cmd.source_seq
                    return ModelTransitionOutcome(
                        disposition="accepted",
                        committed_source_seq=cmd.source_seq,
                        canonical_revision=None,
                    )

                existing.record_kind = cmd.record_kind
                existing.platform_user_id = cmd.platform_user_id or existing.platform_user_id
                existing.display_name = cmd.display_name
                existing.updated_at = cmd_updated_at
                existing.content_fingerprint = _model_hash(cmd.chat_content_dict())
                self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
                self.checkpoints[key] = cmd.source_seq
                self.canonical_revision += 1
                return ModelTransitionOutcome(
                    disposition="accepted",
                    committed_source_seq=cmd.source_seq,
                    canonical_revision=self.canonical_revision,
                )
            else:
                self.chats[cmd.chat_id] = ModelChat(
                    chat_id=cmd.chat_id,
                    record_kind=cmd.record_kind,
                    platform_user_id=cmd.platform_user_id,
                    display_name=cmd.display_name,
                    updated_at=cmd_updated_at,
                    content_fingerprint=_model_hash(cmd.chat_content_dict()),
                    is_deleted=False,
                )
                self._invalidate_complete_coverage_for_chat(cmd.chat_id)
                self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
                self.checkpoints[key] = cmd.source_seq
                self.canonical_revision += 1
                return ModelTransitionOutcome(
                    disposition="accepted",
                    committed_source_seq=cmd.source_seq,
                    canonical_revision=self.canonical_revision,
                )

        elif isinstance(cmd, ModelMessageUpsertCommand):
            if ("message", cmd.message_id) in self._tombstones:
                self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
                self.checkpoints[key] = cmd.source_seq
                return ModelTransitionOutcome(
                    disposition="accepted",
                    committed_source_seq=cmd.source_seq,
                    canonical_revision=None,
                )

            parent = self.chats.get(cmd.chat_id)
            if parent is None or parent.is_deleted:
                return ModelTransitionOutcome(
                    disposition="rejected",
                    committed_source_seq=checkpoint,
                    code="invariant_failed",
                    error_message="message references an unknown or deleted chat",
                )

            existing = self.messages.get(cmd.message_id)
            msg_fp = _model_hash(cmd.message_content_dict())
            if existing is not None and not existing.is_deleted:
                if existing.content_fingerprint != msg_fp:
                    return ModelTransitionOutcome(
                        disposition="rejected",
                        committed_source_seq=checkpoint,
                        code="invariant_failed",
                        error_message="immutable message identifier has conflicting content",
                    )
                self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
                self.checkpoints[key] = cmd.source_seq
                return ModelTransitionOutcome(
                    disposition="accepted",
                    committed_source_seq=cmd.source_seq,
                    canonical_revision=None,
                )
            else:
                self.messages[cmd.message_id] = ModelMessage(
                    message_id=cmd.message_id,
                    chat_id=cmd.chat_id,
                    sender_platform_user_id=cmd.sender_platform_user_id,
                    text=cmd.text,
                    sent_at=_canonical_time(cmd.sent_at) or cmd.sent_at,
                    direction=cmd.direction,
                    content_fingerprint=msg_fp,
                    is_deleted=False,
                )
                self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
                self.checkpoints[key] = cmd.source_seq
                self.canonical_revision += 1
                return ModelTransitionOutcome(
                    disposition="accepted",
                    committed_source_seq=cmd.source_seq,
                    canonical_revision=self.canonical_revision,
                )

        elif isinstance(cmd, ModelChatDeleteCommand):
            self._tombstones.add(("chat", cmd.chat_id))
            if cmd.chat_id in self.chats:
                self.chats[cmd.chat_id].is_deleted = True
            for msg in self.messages.values():
                if msg.chat_id == cmd.chat_id:
                    msg.is_deleted = True
            self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
            self.checkpoints[key] = cmd.source_seq
            self.canonical_revision += 1
            return ModelTransitionOutcome(
                disposition="accepted",
                committed_source_seq=cmd.source_seq,
                canonical_revision=self.canonical_revision,
            )

        elif isinstance(cmd, ModelMessageDeleteCommand):
            existing = self.messages.get(cmd.message_id)
            if existing is not None and existing.chat_id != cmd.chat_id:
                return ModelTransitionOutcome(
                    disposition="rejected",
                    committed_source_seq=checkpoint,
                    code="invariant_failed",
                    error_message="message tombstone conflicts with canonical conversation",
                )
            if (
                ("message", cmd.message_id) in self._tombstones
                and self._message_tombstone_chats.get(cmd.message_id) not in (None, cmd.chat_id)
            ):
                return ModelTransitionOutcome(
                    disposition="rejected",
                    committed_source_seq=checkpoint,
                    code="invariant_failed",
                    error_message="tombstone identity was reused with a different conversation",
                )
            self._tombstones.add(("message", cmd.message_id))
            self._message_tombstone_chats[cmd.message_id] = cmd.chat_id
            if existing is not None:
                existing.is_deleted = True
            self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
            self.checkpoints[key] = cmd.source_seq
            self.canonical_revision += 1
            return ModelTransitionOutcome(
                disposition="accepted",
                committed_source_seq=cmd.source_seq,
                canonical_revision=self.canonical_revision,
            )

        elif isinstance(cmd, ModelCoverageObservedCommand):
            changed = self._apply_coverage({"type": cmd.coverage_type, **cmd.evidence_payload})
            self._record_event(key, cmd.event_id, cmd.source_seq, fingerprint)
            self.checkpoints[key] = cmd.source_seq
            if changed:
                self.canonical_revision += 1
            return ModelTransitionOutcome(
                disposition="accepted",
                committed_source_seq=cmd.source_seq,
                canonical_revision=self.canonical_revision if changed else None,
            )

        raise ValueError(f"Unknown command type {type(cmd)!r}")
