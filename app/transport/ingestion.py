"""Durable-ingestion sequencing and the replaceable repository boundary.

The in-memory repository is intentionally the only class that owns storage
mutation. Sequencing and validation live in :class:`IngestionService`, so a
database-backed repository can replace it without moving protocol rules into
the WebSocket endpoint.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from uuid import UUID


ZERO_ANALYTICS = {
    "total_conversations": 0,
    "total_messages": 0,
    "inbound_messages": 0,
    "outbound_messages": 0,
}


@dataclass(frozen=True, slots=True)
class StreamKey:
    creator_account_id: str
    agent_installation_id: UUID
    agent_stream_id: UUID


@dataclass(slots=True)
class StoredEvent:
    event_id: UUID
    source_seq: int
    fingerprint: str
    payload: dict[str, Any] | None = None


@dataclass(slots=True)
class StoredSnapshot:
    snapshot_id: UUID
    through_seq: int
    fingerprint: str


@dataclass(slots=True)
class RawStreamState:
    checkpoint: int = 0
    chats: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    events_by_id: dict[UUID, StoredEvent] = field(default_factory=dict)
    event_ids_by_seq: dict[int, UUID] = field(default_factory=dict)
    snapshots_by_id: dict[UUID, StoredSnapshot] = field(default_factory=dict)


@dataclass(slots=True)
class AccountReadModel:
    view_revision: int = 0
    conversations: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class CommitOutcome:
    status: Literal["accepted", "duplicate", "gap", "rejected"]
    committed_source_seq: int
    snapshot_id: UUID | None = None
    code: Literal["sequence_gap", "invariant_failed"] | None = None
    retryable: bool = False
    detail: str | None = None
    state_delta: dict[str, Any] | None = None


class IngestionRepository(Protocol):
    """Atomic storage operations needed by the sequencing service."""

    def checkpoint(self, key: StreamKey) -> int | None: ...

    def stream(self, key: StreamKey) -> RawStreamState | None: ...

    def account_read_model(self, creator_account_id: str) -> AccountReadModel: ...

    def commit_snapshot(
        self,
        key: StreamKey,
        *,
        expected_checkpoint: int,
        stream: RawStreamState,
        account: AccountReadModel,
    ) -> bool: ...

    def commit_delta(
        self,
        key: StreamKey,
        *,
        expected_checkpoint: int,
        stream: RawStreamState,
        account: AccountReadModel,
    ) -> bool: ...

    def reset(self) -> None: ...


class InMemoryIngestionRepository:
    """Copy-on-write repository whose two assignments form one commit unit."""

    def __init__(self) -> None:
        self._streams: dict[StreamKey, RawStreamState] = {}
        self._accounts: dict[str, AccountReadModel] = {}

    def checkpoint(self, key: StreamKey) -> int | None:
        state = self._streams.get(key)
        return None if state is None else state.checkpoint

    def stream(self, key: StreamKey) -> RawStreamState | None:
        state = self._streams.get(key)
        return deepcopy(state) if state is not None else None

    def account_read_model(self, creator_account_id: str) -> AccountReadModel:
        return deepcopy(self._accounts.get(creator_account_id, AccountReadModel()))

    def commit_snapshot(
        self,
        key: StreamKey,
        *,
        expected_checkpoint: int,
        stream: RawStreamState,
        account: AccountReadModel,
    ) -> bool:
        if stream.checkpoint < expected_checkpoint:
            raise ValueError("snapshot checkpoint cannot move backwards")
        return self._commit(key, expected_checkpoint, stream, account)

    def commit_delta(
        self,
        key: StreamKey,
        *,
        expected_checkpoint: int,
        stream: RawStreamState,
        account: AccountReadModel,
    ) -> bool:
        if stream.checkpoint != expected_checkpoint + 1:
            raise ValueError("delta checkpoint must advance contiguously")
        return self._commit(key, expected_checkpoint, stream, account)

    def _commit(
        self,
        key: StreamKey,
        expected_checkpoint: int,
        stream: RawStreamState,
        account: AccountReadModel,
    ) -> bool:
        current = self._streams.get(key)
        current_checkpoint = 0 if current is None else current.checkpoint
        if current_checkpoint != expected_checkpoint:
            return False
        current_account = self._accounts.get(key.creator_account_id, AccountReadModel())
        if account.view_revision not in {
            current_account.view_revision,
            current_account.view_revision + 1,
        }:
            return False
        if (
            account.view_revision == current_account.view_revision
            and account.conversations != current_account.conversations
        ):
            return False
        stored_stream = deepcopy(stream)
        stored_account = deepcopy(account)
        self._streams[key] = stored_stream
        self._accounts[key.creator_account_id] = stored_account
        return True

    def reset(self) -> None:
        self._streams.clear()
        self._accounts.clear()


class IngestionService:
    """ADR 0004 sequencing, deduplication, validation, and projection."""

    def __init__(self, repository: IngestionRepository) -> None:
        self.repository = repository
        self._locks: dict[StreamKey, asyncio.Lock] = {}

    def reset(self) -> None:
        self.repository.reset()
        self._locks.clear()

    def checkpoint(self, key: StreamKey) -> int | None:
        return self.repository.checkpoint(key)

    def state_snapshot_payload(self, creator_account_id: str) -> dict[str, Any]:
        account = self.repository.account_read_model(creator_account_id)
        return {
            "creator_account_id": creator_account_id,
            "view_revision": account.view_revision,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "conversations": [
                deepcopy(account.conversations[conversation_id])
                for conversation_id in sorted(account.conversations)
            ],
            # Protocol v1 requires this slice; this read model represents it
            # with the canonical empty analytics value.
            "analytics": dict(ZERO_ANALYTICS),
        }

    async def ingest_snapshot(self, key: StreamKey, payload: Any) -> CommitOutcome:
        async with self._locks.setdefault(key, asyncio.Lock()):
            current = self.repository.stream(key) or RawStreamState()
            fingerprint = _fingerprint(
                {
                    "through_seq": payload.through_seq,
                    "chats": payload.chats,
                    "messages": payload.messages,
                }
            )
            previous = current.snapshots_by_id.get(payload.snapshot_id)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    return _invariant_failure(
                        current.checkpoint,
                        "snapshot_id was reused with different snapshot content",
                        snapshot_id=payload.snapshot_id,
                    )
                return CommitOutcome(
                    status="duplicate",
                    committed_source_seq=current.checkpoint,
                    snapshot_id=payload.snapshot_id,
                )
            if payload.through_seq < current.checkpoint:
                return _invariant_failure(
                    current.checkpoint,
                    "Snapshot high-water mark is behind the committed checkpoint",
                    snapshot_id=payload.snapshot_id,
                )

            try:
                chats, messages = _validated_snapshot(payload.chats, payload.messages)
            except InvariantViolation as error:
                return _invariant_failure(
                    current.checkpoint, str(error), snapshot_id=payload.snapshot_id
                )

            replacement = deepcopy(current)
            replacement.checkpoint = payload.through_seq
            replacement.chats = chats
            replacement.messages = messages
            replacement.snapshots_by_id[payload.snapshot_id] = StoredSnapshot(
                snapshot_id=payload.snapshot_id,
                through_seq=payload.through_seq,
                fingerprint=fingerprint,
            )
            account = self.repository.account_read_model(key.creator_account_id)
            state_delta = _replace_projection(key.creator_account_id, account, replacement)
            if not self.repository.commit_snapshot(
                key,
                expected_checkpoint=current.checkpoint,
                stream=replacement,
                account=account,
            ):
                return CommitOutcome(
                    status="gap",
                    committed_source_seq=self.repository.checkpoint(key) or 0,
                    code="sequence_gap",
                    retryable=True,
                    detail="Checkpoint changed while committing the snapshot",
                    snapshot_id=payload.snapshot_id,
                )
            return CommitOutcome(
                status="accepted",
                committed_source_seq=replacement.checkpoint,
                snapshot_id=payload.snapshot_id,
                state_delta=state_delta,
            )

    async def ingest_delta(self, key: StreamKey, payload: Any) -> CommitOutcome:
        async with self._locks.setdefault(key, asyncio.Lock()):
            current = self.repository.stream(key)
            if current is None:
                return CommitOutcome(
                    status="gap",
                    committed_source_seq=0,
                    code="sequence_gap",
                    retryable=True,
                    detail="A complete snapshot is required before the first delta",
                )
            fingerprint = _fingerprint(
                {"source_seq": payload.source_seq, "change": payload.change}
            )
            previous = current.events_by_id.get(payload.event_id)
            if previous is not None:
                if previous.source_seq != payload.source_seq or previous.fingerprint != fingerprint:
                    return _invariant_failure(
                        current.checkpoint,
                        "event_id was reused with a different sequence or change",
                    )
                return CommitOutcome(
                    status="duplicate", committed_source_seq=current.checkpoint
                )

            if payload.source_seq <= current.checkpoint:
                previous_event_id = current.event_ids_by_seq.get(payload.source_seq)
                if previous_event_id is not None and previous_event_id != payload.event_id:
                    return _invariant_failure(
                        current.checkpoint,
                        "source_seq was already committed for a different event_id",
                    )
                # A snapshot covers all earlier source sequences without
                # retaining their individual event ids. Their resend is safe.
                return CommitOutcome(
                    status="duplicate", committed_source_seq=current.checkpoint
                )

            expected = current.checkpoint + 1
            if payload.source_seq != expected:
                return CommitOutcome(
                    status="gap",
                    committed_source_seq=current.checkpoint,
                    code="sequence_gap",
                    retryable=True,
                    detail=f"Expected source sequence {expected}",
                )

            replacement = deepcopy(current)
            try:
                _apply_raw_change(replacement, payload.change)
            except InvariantViolation as error:
                return _invariant_failure(current.checkpoint, str(error))
            replacement.checkpoint = payload.source_seq
            replacement.events_by_id[payload.event_id] = StoredEvent(
                event_id=payload.event_id,
                source_seq=payload.source_seq,
                fingerprint=fingerprint,
                payload=payload.change.model_dump(mode="json"),
            )
            replacement.event_ids_by_seq[payload.source_seq] = payload.event_id

            account = self.repository.account_read_model(key.creator_account_id)
            state_delta = _replace_projection(key.creator_account_id, account, replacement)
            if not self.repository.commit_delta(
                key,
                expected_checkpoint=current.checkpoint,
                stream=replacement,
                account=account,
            ):
                return CommitOutcome(
                    status="gap",
                    committed_source_seq=self.repository.checkpoint(key) or 0,
                    code="sequence_gap",
                    retryable=True,
                    detail=f"Expected source sequence {expected}",
                )
            return CommitOutcome(
                status="accepted",
                committed_source_seq=replacement.checkpoint,
                state_delta=state_delta,
            )


class InvariantViolation(ValueError):
    pass


def _invariant_failure(
    checkpoint: int, detail: str, *, snapshot_id: UUID | None = None
) -> CommitOutcome:
    return CommitOutcome(
        status="rejected",
        committed_source_seq=checkpoint,
        snapshot_id=snapshot_id,
        code="invariant_failed",
        retryable=False,
        detail=detail,
    )


def _fingerprint(value: Any) -> str:
    def default(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if isinstance(item, (datetime, UUID)):
            return str(item)
        raise TypeError(f"Cannot fingerprint {type(item)!r}")

    return json.dumps(value, default=default, sort_keys=True, separators=(",", ":"))


def _validated_snapshot(
    chat_models: list[Any], message_models: list[Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    chats: dict[str, dict[str, Any]] = {}
    for model in chat_models:
        chat = model.model_dump(mode="json")
        if chat["chat_id"] in chats:
            raise InvariantViolation(f"Duplicate chat_id {chat['chat_id']!r} in snapshot")
        chats[chat["chat_id"]] = chat
    messages: dict[str, dict[str, Any]] = {}
    for model in message_models:
        message = model.model_dump(mode="json")
        if message["message_id"] in messages:
            raise InvariantViolation(
                f"Duplicate message_id {message['message_id']!r} in snapshot"
            )
        if message["chat_id"] not in chats:
            raise InvariantViolation(
                f"Message {message['message_id']!r} references unknown chat_id"
            )
        messages[message["message_id"]] = message
    return chats, messages


def _apply_raw_change(stream: RawStreamState, change_model: Any) -> None:
    change = change_model.model_dump(mode="json")
    change_type = change["type"]
    if change_type == "chat.upsert":
        chat = change["chat"]
        stream.chats[chat["chat_id"]] = chat
        return
    if change_type == "chat.delete":
        chat_id = change["chat_id"]
        stream.chats.pop(chat_id, None)
        stream.messages = {
            message_id: message
            for message_id, message in stream.messages.items()
            if message["chat_id"] != chat_id
        }
        return
    if change_type == "message.upsert":
        message = change["message"]
        if message["chat_id"] not in stream.chats:
            raise InvariantViolation(
                f"Message {message['message_id']!r} references unknown chat_id"
            )
        stream.messages[message["message_id"]] = message
        return
    if change_type == "message.delete":
        chat_id = change["chat_id"]
        if chat_id not in stream.chats:
            raise InvariantViolation("Message deletion references unknown chat_id")
        existing = stream.messages.get(change["message_id"])
        if existing is not None and existing["chat_id"] != chat_id:
            raise InvariantViolation("Message deletion conflicts with the stored chat_id")
        stream.messages.pop(change["message_id"], None)
        return
    raise InvariantViolation(f"Unsupported raw change type {change_type!r}")


def _project(stream: RawStreamState) -> dict[str, dict[str, Any]]:
    messages_by_chat: dict[str, list[dict[str, Any]]] = {
        chat_id: [] for chat_id in stream.chats
    }
    for message in stream.messages.values():
        if message["chat_id"] in messages_by_chat:
            messages_by_chat[message["chat_id"]].append(message)
    conversations: dict[str, dict[str, Any]] = {}
    for chat_id, chat in stream.chats.items():
        raw_messages = sorted(
            messages_by_chat[chat_id],
            key=lambda item: (item["sent_at"], item["message_id"]),
        )
        conversations[chat_id] = {
            "conversation_id": chat_id,
            "platform_user_id": chat["platform_user_id"],
            "display_name": chat["display_name"],
            "unread_count": 0,
            "last_message_at": raw_messages[-1]["sent_at"] if raw_messages else None,
            "messages": [
                {
                    "message_id": message["message_id"],
                    "text": message["text"],
                    "sent_at": message["sent_at"],
                    "direction": message["direction"],
                    "sentiment": "unknown",
                }
                for message in raw_messages
            ],
        }
    return conversations


def _replace_projection(
    creator_account_id: str,
    account: AccountReadModel,
    stream: RawStreamState,
) -> dict[str, Any] | None:
    projected = _project(stream)
    changes: list[dict[str, Any]] = []
    for conversation_id in sorted(set(account.conversations) - set(projected)):
        changes.append(
            {"type": "conversation.delete", "conversation_id": conversation_id}
        )
    for conversation_id in sorted(projected):
        if account.conversations.get(conversation_id) != projected[conversation_id]:
            changes.append(
                {
                    "type": "conversation.upsert",
                    "conversation": deepcopy(projected[conversation_id]),
                }
            )
    account.conversations = projected
    if not changes:
        return None
    account.view_revision += 1
    return {
        "creator_account_id": creator_account_id,
        "view_revision": account.view_revision,
        "committed_at": datetime.now(timezone.utc).isoformat(),
        "changes": changes,
    }
