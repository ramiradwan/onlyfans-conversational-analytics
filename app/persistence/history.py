"""Authoritative protocol-v2 history ingestion and account-level canonical state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from app.persistence.database import CanonicalSQLite, LocalSQLite
from app.persistence.migrations import MigrationChecksumError, MigrationRunner
from app.persistence.projection_pipeline import (
    CanonicalProjectionConversation,
    CanonicalProjectionMessage,
    DeterministicProjectionPipeline,
    ProjectionPipeline,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _iso(value: datetime | str) -> str:
    if isinstance(value, str):
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.isoformat()


def _staged_chat(record: dict[str, Any]) -> tuple[Any, ...]:
    if record["tombstone"]:
        return (record["chat_id"], 1, None, None, None, None, None)
    chat = record["chat"]
    return (
        chat["chat_id"],
        0,
        chat["record_kind"],
        chat.get("platform_user_id"),
        chat.get("display_name"),
        chat.get("updated_at"),
        _hash(chat),
    )


def _staged_message(record: dict[str, Any]) -> tuple[Any, ...]:
    if record["tombstone"]:
        return (
            record["message_id"], record["chat_id"], 1,
            None, None, None, None, None, None,
        )
    message = dict(record["message"])
    message.pop("record_kind", None)
    return (
        message["message_id"],
        message["chat_id"],
        0,
        message["sender_platform_user_id"],
        message["text"],
        message["sent_at"],
        message["direction"],
        message.get("upstream_updated_at"),
        _hash(message),
    )


@dataclass(frozen=True, slots=True)
class StreamKey:
    creator_account_id: str
    agent_installation_id: UUID
    agent_stream_id: UUID

    def sql(self) -> tuple[str, str, str]:
        return (
            self.creator_account_id,
            str(self.agent_installation_id),
            str(self.agent_stream_id),
        )


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: Literal["accepted", "duplicate", "gap", "rejected"]
    committed_source_seq: int
    snapshot_id: UUID | None = None
    next_expected_chunk_index: int | None = None
    snapshot_committed: bool = False
    canonical_revision: int | None = None
    code: str | None = None
    retryable: bool = False
    detail: str | None = None


class InvariantViolation(ValueError):
    pass


class HistoryRepository:
    """All acknowledged source effects commit through this repository."""

    def __init__(self, database: CanonicalSQLite) -> None:
        self.database = database

    def reset(self) -> None:
        """Remove v2 history state while retaining configuration and command audit data."""
        with self.database.transaction() as connection:
            for table in (
                "projection_activation_intents",
                "projection_work",
                "live_ingest_state",
                "account_coverage_heads",
                "coverage_members",
                "coverage_generations",
                "entity_conflicts",
                "entity_tombstones",
                "account_messages",
                "account_chats",
                "committed_snapshots",
                "raw_ingest_events",
                "snapshot_coverage_records",
                "snapshot_message_records",
                "snapshot_chat_records",
                "snapshot_chunks",
                "snapshot_uploads",
                "stream_message_membership",
                "stream_chat_membership",
                "stream_epochs",
                "ingest_checkpoints",
                "ingest_streams",
                "history_settings",
                "account_heads",
            ):
                connection.execute(f"DELETE FROM {table}")

    def checkpoint(self, key: StreamKey) -> int | None:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT committed_source_seq FROM ingest_checkpoints
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?""",
                key.sql(),
            ).fetchone()
            return None if row is None else int(row[0])

    def pending_snapshot(self, key: StreamKey) -> tuple[UUID, int] | None:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT snapshot_id, next_chunk_index FROM snapshot_uploads
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                     AND state='staging' ORDER BY created_at DESC LIMIT 1""",
                key.sql(),
            ).fetchone()
            return None if row is None else (UUID(row[0]), int(row[1]))

    def account_has_pending_snapshot(self, account_id: str) -> bool:
        with self.database.read() as connection:
            return connection.execute(
                """SELECT 1 FROM snapshot_uploads
                    WHERE creator_account_id=? AND state='staging' LIMIT 1""",
                (account_id,),
            ).fetchone() is not None

    @staticmethod
    def _require_stream_identity(key: StreamKey, payload: Any) -> None:
        if (
            str(payload.creator_account_id) != key.creator_account_id
            or payload.agent_installation_id != key.agent_installation_id
            or payload.agent_stream_id != key.agent_stream_id
        ):
            raise InvariantViolation("ingest payload identity does not match the bound stream")

    @staticmethod
    def _ensure_account(connection: sqlite3.Connection, account_id: str, now: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO account_heads(creator_account_id, updated_at) VALUES (?, ?)",
            (account_id, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO account_coverage_heads
               (creator_account_id, coverage_revision, updated_at) VALUES (?, 0, ?)""",
            (account_id, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO history_settings(
                   creator_account_id, settings_revision, consent_policy_version,
                   consent_revision, authorized_platform_creator_id, desired_state,
                   effective_state, required_config_revision, effective_config_revision,
                   effective_settings_revision, recent_window_days, page_size,
                   pages_per_wake, request_interval_ms, retry_limit, updated_at
               ) VALUES (?,0,'history-consent-v1',NULL,NULL,'not_started',
                         'not_applied',NULL,NULL,NULL,30,100,2,500,3,?)""",
            (account_id, now),
        )

    @staticmethod
    def _ensure_stream(connection: sqlite3.Connection, key: StreamKey, now: str) -> int:
        HistoryRepository._ensure_account(connection, key.creator_account_id, now)
        connection.execute(
            """INSERT OR IGNORE INTO ingest_streams(
                   creator_account_id, agent_installation_id, agent_stream_id, created_at
               ) VALUES (?,?,?,?)""",
            (*key.sql(), now),
        )
        row = connection.execute(
            """SELECT stream_epoch FROM stream_epochs
               WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?""",
            key.sql(),
        ).fetchone()
        if row is None:
            epoch = int(
                connection.execute(
                    "SELECT COALESCE(MAX(stream_epoch),0)+1 FROM stream_epochs WHERE creator_account_id=?",
                    (key.creator_account_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """INSERT INTO stream_epochs(
                       creator_account_id,agent_installation_id,agent_stream_id,stream_epoch,activated_at
                   ) VALUES (?,?,?,?,?)""",
                (*key.sql(), epoch, now),
            )
            return epoch
        return int(row[0])

    @staticmethod
    def _current_checkpoint(connection: sqlite3.Connection, key: StreamKey) -> int | None:
        row = connection.execute(
            """SELECT committed_source_seq FROM ingest_checkpoints
               WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?""",
            key.sql(),
        ).fetchone()
        return None if row is None else int(row[0])

    @staticmethod
    def _advance_checkpoint(
        connection: sqlite3.Connection, key: StreamKey, sequence: int, now: str
    ) -> None:
        connection.execute(
            """INSERT INTO ingest_checkpoints(
                   creator_account_id,agent_installation_id,agent_stream_id,
                   committed_source_seq,committed_at
               ) VALUES (?,?,?,?,?)
               ON CONFLICT(creator_account_id,agent_installation_id,agent_stream_id)
               DO UPDATE SET committed_source_seq=excluded.committed_source_seq,
                             committed_at=excluded.committed_at""",
            (*key.sql(), sequence, now),
        )

    def begin_snapshot(self, key: StreamKey, payload: Any) -> IngestResult:
        self._require_stream_identity(key, payload)
        now = _iso(utc_now())
        fingerprint = _hash(
            {
                "through_seq": payload.through_seq,
                "chunk_count": payload.chunk_count,
                "record_counts": payload.record_counts.model_dump(mode="json"),
                "max_frame_bytes": payload.max_frame_bytes,
            }
        )
        with self.database.transaction() as connection:
            self._ensure_stream(connection, key, now)
            starting_checkpoint = self._current_checkpoint(connection, key)
            existing = connection.execute(
                """SELECT begin_fingerprint,next_chunk_index,state,through_seq FROM snapshot_uploads
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                     AND snapshot_id=?""",
                (*key.sql(), str(payload.snapshot_id)),
            ).fetchone()
            if existing is not None:
                if existing[0] != fingerprint:
                    return IngestResult(
                        "rejected", self._current_checkpoint(connection, key) or 0,
                        snapshot_id=payload.snapshot_id, code="chunk_conflict",
                        detail="snapshot begin was reused with different metadata",
                    )
                return IngestResult(
                    "duplicate", self._current_checkpoint(connection, key) or 0,
                    snapshot_id=payload.snapshot_id,
                    next_expected_chunk_index=int(existing[1]),
                    snapshot_committed=existing[2] == "committed",
                )
            pending = connection.execute(
                """SELECT snapshot_id FROM snapshot_uploads
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                     AND state='staging' LIMIT 1""",
                key.sql(),
            ).fetchone()
            if pending is not None:
                return IngestResult(
                    "rejected", self._current_checkpoint(connection, key) or 0,
                    snapshot_id=payload.snapshot_id, code="invariant_failed",
                    detail=f"snapshot {pending[0]} is already pending",
                )
            connection.execute(
                """INSERT INTO snapshot_uploads(
                       creator_account_id,agent_installation_id,agent_stream_id,snapshot_id,
                       starting_checkpoint,through_seq,chunk_count,expected_chats,expected_messages,
                       expected_coverage_evidence,begin_fingerprint,state,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'staging',?)""",
                (
                    *key.sql(), str(payload.snapshot_id), starting_checkpoint, payload.through_seq,
                    payload.chunk_count, payload.record_counts.chats,
                    payload.record_counts.messages,
                    payload.record_counts.coverage_evidence, fingerprint, now,
                ),
            )
            return IngestResult(
                "accepted", self._current_checkpoint(connection, key) or 0,
                snapshot_id=payload.snapshot_id, next_expected_chunk_index=0,
            )

    def add_snapshot_chunk(self, key: StreamKey, payload: Any) -> IngestResult:
        self._require_stream_identity(key, payload)
        if len(_json(payload.model_dump(mode="json")).encode("utf-8")) > 512 * 1024:
            raise InvariantViolation("snapshot frame exceeds 512 KiB")
        now = _iso(utc_now())
        records = payload.records
        fingerprint = _hash({"entity_kind": payload.entity_kind, "records": records})
        with self.database.transaction() as connection:
            upload = connection.execute(
                """SELECT chunk_count,next_chunk_index,state,last_entity_kind FROM snapshot_uploads
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                     AND snapshot_id=?""",
                (*key.sql(), str(payload.snapshot_id)),
            ).fetchone()
            checkpoint = self._current_checkpoint(connection, key) or 0
            if upload is None:
                return IngestResult("rejected", checkpoint, snapshot_id=payload.snapshot_id,
                                    code="snapshot_incomplete", detail="snapshot begin is missing")
            existing = connection.execute(
                """SELECT fingerprint FROM snapshot_chunks
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                     AND snapshot_id=? AND chunk_index=?""",
                (*key.sql(), str(payload.snapshot_id), payload.chunk_index),
            ).fetchone()
            if existing is not None:
                if existing[0] != fingerprint:
                    return IngestResult("rejected", checkpoint, snapshot_id=payload.snapshot_id,
                                        code="chunk_conflict", detail="chunk index was reused with different records")
                return IngestResult("duplicate", checkpoint, snapshot_id=payload.snapshot_id,
                                    next_expected_chunk_index=int(upload[1]),
                                    snapshot_committed=upload[2] == "committed")
            if upload[2] == "committed":
                return IngestResult("rejected", checkpoint, snapshot_id=payload.snapshot_id,
                                    next_expected_chunk_index=int(upload[1]),
                                    snapshot_committed=True, code="chunk_conflict",
                                    detail="committed snapshot has no matching chunk fingerprint")
            if payload.chunk_index != int(upload[1]) or payload.chunk_index >= int(upload[0]):
                return IngestResult("gap", checkpoint, snapshot_id=payload.snapshot_id,
                                    next_expected_chunk_index=int(upload[1]), code="sequence_gap",
                                    retryable=True, detail=f"expected snapshot chunk {upload[1]}")
            kind_order = {"chat": 0, "message": 1, "coverage_evidence": 2}
            if upload[3] is not None and kind_order[payload.entity_kind] < kind_order[str(upload[3])]:
                return IngestResult(
                    "rejected",
                    checkpoint,
                    snapshot_id=payload.snapshot_id,
                    next_expected_chunk_index=int(upload[1]),
                    code="invariant_failed",
                    detail="snapshot chunks must be ordered chat, message, coverage_evidence",
                )
            connection.execute(
                """INSERT INTO snapshot_chunks(
                       creator_account_id,agent_installation_id,agent_stream_id,snapshot_id,
                       chunk_index,entity_kind,record_count,fingerprint,committed_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (*key.sql(), str(payload.snapshot_id), payload.chunk_index,
                 payload.entity_kind, len(records), fingerprint, now),
            )
            try:
                if payload.entity_kind == "chat":
                    connection.executemany(
                        """INSERT INTO snapshot_chat_records(
                               creator_account_id,agent_installation_id,agent_stream_id,snapshot_id,
                               chat_id,chunk_index,record_json,is_tombstone,record_kind,
                               platform_user_id,display_name,upstream_updated_at,content_hash
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        [
                            (
                                *key.sql(),
                                str(payload.snapshot_id),
                                staged[0],
                                payload.chunk_index,
                                _json(record),
                                *staged[1:],
                            )
                            for record in records
                            for staged in (_staged_chat(record),)
                        ],
                    )
                elif payload.entity_kind == "message":
                    connection.executemany(
                        """INSERT INTO snapshot_message_records(
                               creator_account_id,agent_installation_id,agent_stream_id,snapshot_id,
                               message_id,chat_id,chunk_index,record_json,is_tombstone,
                               sender_platform_user_id,text,sent_at,direction,
                               upstream_updated_at,content_hash
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        [
                            (
                                *key.sql(),
                                str(payload.snapshot_id),
                                staged[0],
                                staged[1],
                                payload.chunk_index,
                                _json(record),
                                *staged[2:],
                            )
                            for record in records
                            for staged in (_staged_message(record),)
                        ],
                    )
                else:
                    connection.executemany(
                        """INSERT INTO snapshot_coverage_records(
                               creator_account_id,agent_installation_id,agent_stream_id,snapshot_id,
                               evidence_id,chunk_index,record_index,record_json
                           ) VALUES (?,?,?,?,?,?,?,?)""",
                        [(*key.sql(), str(payload.snapshot_id), _hash(record),
                          payload.chunk_index, index, _json(record))
                         for index, record in enumerate(records)],
                    )
            except sqlite3.IntegrityError as error:
                raise InvariantViolation("snapshot contains a duplicate entity identifier") from error
            counts_column = {
                "chat": "received_chats",
                "message": "received_messages",
                "coverage_evidence": "received_coverage_evidence",
            }[payload.entity_kind]
            connection.execute(
                f"""UPDATE snapshot_uploads
                    SET next_chunk_index=next_chunk_index+1,
                        last_entity_kind=?,
                        {counts_column}={counts_column}+?
                    WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                      AND snapshot_id=?""",
                (payload.entity_kind, len(records), *key.sql(), str(payload.snapshot_id)),
            )
            return IngestResult("accepted", checkpoint, snapshot_id=payload.snapshot_id,
                                next_expected_chunk_index=payload.chunk_index + 1)

    @staticmethod
    def _canonical_revision(connection: sqlite3.Connection, account_id: str, now: str) -> int:
        row = connection.execute(
            """UPDATE account_heads SET canonical_revision=canonical_revision+1, updated_at=?
               WHERE creator_account_id=? RETURNING canonical_revision""",
            (now, account_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("account head is missing")
        return int(row[0])

    @staticmethod
    def _tombstoned(connection: sqlite3.Connection, account_id: str, kind: str, entity_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM entity_tombstones WHERE creator_account_id=? AND entity_kind=? AND entity_id=?",
            (account_id, kind, entity_id),
        ).fetchone() is not None

    def _merge_chat(
        self, connection: sqlite3.Connection, account_id: str, chat: dict[str, Any],
        epoch: int, source_seq: int, event_id: str | None, now: str,
    ) -> bool:
        if self._tombstoned(connection, account_id, "chat", chat["chat_id"]):
            return False
        content_hash = _hash(chat)
        row = connection.execute(
            """SELECT record_kind,platform_user_id,upstream_updated_at,content_hash,is_deleted
               FROM account_chats
               WHERE creator_account_id=? AND chat_id=?""",
            (account_id, chat["chat_id"]),
        ).fetchone()
        incoming_full = chat["record_kind"] == "full"
        should_write = row is None
        if row is not None:
            if row[4]:
                return False
            if row[3] == content_hash:
                return False
            incoming_platform_id = chat.get("platform_user_id")
            if row[1] is not None and incoming_platform_id is not None and row[1] != incoming_platform_id:
                raise InvariantViolation("chat platform identity conflicts with canonical identity")
            if row[0] == "full" and not incoming_full:
                return False
            if row[0] == "placeholder" and incoming_full:
                should_write = True
            elif incoming_full:
                existing_time = datetime.fromisoformat(row[2])
                incoming_time = datetime.fromisoformat(chat["updated_at"])
                if incoming_time < existing_time:
                    return False
                if incoming_time == existing_time:
                    raise InvariantViolation("chat version has conflicting content")
                should_write = True
            else:
                return False
        if should_write:
            connection.execute(
                """INSERT INTO account_chats(
                       creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                       upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                       winning_event_id,is_deleted,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)
                   ON CONFLICT(creator_account_id,chat_id) DO UPDATE SET
                       record_kind=excluded.record_kind,platform_user_id=excluded.platform_user_id,
                       display_name=excluded.display_name,upstream_updated_at=excluded.upstream_updated_at,
                       content_hash=excluded.content_hash,winning_stream_epoch=excluded.winning_stream_epoch,
                       winning_source_seq=excluded.winning_source_seq,winning_event_id=excluded.winning_event_id,
                       is_deleted=0,updated_at=excluded.updated_at""",
                (account_id, chat["chat_id"], chat["record_kind"], chat.get("platform_user_id"),
                 chat.get("display_name"), chat.get("updated_at"), content_hash, epoch, source_seq,
                 event_id, now),
            )
            return True
        return False

    def _merge_message(
        self, connection: sqlite3.Connection, account_id: str, message: dict[str, Any],
        epoch: int, source_seq: int, event_id: str | None, now: str,
    ) -> bool:
        if self._tombstoned(connection, account_id, "message", message["message_id"]):
            return False
        parent = connection.execute(
            "SELECT is_deleted FROM account_chats WHERE creator_account_id=? AND chat_id=?",
            (account_id, message["chat_id"]),
        ).fetchone()
        if parent is None or parent[0]:
            raise InvariantViolation("message references an unknown or deleted chat")
        clean = dict(message)
        clean.pop("record_kind", None)
        content_hash = _hash(clean)
        row = connection.execute(
            """SELECT content_hash,upstream_updated_at,is_deleted FROM account_messages
               WHERE creator_account_id=? AND message_id=?""",
            (account_id, message["message_id"]),
        ).fetchone()
        if row is not None:
            if row[2] or row[0] == content_hash:
                return False
            raise InvariantViolation("immutable message identifier has conflicting content")
        connection.execute(
            """INSERT INTO account_messages(
                   creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,direction,
                   upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                   winning_event_id,is_deleted,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)
               ON CONFLICT(creator_account_id,message_id) DO UPDATE SET
                   chat_id=excluded.chat_id,sender_platform_user_id=excluded.sender_platform_user_id,
                   text=excluded.text,sent_at=excluded.sent_at,direction=excluded.direction,
                   upstream_updated_at=excluded.upstream_updated_at,content_hash=excluded.content_hash,
                   winning_stream_epoch=excluded.winning_stream_epoch,
                   winning_source_seq=excluded.winning_source_seq,
                   winning_event_id=excluded.winning_event_id,is_deleted=0,updated_at=excluded.updated_at""",
            (account_id, message["message_id"], message["chat_id"],
             message["sender_platform_user_id"], message["text"], message["sent_at"],
             message["direction"], message.get("upstream_updated_at"), content_hash,
             epoch, source_seq, event_id, now),
        )
        return True

    @staticmethod
    def _invalidate_complete_coverage_for_conversation(
        connection: sqlite3.Connection,
        account_id: str,
        conversation_id: str,
        now: str,
    ) -> bool:
        generation = connection.execute(
            """SELECT g.generation_id,g.state
                 FROM account_coverage_heads h
                 JOIN coverage_generations g
                   ON g.creator_account_id=h.creator_account_id
                  AND g.generation_id=h.active_generation_id
                WHERE h.creator_account_id=?""",
            (account_id,),
        ).fetchone()
        if generation is None or generation[1] != "complete":
            return False
        member = connection.execute(
            """SELECT 1 FROM coverage_members
                WHERE creator_account_id=? AND generation_id=? AND conversation_id=?""",
            (account_id, generation[0], conversation_id),
        ).fetchone()
        if member is not None:
            return False
        connection.execute(
            """UPDATE coverage_generations
                  SET state='superseded',reason_code='new_conversation_discovered'
                WHERE creator_account_id=? AND generation_id=? AND state='complete'""",
            (account_id, generation[0]),
        )
        connection.execute(
            """UPDATE account_coverage_heads
                  SET coverage_revision=coverage_revision+1,updated_at=?
                WHERE creator_account_id=?""",
            (now, account_id),
        )
        return True

    @staticmethod
    def _delete_entity(
        connection: sqlite3.Connection, account_id: str, kind: str, entity_id: str,
        chat_id: str | None, epoch: int, source_seq: int, event_id: str | None, now: str,
    ) -> bool:
        if kind == "message":
            canonical_message = connection.execute(
                """SELECT chat_id FROM account_messages
                   WHERE creator_account_id=? AND message_id=?""",
                (account_id, entity_id),
            ).fetchone()
            if canonical_message is not None and canonical_message[0] != chat_id:
                raise InvariantViolation("message tombstone conflicts with canonical conversation")
        existing = connection.execute(
            "SELECT chat_id FROM entity_tombstones WHERE creator_account_id=? AND entity_kind=? AND entity_id=?",
            (account_id, kind, entity_id),
        ).fetchone()
        if existing is not None and existing[0] != chat_id:
            raise InvariantViolation("tombstone identity was reused with a different conversation")
        connection.execute(
            """INSERT OR IGNORE INTO entity_tombstones(
                   creator_account_id,entity_kind,entity_id,chat_id,stream_epoch,source_seq,event_id,deleted_at
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (account_id, kind, entity_id, chat_id, epoch, source_seq, event_id, now),
        )
        if kind == "chat":
            connection.execute(
                "UPDATE account_chats SET is_deleted=1,updated_at=? WHERE creator_account_id=? AND chat_id=?",
                (now, account_id, entity_id),
            )
            connection.execute(
                "UPDATE account_messages SET is_deleted=1,updated_at=? WHERE creator_account_id=? AND chat_id=?",
                (now, account_id, entity_id),
            )
        else:
            connection.execute(
                "UPDATE account_messages SET is_deleted=1,updated_at=? WHERE creator_account_id=? AND message_id=?",
                (now, account_id, entity_id),
            )
        return existing is None

    @staticmethod
    def _merge_staged_snapshot_entities(
        connection: sqlite3.Connection,
        key: StreamKey,
        snapshot_id: str,
        *,
        epoch: int,
        source_seq: int,
        now: str,
    ) -> bool:
        """Validate and merge one staged snapshot with set-based SQLite work."""
        scope = (*key.sql(), snapshot_id)
        account_id = key.creator_account_id
        invalid_chat = connection.execute(
            """SELECT 1 FROM snapshot_chat_records
               WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                 AND snapshot_id=?
                 AND (is_tombstone IS NULL OR (
                     is_tombstone=0 AND (
                         record_kind IS NULL OR content_hash IS NULL OR (
                             record_kind='full' AND (
                                 platform_user_id IS NULL OR upstream_updated_at IS NULL
                             )
                         )
                     )
                 )) LIMIT 1""",
            scope,
        ).fetchone()
        invalid_message = connection.execute(
            """SELECT 1 FROM snapshot_message_records
               WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                 AND snapshot_id=?
                 AND (is_tombstone IS NULL OR (
                     is_tombstone=0 AND (
                         sender_platform_user_id IS NULL OR text IS NULL OR sent_at IS NULL
                         OR direction IS NULL OR content_hash IS NULL
                     )
                 )) LIMIT 1""",
            scope,
        ).fetchone()
        if invalid_chat is not None or invalid_message is not None:
            raise InvariantViolation("snapshot staging material is incomplete or obsolete")

        platform_conflict = connection.execute(
            """SELECT 1
                 FROM snapshot_chat_records s
                 JOIN account_chats c
                   ON c.creator_account_id=s.creator_account_id AND c.chat_id=s.chat_id
                WHERE s.creator_account_id=? AND s.agent_installation_id=?
                  AND s.agent_stream_id=? AND s.snapshot_id=? AND s.is_tombstone=0
                  AND c.is_deleted=0 AND c.platform_user_id IS NOT NULL
                  AND s.platform_user_id IS NOT NULL
                  AND c.platform_user_id<>s.platform_user_id
                LIMIT 1""",
            scope,
        ).fetchone()
        if platform_conflict is not None:
            raise InvariantViolation("chat platform identity conflicts with canonical identity")
        chat_version_conflict = connection.execute(
            """SELECT 1
                 FROM snapshot_chat_records s
                 JOIN account_chats c
                   ON c.creator_account_id=s.creator_account_id AND c.chat_id=s.chat_id
                WHERE s.creator_account_id=? AND s.agent_installation_id=?
                  AND s.agent_stream_id=? AND s.snapshot_id=? AND s.is_tombstone=0
                  AND c.is_deleted=0 AND c.record_kind='full' AND s.record_kind='full'
                  AND c.upstream_updated_at=s.upstream_updated_at
                  AND c.content_hash<>s.content_hash
                LIMIT 1""",
            scope,
        ).fetchone()
        if chat_version_conflict is not None:
            raise InvariantViolation("chat version has conflicting content")

        connection.execute("DROP TABLE IF EXISTS temp.snapshot_changed_chats")
        connection.execute(
            "CREATE TEMP TABLE snapshot_changed_chats(chat_id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.execute(
            """INSERT INTO snapshot_changed_chats(chat_id)
               SELECT s.chat_id
                 FROM snapshot_chat_records s
                 LEFT JOIN account_chats c
                   ON c.creator_account_id=s.creator_account_id AND c.chat_id=s.chat_id
                 LEFT JOIN entity_tombstones t
                   ON t.creator_account_id=s.creator_account_id
                  AND t.entity_kind='chat' AND t.entity_id=s.chat_id
                WHERE s.creator_account_id=? AND s.agent_installation_id=?
                  AND s.agent_stream_id=? AND s.snapshot_id=? AND s.is_tombstone=0
                  AND t.entity_id IS NULL
                  AND (
                      c.chat_id IS NULL OR (
                          c.is_deleted=0 AND c.content_hash<>s.content_hash AND (
                              (c.record_kind='placeholder' AND s.record_kind='full') OR
                              (c.record_kind='full' AND s.record_kind='full'
                               AND s.upstream_updated_at>c.upstream_updated_at)
                          )
                      )
                  )""",
            scope,
        )
        changed_chat_count = int(
            connection.execute("SELECT COUNT(*) FROM snapshot_changed_chats").fetchone()[0]
        )

        chat_tombstones = connection.execute(
            """INSERT OR IGNORE INTO entity_tombstones(
                   creator_account_id,entity_kind,entity_id,chat_id,stream_epoch,
                   source_seq,event_id,deleted_at
               )
               SELECT ?, 'chat', chat_id, chat_id, ?, ?, NULL, ?
                 FROM snapshot_chat_records
                WHERE creator_account_id=? AND agent_installation_id=?
                  AND agent_stream_id=? AND snapshot_id=? AND is_tombstone=1""",
            (account_id, epoch, source_seq, now, *scope),
        ).rowcount
        connection.execute(
            """UPDATE account_chats SET is_deleted=1,updated_at=?
                WHERE creator_account_id=? AND chat_id IN (
                    SELECT chat_id FROM snapshot_chat_records
                     WHERE creator_account_id=? AND agent_installation_id=?
                       AND agent_stream_id=? AND snapshot_id=? AND is_tombstone=1
                )""",
            (now, account_id, *scope),
        )
        connection.execute(
            """UPDATE account_messages SET is_deleted=1,updated_at=?
                WHERE creator_account_id=? AND chat_id IN (
                    SELECT chat_id FROM snapshot_chat_records
                     WHERE creator_account_id=? AND agent_installation_id=?
                       AND agent_stream_id=? AND snapshot_id=? AND is_tombstone=1
                )""",
            (now, account_id, *scope),
        )
        connection.execute(
            """INSERT INTO account_chats(
                   creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                   upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                   winning_event_id,is_deleted,updated_at
               )
               SELECT ?,s.chat_id,s.record_kind,s.platform_user_id,s.display_name,
                      s.upstream_updated_at,s.content_hash,?,?,NULL,0,?
                 FROM snapshot_chat_records s
                 JOIN snapshot_changed_chats x ON x.chat_id=s.chat_id
                WHERE s.creator_account_id=? AND s.agent_installation_id=?
                  AND s.agent_stream_id=? AND s.snapshot_id=?
               ON CONFLICT(creator_account_id,chat_id) DO UPDATE SET
                   record_kind=excluded.record_kind,
                   platform_user_id=excluded.platform_user_id,
                   display_name=excluded.display_name,
                   upstream_updated_at=excluded.upstream_updated_at,
                   content_hash=excluded.content_hash,
                   winning_stream_epoch=excluded.winning_stream_epoch,
                   winning_source_seq=excluded.winning_source_seq,
                   winning_event_id=NULL,is_deleted=0,updated_at=excluded.updated_at""",
            (account_id, epoch, source_seq, now, *scope),
        )

        coverage_invalidated = connection.execute(
            """UPDATE coverage_generations
                  SET state='superseded',reason_code='new_conversation_discovered'
                WHERE creator_account_id=? AND state='complete'
                  AND generation_id=(
                      SELECT active_generation_id FROM account_coverage_heads
                       WHERE creator_account_id=?
                  )
                  AND EXISTS (
                      SELECT 1 FROM snapshot_changed_chats x
                       WHERE NOT EXISTS (
                           SELECT 1 FROM coverage_members m
                            WHERE m.creator_account_id=?
                              AND m.generation_id=coverage_generations.generation_id
                              AND m.conversation_id=x.chat_id
                       )
                  )""",
            (account_id, account_id, account_id),
        ).rowcount
        if coverage_invalidated:
            connection.execute(
                """UPDATE account_coverage_heads
                      SET coverage_revision=coverage_revision+1,updated_at=?
                    WHERE creator_account_id=?""",
                (now, account_id),
            )

        tombstone_parent_conflict = connection.execute(
            """SELECT 1
                 FROM snapshot_message_records s
                 JOIN account_messages m
                   ON m.creator_account_id=s.creator_account_id AND m.message_id=s.message_id
                WHERE s.creator_account_id=? AND s.agent_installation_id=?
                  AND s.agent_stream_id=? AND s.snapshot_id=? AND s.is_tombstone=1
                  AND m.chat_id<>s.chat_id LIMIT 1""",
            scope,
        ).fetchone()
        prior_tombstone_conflict = connection.execute(
            """SELECT 1
                 FROM snapshot_message_records s
                 JOIN entity_tombstones t
                   ON t.creator_account_id=s.creator_account_id
                  AND t.entity_kind='message' AND t.entity_id=s.message_id
                WHERE s.creator_account_id=? AND s.agent_installation_id=?
                  AND s.agent_stream_id=? AND s.snapshot_id=? AND s.is_tombstone=1
                  AND t.chat_id<>s.chat_id LIMIT 1""",
            scope,
        ).fetchone()
        if tombstone_parent_conflict is not None or prior_tombstone_conflict is not None:
            raise InvariantViolation("message tombstone conflicts with canonical conversation")

        orphan = connection.execute(
            """SELECT 1
                 FROM snapshot_message_records s
                 LEFT JOIN entity_tombstones t
                   ON t.creator_account_id=s.creator_account_id
                  AND t.entity_kind='message' AND t.entity_id=s.message_id
                 LEFT JOIN account_chats c
                   ON c.creator_account_id=s.creator_account_id AND c.chat_id=s.chat_id
                WHERE s.creator_account_id=? AND s.agent_installation_id=?
                  AND s.agent_stream_id=? AND s.snapshot_id=? AND s.is_tombstone=0
                  AND t.entity_id IS NULL AND (c.chat_id IS NULL OR c.is_deleted=1)
                LIMIT 1""",
            scope,
        ).fetchone()
        if orphan is not None:
            raise InvariantViolation("message references an unknown or deleted chat")
        immutable_conflict = connection.execute(
            """SELECT 1
                 FROM snapshot_message_records s
                 JOIN account_messages m
                   ON m.creator_account_id=s.creator_account_id AND m.message_id=s.message_id
                 LEFT JOIN entity_tombstones t
                   ON t.creator_account_id=s.creator_account_id
                  AND t.entity_kind='message' AND t.entity_id=s.message_id
                WHERE s.creator_account_id=? AND s.agent_installation_id=?
                  AND s.agent_stream_id=? AND s.snapshot_id=? AND s.is_tombstone=0
                  AND t.entity_id IS NULL AND m.is_deleted=0 AND m.content_hash<>s.content_hash
                LIMIT 1""",
            scope,
        ).fetchone()
        if immutable_conflict is not None:
            raise InvariantViolation("immutable message identifier has conflicting content")

        message_inserts = connection.execute(
            """INSERT INTO account_messages(
                   creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                   direction,upstream_updated_at,content_hash,winning_stream_epoch,
                   winning_source_seq,winning_event_id,is_deleted,updated_at
               )
               SELECT ?,s.message_id,s.chat_id,s.sender_platform_user_id,s.text,s.sent_at,
                      s.direction,s.upstream_updated_at,s.content_hash,?,?,NULL,0,?
                 FROM snapshot_message_records s
                 LEFT JOIN account_messages m
                   ON m.creator_account_id=s.creator_account_id AND m.message_id=s.message_id
                 LEFT JOIN entity_tombstones t
                   ON t.creator_account_id=s.creator_account_id
                  AND t.entity_kind='message' AND t.entity_id=s.message_id
                WHERE s.creator_account_id=? AND s.agent_installation_id=?
                  AND s.agent_stream_id=? AND s.snapshot_id=? AND s.is_tombstone=0
                  AND m.message_id IS NULL AND t.entity_id IS NULL""",
            (account_id, epoch, source_seq, now, *scope),
        ).rowcount
        message_tombstones = connection.execute(
            """INSERT OR IGNORE INTO entity_tombstones(
                   creator_account_id,entity_kind,entity_id,chat_id,stream_epoch,
                   source_seq,event_id,deleted_at
               )
               SELECT ?, 'message', message_id, chat_id, ?, ?, NULL, ?
                 FROM snapshot_message_records
                WHERE creator_account_id=? AND agent_installation_id=?
                  AND agent_stream_id=? AND snapshot_id=? AND is_tombstone=1""",
            (account_id, epoch, source_seq, now, *scope),
        ).rowcount
        connection.execute(
            """UPDATE account_messages SET is_deleted=1,updated_at=?
                WHERE creator_account_id=? AND message_id IN (
                    SELECT message_id FROM snapshot_message_records
                     WHERE creator_account_id=? AND agent_installation_id=?
                       AND agent_stream_id=? AND snapshot_id=? AND is_tombstone=1
                )""",
            (now, account_id, *scope),
        )
        return any(
            (
                changed_chat_count,
                chat_tombstones,
                coverage_invalidated,
                message_inserts,
                message_tombstones,
            )
        )

    def _record_entity_conflict(
        self,
        key: StreamKey,
        *,
        entity_kind: Literal["chat", "message"],
        entity_id: str,
        incoming_hash: str,
        source_seq: int,
        reason: str,
    ) -> None:
        table = "account_chats" if entity_kind == "chat" else "account_messages"
        id_column = "chat_id" if entity_kind == "chat" else "message_id"
        now = _iso(utc_now())
        with self.database.transaction() as connection:
            existing = connection.execute(
                f"""SELECT content_hash FROM {table}
                    WHERE creator_account_id=? AND {id_column}=?""",
                (key.creator_account_id, entity_id),
            ).fetchone()
            epoch = connection.execute(
                """SELECT stream_epoch FROM stream_epochs
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?""",
                key.sql(),
            ).fetchone()
            if existing is None or existing[0] == incoming_hash:
                return
            connection.execute(
                """INSERT OR IGNORE INTO entity_conflicts(
                       creator_account_id,entity_kind,entity_id,existing_hash,incoming_hash,
                       stream_epoch,source_seq,observed_at,reason
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    key.creator_account_id,
                    entity_kind,
                    entity_id,
                    existing[0],
                    incoming_hash,
                    0 if epoch is None else int(epoch[0]),
                    source_seq,
                    now,
                    reason,
                ),
            )

    def _record_delta_conflict(self, key: StreamKey, payload: Any, reason: str) -> None:
        change = payload.model_dump(mode="json")["change"]
        if change["type"] == "chat.upsert":
            entity_kind: Literal["chat", "message"] = "chat"
            material = change["chat"]
            entity_id = material["chat_id"]
        elif change["type"] == "message.upsert":
            entity_kind = "message"
            material = dict(change["message"])
            material.pop("record_kind", None)
            entity_id = material["message_id"]
        else:
            return
        self._record_entity_conflict(
            key,
            entity_kind=entity_kind,
            entity_id=entity_id,
            incoming_hash=_hash(material),
            source_seq=int(payload.source_seq),
            reason=reason,
        )

    def _record_snapshot_conflicts(
        self, key: StreamKey, snapshot_id: str, reason: str
    ) -> None:
        now = _iso(utc_now())
        with self.database.transaction() as connection:
            upload = connection.execute(
                """SELECT through_seq FROM snapshot_uploads
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                     AND snapshot_id=?""",
                (*key.sql(), snapshot_id),
            ).fetchone()
            epoch = connection.execute(
                """SELECT stream_epoch FROM stream_epochs
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?""",
                key.sql(),
            ).fetchone()
            if upload is None:
                return
            common = (
                key.creator_account_id,
                0 if epoch is None else int(epoch[0]),
                int(upload[0]),
                now,
                reason,
                *key.sql(),
                snapshot_id,
            )
            connection.execute(
                """INSERT OR IGNORE INTO entity_conflicts(
                       creator_account_id,entity_kind,entity_id,existing_hash,incoming_hash,
                       stream_epoch,source_seq,observed_at,reason
                   )
                   SELECT ?, 'chat', s.chat_id, c.content_hash, s.content_hash, ?, ?, ?, ?
                     FROM snapshot_chat_records s
                     JOIN account_chats c
                       ON c.creator_account_id=s.creator_account_id AND c.chat_id=s.chat_id
                    WHERE s.creator_account_id=? AND s.agent_installation_id=?
                      AND s.agent_stream_id=? AND s.snapshot_id=? AND s.is_tombstone=0
                      AND c.is_deleted=0 AND c.content_hash<>s.content_hash
                      AND (
                          (c.platform_user_id IS NOT NULL AND s.platform_user_id IS NOT NULL
                           AND c.platform_user_id<>s.platform_user_id) OR
                          (c.record_kind='full' AND s.record_kind='full'
                           AND c.upstream_updated_at=s.upstream_updated_at)
                      )""",
                common,
            )
            connection.execute(
                """INSERT OR IGNORE INTO entity_conflicts(
                       creator_account_id,entity_kind,entity_id,existing_hash,incoming_hash,
                       stream_epoch,source_seq,observed_at,reason
                   )
                   SELECT ?, 'message', s.message_id, m.content_hash, s.content_hash, ?, ?, ?, ?
                     FROM snapshot_message_records s
                     JOIN account_messages m
                       ON m.creator_account_id=s.creator_account_id AND m.message_id=s.message_id
                     LEFT JOIN entity_tombstones t
                       ON t.creator_account_id=s.creator_account_id
                      AND t.entity_kind='message' AND t.entity_id=s.message_id
                    WHERE s.creator_account_id=? AND s.agent_installation_id=?
                      AND s.agent_stream_id=? AND s.snapshot_id=? AND s.is_tombstone=0
                      AND t.entity_id IS NULL AND m.is_deleted=0
                      AND m.content_hash<>s.content_hash""",
                common,
            )

    def commit_snapshot(self, key: StreamKey, payload: Any) -> IngestResult:
        try:
            return self._commit_snapshot(key, payload)
        except InvariantViolation as error:
            self._record_snapshot_conflicts(key, str(payload.snapshot_id), str(error))
            raise

    def _commit_snapshot(self, key: StreamKey, payload: Any) -> IngestResult:
        self._require_stream_identity(key, payload)
        now = _iso(utc_now())
        with self.database.transaction() as connection:
            upload = connection.execute(
                """SELECT through_seq,chunk_count,next_chunk_index,expected_chats,expected_messages,
                          expected_coverage_evidence,received_chats,received_messages,
                          received_coverage_evidence,state,starting_checkpoint
                   FROM snapshot_uploads WHERE creator_account_id=? AND agent_installation_id=?
                     AND agent_stream_id=? AND snapshot_id=?""",
                (*key.sql(), str(payload.snapshot_id)),
            ).fetchone()
            checkpoint = self._current_checkpoint(connection, key) or 0
            if upload is None:
                return IngestResult("rejected", checkpoint, snapshot_id=payload.snapshot_id,
                                    code="snapshot_incomplete", detail="snapshot begin is missing")
            if int(payload.chunk_count) != int(upload[1]):
                return IngestResult("rejected", checkpoint, snapshot_id=payload.snapshot_id,
                                    next_expected_chunk_index=int(upload[2]), code="chunk_conflict",
                                    detail="snapshot commit chunk_count conflicts with begin")
            if upload[9] == "committed":
                return IngestResult("duplicate", checkpoint, snapshot_id=payload.snapshot_id,
                                    next_expected_chunk_index=int(upload[2]), snapshot_committed=True)
            current_checkpoint = self._current_checkpoint(connection, key)
            if current_checkpoint != upload[10]:
                return IngestResult(
                    "rejected",
                    0 if current_checkpoint is None else current_checkpoint,
                    snapshot_id=payload.snapshot_id,
                    next_expected_chunk_index=int(upload[2]),
                    code="invariant_failed",
                    detail="stream checkpoint changed while snapshot was staged",
                )
            if (
                int(upload[1]) != int(upload[2])
                or int(upload[3]) != int(upload[6])
                or int(upload[4]) != int(upload[7])
                or int(upload[5]) != int(upload[8])
            ):
                return IngestResult("rejected", checkpoint, snapshot_id=payload.snapshot_id,
                                    next_expected_chunk_index=int(upload[2]), code="snapshot_incomplete",
                                    detail="snapshot chunks or record counts are incomplete")
            through_seq = int(upload[0])
            if through_seq < checkpoint:
                return IngestResult("rejected", checkpoint, snapshot_id=payload.snapshot_id,
                                    code="invariant_failed", detail="snapshot through_seq is behind checkpoint")
            epoch = self._ensure_stream(connection, key, now)
            changed = self._merge_staged_snapshot_entities(
                connection,
                key,
                str(payload.snapshot_id),
                epoch=epoch,
                source_seq=through_seq,
                now=now,
            )
            coverage_changed = False
            for row in connection.execute(
                """SELECT record_json FROM snapshot_coverage_records
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                     AND snapshot_id=? ORDER BY chunk_index,record_index""",
                (*key.sql(), str(payload.snapshot_id)),
            ):
                evidence = json.loads(row[0])
                coverage_changed |= self._apply_coverage(
                    connection, key.creator_account_id, evidence, now
                )
            changed |= coverage_changed
            revision = None
            if changed:
                revision = self._canonical_revision(connection, key.creator_account_id, now)
                connection.execute(
                    """INSERT INTO projection_work(
                           creator_account_id,canonical_revision,work_kind,conversation_id,created_at
                       ) VALUES (?,?, 'reseed', NULL, ?)""",
                    (key.creator_account_id, revision, now),
                )
            connection.execute(
                """INSERT INTO committed_snapshots(
                       creator_account_id,agent_installation_id,agent_stream_id,snapshot_id,
                       through_seq,chat_count,message_count,coverage_evidence_count,committed_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (*key.sql(), str(payload.snapshot_id), through_seq, int(upload[3]),
                 int(upload[4]), int(upload[5]), now),
            )
            # A snapshot replaces this source stream's provenance membership,
            # while canonical account entities remain merge-only.
            connection.execute(
                """DELETE FROM stream_message_membership
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?""",
                key.sql(),
            )
            connection.execute(
                """DELETE FROM stream_chat_membership
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?""",
                key.sql(),
            )
            connection.execute(
                """INSERT INTO stream_chat_membership(
                       creator_account_id,agent_installation_id,agent_stream_id,chat_id,
                       observed_source_seq
                   ) SELECT creator_account_id,agent_installation_id,agent_stream_id,chat_id,?
                     FROM snapshot_chat_records
                    WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                      AND snapshot_id=? AND json_extract(record_json,'$.tombstone')=0""",
                (through_seq, *key.sql(), str(payload.snapshot_id)),
            )
            connection.execute(
                """INSERT INTO stream_message_membership(
                       creator_account_id,agent_installation_id,agent_stream_id,message_id,chat_id,
                       observed_source_seq
                   ) SELECT creator_account_id,agent_installation_id,agent_stream_id,message_id,chat_id,?
                     FROM snapshot_message_records
                    WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                      AND snapshot_id=? AND json_extract(record_json,'$.tombstone')=0""",
                (through_seq, *key.sql(), str(payload.snapshot_id)),
            )
            self._advance_checkpoint(connection, key, through_seq, now)
            connection.execute(
                """UPDATE snapshot_uploads SET state='committed',committed_at=?
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                     AND snapshot_id=?""",
                (now, *key.sql(), str(payload.snapshot_id)),
            )
            connection.execute(
                """DELETE FROM snapshot_chat_records WHERE creator_account_id=?
                   AND agent_installation_id=? AND agent_stream_id=? AND snapshot_id=?""",
                (*key.sql(), str(payload.snapshot_id)),
            )
            connection.execute(
                """DELETE FROM snapshot_message_records WHERE creator_account_id=?
                   AND agent_installation_id=? AND agent_stream_id=? AND snapshot_id=?""",
                (*key.sql(), str(payload.snapshot_id)),
            )
            connection.execute(
                """DELETE FROM snapshot_coverage_records WHERE creator_account_id=?
                   AND agent_installation_id=? AND agent_stream_id=? AND snapshot_id=?""",
                (*key.sql(), str(payload.snapshot_id)),
            )
            return IngestResult("accepted", through_seq, snapshot_id=payload.snapshot_id,
                                next_expected_chunk_index=int(upload[2]), snapshot_committed=True,
                                canonical_revision=revision)

    def _apply_coverage(
        self, connection: sqlite3.Connection, account_id: str, evidence: dict[str, Any], now: str
    ) -> bool:
        kind = evidence["type"]
        generation_id = str(evidence["generation_id"])
        if kind == "generation.started":
            existing = connection.execute(
                """SELECT authorization_revision,as_of FROM coverage_generations
                   WHERE creator_account_id=? AND generation_id=?""",
                (account_id, generation_id),
            ).fetchone()
            if existing:
                if existing[0] != evidence["authorization_revision"] or existing[1] != evidence["as_of"]:
                    raise InvariantViolation("coverage generation identity was reused")
                return False
            authorization = connection.execute(
                """SELECT consent_revision,authorized_platform_creator_id,
                          desired_state,effective_state,required_config_revision,
                          effective_config_revision,settings_revision,
                          effective_settings_revision
                     FROM history_settings WHERE creator_account_id=?""",
                (account_id,),
            ).fetchone()
            if (
                authorization is None
                or authorization[0] != evidence["authorization_revision"]
                or authorization[1] is None
                or authorization[2] != "running"
                or authorization[3] != "running"
                or authorization[4] is None
                or authorization[4] != authorization[5]
                or authorization[7] is None
                or int(authorization[6]) != int(authorization[7])
            ):
                raise InvariantViolation(
                    "coverage generation is not authorized by the current applied consent/config"
                )
            connection.execute(
                """UPDATE coverage_generations SET state='superseded',reason_code='new_generation'
                   WHERE creator_account_id=? AND state IN ('discovering','backfilling')""",
                (account_id,),
            )
            connection.execute(
                """INSERT INTO coverage_generations(
                       creator_account_id,generation_id,authorization_revision,as_of,state
                   ) VALUES (?,?,?,?,'discovering')""",
                (account_id, generation_id, evidence["authorization_revision"], evidence["as_of"]),
            )
            connection.execute(
                """UPDATE account_coverage_heads SET active_generation_id=?,coverage_revision=coverage_revision+1,
                   updated_at=? WHERE creator_account_id=?""",
                (generation_id, now, account_id),
            )
            return True
        generation = connection.execute(
            """SELECT state,as_of,inventory_ended_at,closed_at FROM coverage_generations
               WHERE creator_account_id=? AND generation_id=?""",
            (account_id, generation_id),
        ).fetchone()
        if generation is None:
            raise InvariantViolation("coverage evidence references an unknown generation")
        conversation_id = evidence.get("conversation_id")
        if kind == "inventory.member":
            existing = connection.execute(
                """SELECT 1 FROM coverage_members
                   WHERE creator_account_id=? AND generation_id=? AND conversation_id=?""",
                (account_id, generation_id, conversation_id),
            ).fetchone()
            if existing is not None:
                return False
            if generation[0] not in {"discovering", "backfilling"}:
                raise InvariantViolation("closed coverage generation cannot gain inventory members")
            if generation[2] is not None:
                raise InvariantViolation("inventory membership is frozen after inventory.ended")
            chat = connection.execute(
                "SELECT 1 FROM account_chats WHERE creator_account_id=? AND chat_id=? AND is_deleted=0",
                (account_id, conversation_id),
            ).fetchone()
            if chat is None:
                raise InvariantViolation("inventory member references an unknown conversation")
            connection.execute(
                """INSERT INTO coverage_members(
                       creator_account_id,generation_id,conversation_id
                   ) VALUES (?,?,?)""", (account_id, generation_id, conversation_id)
            )
            changed = True
        elif kind == "inventory.ended":
            if generation[2] is not None:
                if generation[2] == evidence["observed_at"]:
                    return False
                raise InvariantViolation("inventory.ended was replayed with conflicting evidence")
            if generation[0] != "discovering":
                raise InvariantViolation("inventory can only end while discovery is active")
            connection.execute(
                """UPDATE coverage_generations SET inventory_ended_at=?,state='backfilling'
                   WHERE creator_account_id=? AND generation_id=?""",
                (evidence["observed_at"], account_id, generation_id),
            )
            changed = True
        elif kind in {"conversation.history_started", "conversation.head_reconciled"}:
            if generation[2] is None:
                raise InvariantViolation("conversation evidence requires frozen inventory")
            member = connection.execute(
                """SELECT history_started_at,earliest_observed_at,head_reconciled_through
                   FROM coverage_members
                   WHERE creator_account_id=? AND generation_id=? AND conversation_id=?""",
                (account_id, generation_id, conversation_id),
            ).fetchone()
            if member is None:
                raise InvariantViolation("conversation evidence requires frozen inventory membership")
            if kind == "conversation.history_started":
                if member[0] is not None:
                    if member[0] == evidence["observed_at"] and member[1] == evidence.get("earliest_observed_at"):
                        return False
                    raise InvariantViolation("conversation history boundary evidence conflicts")
                if generation[0] not in {"discovering", "backfilling"}:
                    raise InvariantViolation("closed coverage generation cannot change history evidence")
                connection.execute(
                    """UPDATE coverage_members SET history_started_at=?,earliest_observed_at=?
                       WHERE creator_account_id=? AND generation_id=? AND conversation_id=?""",
                    (evidence["observed_at"], evidence.get("earliest_observed_at"),
                     account_id, generation_id, conversation_id),
                )
            else:
                if member[2] is not None:
                    if member[2] == evidence["reconciled_through"]:
                        return False
                    raise InvariantViolation("conversation head evidence conflicts")
                if generation[0] not in {"discovering", "backfilling"}:
                    raise InvariantViolation("closed coverage generation cannot change head evidence")
                if datetime.fromisoformat(evidence["reconciled_through"]) < datetime.fromisoformat(generation[1]):
                    raise InvariantViolation("conversation head must reconcile at or beyond generation as_of")
                connection.execute(
                    """UPDATE coverage_members SET head_reconciled_through=?
                       WHERE creator_account_id=? AND generation_id=? AND conversation_id=?""",
                    (evidence["reconciled_through"], account_id, generation_id, conversation_id),
                )
            changed = True
        elif kind == "generation.closed":
            if generation[3] is not None:
                if generation[3] == evidence["closed_at"]:
                    return False
                raise InvariantViolation("generation.closed was replayed with conflicting evidence")
            if generation[0] not in {"discovering", "backfilling"}:
                raise InvariantViolation("coverage generation cannot be closed from its current state")
            if generation[2] is None:
                complete = False
                reason = "inventory_not_frozen"
            else:
                missing = int(connection.execute(
                    """SELECT COUNT(*) FROM coverage_members
                       WHERE creator_account_id=? AND generation_id=?
                         AND (history_started_at IS NULL OR head_reconciled_through IS NULL)""",
                    (account_id, generation_id),
                ).fetchone()[0])
                complete = missing == 0
                reason = None if complete else "conversation_evidence_missing"
            state = "complete" if complete else "partial"
            connection.execute(
                """UPDATE coverage_generations SET state=?,closed_at=?,reason_code=?
                   WHERE creator_account_id=? AND generation_id=?""",
                (state, evidence["closed_at"], reason, account_id, generation_id),
            )
            connection.execute(
                """UPDATE account_coverage_heads SET
                       active_generation_id=?,
                       last_complete_generation_id=CASE WHEN ? THEN ? ELSE last_complete_generation_id END,
                       coverage_revision=coverage_revision+1,updated_at=?
                   WHERE creator_account_id=?""",
                (generation_id, 1 if complete else 0, generation_id, now, account_id),
            )
            return True
        else:
            raise InvariantViolation(f"unsupported coverage evidence {kind}")
        if changed:
            connection.execute(
                """UPDATE account_coverage_heads SET coverage_revision=coverage_revision+1,updated_at=?
                   WHERE creator_account_id=?""", (now, account_id)
            )
        return changed

    def commit_delta(self, key: StreamKey, payload: Any) -> IngestResult:
        try:
            return self._commit_delta(key, payload)
        except InvariantViolation as error:
            self._record_delta_conflict(key, payload, str(error))
            raise

    def _commit_delta(self, key: StreamKey, payload: Any) -> IngestResult:
        self._require_stream_identity(key, payload)
        now = _iso(utc_now())
        document = payload.model_dump(mode="json")
        fingerprint = _hash({"source_seq": payload.source_seq, "change": document["change"]})
        with self.database.transaction() as connection:
            epoch = self._ensure_stream(connection, key, now)
            checkpoint = self._current_checkpoint(connection, key)
            if checkpoint is None:
                return IngestResult("gap", 0, code="sequence_gap", retryable=True,
                                    detail="a committed snapshot is required before deltas")
            prior = connection.execute(
                """SELECT source_seq,fingerprint FROM raw_ingest_events
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=? AND event_id=?""",
                (*key.sql(), str(payload.event_id)),
            ).fetchone()
            if prior is not None:
                if int(prior[0]) != payload.source_seq or prior[1] != fingerprint:
                    return IngestResult("rejected", checkpoint, code="invariant_failed",
                                        detail="event_id was reused with different content")
                return IngestResult("duplicate", checkpoint)
            if payload.source_seq <= checkpoint:
                sequence_owner = connection.execute(
                    """SELECT event_id FROM raw_ingest_events
                       WHERE creator_account_id=? AND agent_installation_id=?
                         AND agent_stream_id=? AND source_seq=?""",
                    (*key.sql(), payload.source_seq),
                ).fetchone()
                if sequence_owner is not None and sequence_owner[0] != str(payload.event_id):
                    return IngestResult(
                        "rejected", checkpoint, code="invariant_failed",
                        detail="source_seq was already committed for a different event_id",
                    )
                return IngestResult("duplicate", checkpoint)
            if payload.source_seq != checkpoint + 1:
                return IngestResult("gap", checkpoint, code="sequence_gap", retryable=True,
                                    detail=f"expected source sequence {checkpoint + 1}")
            connection.execute(
                """INSERT INTO raw_ingest_events(
                       creator_account_id,agent_installation_id,agent_stream_id,event_id,source_seq,
                       origin,observed_at,fingerprint,event_json,committed_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (*key.sql(), str(payload.event_id), payload.source_seq, payload.acquisition_origin,
                 now, fingerprint, _json(document), now),
            )
            change = document["change"]
            kind = change["type"]
            changed = False
            conversation_id: str | None = None
            if kind == "chat.upsert":
                changed = self._merge_chat(connection, key.creator_account_id, change["chat"], epoch,
                                           payload.source_seq, str(payload.event_id), now)
                conversation_id = change["chat"]["chat_id"]
                if changed:
                    changed |= self._invalidate_complete_coverage_for_conversation(
                        connection, key.creator_account_id, conversation_id, now
                    )
                connection.execute(
                    """INSERT INTO stream_chat_membership(
                           creator_account_id,agent_installation_id,agent_stream_id,chat_id,
                           observed_source_seq
                       ) VALUES (?,?,?,?,?)
                       ON CONFLICT(creator_account_id,agent_installation_id,agent_stream_id,chat_id)
                       DO UPDATE SET observed_source_seq=excluded.observed_source_seq""",
                    (*key.sql(), conversation_id, payload.source_seq),
                )
            elif kind == "chat.delete":
                conversation_id = change["chat_id"]
                changed = self._delete_entity(connection, key.creator_account_id, "chat",
                                              conversation_id, conversation_id, epoch, payload.source_seq,
                                              str(payload.event_id), now)
                connection.execute(
                    """DELETE FROM stream_chat_membership
                       WHERE creator_account_id=? AND agent_installation_id=?
                         AND agent_stream_id=? AND chat_id=?""",
                    (*key.sql(), conversation_id),
                )
                connection.execute(
                    """DELETE FROM stream_message_membership
                       WHERE creator_account_id=? AND agent_installation_id=?
                         AND agent_stream_id=? AND chat_id=?""",
                    (*key.sql(), conversation_id),
                )
            elif kind == "message.upsert":
                conversation_id = change["message"]["chat_id"]
                changed = self._merge_message(connection, key.creator_account_id, change["message"], epoch,
                                              payload.source_seq, str(payload.event_id), now)
                connection.execute(
                    """INSERT INTO stream_message_membership(
                           creator_account_id,agent_installation_id,agent_stream_id,message_id,chat_id,
                           observed_source_seq
                       ) VALUES (?,?,?,?,?,?)
                       ON CONFLICT(creator_account_id,agent_installation_id,agent_stream_id,message_id)
                       DO UPDATE SET chat_id=excluded.chat_id,
                                     observed_source_seq=excluded.observed_source_seq""",
                    (*key.sql(), change["message"]["message_id"], conversation_id,
                     payload.source_seq),
                )
            elif kind == "message.delete":
                conversation_id = change["chat_id"]
                changed = self._delete_entity(connection, key.creator_account_id, "message",
                                              change["message_id"], conversation_id, epoch,
                                              payload.source_seq, str(payload.event_id), now)
                connection.execute(
                    """DELETE FROM stream_message_membership
                       WHERE creator_account_id=? AND agent_installation_id=?
                         AND agent_stream_id=? AND message_id=?""",
                    (*key.sql(), change["message_id"]),
                )
            elif kind == "coverage.observed":
                conversation_id = change["evidence"].get("conversation_id")
                changed = self._apply_coverage(connection, key.creator_account_id, change["evidence"], now)
            else:
                raise InvariantViolation(f"unsupported ingest change {kind}")
            revision = None
            if changed:
                revision = self._canonical_revision(connection, key.creator_account_id, now)
                connection.execute(
                    """INSERT INTO projection_work(
                           creator_account_id,canonical_revision,work_kind,conversation_id,created_at
                       ) VALUES (?,?,?,?,?)""",
                    (key.creator_account_id, revision,
                     "coverage" if kind == "coverage.observed" else "entity", conversation_id, now),
                )
            if payload.acquisition_origin == "passive":
                expires = _iso(utc_now() + timedelta(seconds=120))
                connection.execute(
                    """INSERT INTO live_ingest_state(
                           creator_account_id,last_observed_at,last_committed_at,expires_at,pending_event_count
                       ) VALUES (?,?,?,?,0)
                       ON CONFLICT(creator_account_id) DO UPDATE SET
                           last_observed_at=excluded.last_observed_at,
                           last_committed_at=excluded.last_committed_at,
                           expires_at=excluded.expires_at,pending_event_count=0""",
                    (key.creator_account_id, now, now, expires),
                )
            self._advance_checkpoint(connection, key, payload.source_seq, now)
            return IngestResult("accepted", payload.source_seq, canonical_revision=revision)

    def account_revision(self, account_id: str) -> tuple[int, int]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT canonical_revision,view_revision FROM account_heads WHERE creator_account_id=?",
                (account_id,),
            ).fetchone()
            return (0, 0) if row is None else (int(row[0]), int(row[1]))

    def coverage(self, account_id: str) -> dict[str, Any]:
        with self.database.read() as connection:
            head = connection.execute(
                """SELECT active_generation_id,last_complete_generation_id
                   FROM account_coverage_heads WHERE creator_account_id=?""",
                (account_id,),
            ).fetchone()
            settings = connection.execute(
                """SELECT desired_state,effective_state FROM history_settings
                   WHERE creator_account_id=?""",
                (account_id,),
            ).fetchone()
            desired = None if settings is None else settings[0]
            effective = None if settings is None else settings[1]
            if head is None or head[0] is None:
                phase = "paused" if desired == "paused" else "not_started"
                reason = None
                if desired == "revoked":
                    reason = "consent_revoked"
                elif desired == "running" and effective != "running":
                    reason = "configuration_not_applied"
                return {
                    "status": "unknown", "phase": phase, "generation_id": None,
                    "as_of": None, "discovered_conversations": None,
                    "complete_conversations": 0, "complete_as_of": None,
                    "reason": reason,
                }
            generation = connection.execute(
                """SELECT generation_id,state,as_of,closed_at,reason_code FROM coverage_generations
                   WHERE creator_account_id=? AND generation_id=?""",
                (account_id, head[0]),
            ).fetchone()
            counts = connection.execute(
                """SELECT COUNT(*),SUM(CASE WHEN history_started_at IS NOT NULL
                       AND head_reconciled_through IS NOT NULL THEN 1 ELSE 0 END),
                       MIN(earliest_observed_at),MAX(head_reconciled_through)
                   FROM coverage_members WHERE creator_account_id=? AND generation_id=?""",
                (account_id, head[0]),
            ).fetchone()
            total = int(counts[0] or 0)
            complete = int(counts[1] or 0)
            state = generation[1]
            status = "complete" if state == "complete" else "partial"
            phase = {
                "discovering": "discovering", "backfilling": "backfilling",
                "complete": "complete", "partial": "blocked", "superseded": "repairing",
            }[state]
            reason = generation[4]
            if state != "complete" and desired in {"paused", "revoked"}:
                phase = "paused"
                reason = "consent_revoked" if desired == "revoked" else "history_sync_paused"
            elif state != "complete" and desired == "running" and effective != "running":
                reason = reason or "configuration_not_applied"
            complete_as_of = None
            if head[1] is not None:
                completed_generation = connection.execute(
                    """SELECT as_of FROM coverage_generations
                       WHERE creator_account_id=? AND generation_id=?""",
                    (account_id, head[1]),
                ).fetchone()
                if completed_generation is not None:
                    complete_as_of = completed_generation[0]
            return {
                "status": status, "phase": phase, "generation_id": generation[0],
                "as_of": generation[2],
                "discovered_conversations": total, "complete_conversations": complete,
                "complete_as_of": complete_as_of, "reason": reason,
            }

    def conversation_coverage(self, account_id: str, conversation_id: str) -> dict[str, Any]:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT m.history_started_at,m.earliest_observed_at,m.head_reconciled_through,g.closed_at
                   FROM account_coverage_heads h
                   JOIN coverage_generations g ON g.creator_account_id=h.creator_account_id
                                               AND g.generation_id=h.active_generation_id
                   LEFT JOIN coverage_members m ON m.creator_account_id=g.creator_account_id
                                               AND m.generation_id=g.generation_id
                                               AND m.conversation_id=?
                   WHERE h.creator_account_id=?""",
                (conversation_id, account_id),
            ).fetchone()
            if row is None or row[0] is None:
                return {"status": "unknown" if row is None else "partial", "boundary": None,
                        "earliest_available_at": None if row is None else row[1],
                        "latest_acquired_at": None if row is None else row[2],
                        "data_as_of": None if row is None else row[3],
                        "reason_code": "not_observed"}
            complete = row[2] is not None
            return {"status": "complete" if complete else "partial",
                    "boundary": "history_start", "earliest_available_at": row[1],
                    "latest_acquired_at": row[2], "data_as_of": row[3] or row[0],
                    "reason_code": None if complete else "head_not_reconciled"}

    def live_freshness(self, account_id: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT last_observed_at,last_committed_at,expires_at,pending_event_count
                   FROM live_ingest_state WHERE creator_account_id=?""", (account_id,)
            ).fetchone()
            if row is None:
                return {"status": "unknown", "last_observed_at": None, "last_committed_at": None,
                        "expires_at": None, "pending_count": None, "reason": "no_live_observation"}
            current = datetime.fromisoformat(row[2]) > now and int(row[3] or 0) == 0
            return {"status": "current" if current else "delayed", "last_observed_at": row[0],
                    "last_committed_at": row[1], "expires_at": row[2],
                    "pending_count": row[3], "reason": None if current else "live_observation_expired"}

    def history_settings(self, account_id: str) -> dict[str, Any]:
        now = _iso(utc_now())
        with self.database.transaction() as connection:
            self._ensure_account(connection, account_id, now)
            row = connection.execute("SELECT * FROM history_settings WHERE creator_account_id=?", (account_id,)).fetchone()
            return dict(row)

    def update_history_settings(self, account_id: str, *, expected_revision: int, values: dict[str, Any]) -> dict[str, Any]:
        now = _iso(utc_now())
        with self.database.transaction() as connection:
            self._ensure_account(connection, account_id, now)
            row = connection.execute(
                "SELECT settings_revision FROM history_settings WHERE creator_account_id=?",
                (account_id,),
            ).fetchone()
            if int(row[0]) != expected_revision:
                raise LookupError("settings_revision_conflict")
            connection.execute(
                """UPDATE history_settings SET
                       settings_revision=settings_revision+1,
                       consent_policy_version=?,consent_revision=?,
                       authorized_platform_creator_id=?,desired_state=?,
                       required_config_revision=NULL,
                       recent_window_days=?,page_size=?,pages_per_wake=?,
                       request_interval_ms=?,retry_limit=?,updated_at=?
                   WHERE creator_account_id=?""",
                (
                    values["consent_policy_version"], values.get("consent_revision"),
                    values.get("authorized_platform_creator_id"), values["desired_state"],
                    values["recent_window_days"], values["page_size"],
                    values["pages_per_wake"], values["request_interval_ms"],
                    values["retry_limit"], now, account_id,
                ),
            )
        return self.history_settings(account_id)

    def bind_history_config(
        self,
        account_id: str,
        *,
        settings_revision: int,
        config_revision: str,
    ) -> dict[str, Any]:
        """Bind an immutable Agent config to the still-current desired settings revision."""
        now = _iso(utc_now())
        with self.database.transaction() as connection:
            self._ensure_account(connection, account_id, now)
            cursor = connection.execute(
                """UPDATE history_settings SET required_config_revision=?,updated_at=?
                   WHERE creator_account_id=? AND settings_revision=?
                     AND required_config_revision IS NULL""",
                (config_revision, now, account_id, settings_revision),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    """SELECT settings_revision,required_config_revision FROM history_settings
                       WHERE creator_account_id=?""",
                    (account_id,),
                ).fetchone()
                if int(row[0]) != settings_revision or row[1] != config_revision:
                    raise LookupError("history_settings_changed_during_publication")
        return self.history_settings(account_id)

    def mark_history_config_applied(
        self, account_id: str, config_revision: str
    ) -> dict[str, Any]:
        """Advance desired history settings to effective only after Agent confirmation."""
        now = _iso(utc_now())
        with self.database.transaction() as connection:
            self._ensure_account(connection, account_id, now)
            row = connection.execute(
                """SELECT desired_state,settings_revision,required_config_revision
                   FROM history_settings WHERE creator_account_id=?""",
                (account_id,),
            ).fetchone()
            if row[2] == config_revision:
                desired = str(row[0])
                effective = "not_applied" if desired == "not_started" else desired
                connection.execute(
                    """UPDATE history_settings SET effective_state=?,
                           effective_config_revision=?,effective_settings_revision=?,updated_at=?
                        WHERE creator_account_id=?""",
                    (effective, config_revision, int(row[1]), now, account_id),
                )
        return self.history_settings(account_id)


class ProjectionCursorStale(ValueError):
    """Raised when a page cursor targets a projection generation that is no longer active."""


PROJECTION_BATCH_SIZE = 256
CONVERSATION_BATCH_SIZE = 64


def _quarantine_projection_files(path: Path) -> None:
    """Move a projection database and its WAL/SHM sidecars aside for rebuild.

    Requires no open connections to the path; raises otherwise.
    """
    nonce = uuid4().hex
    with LocalSQLite.exclusive_lifecycle(path) as target:
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{target}{suffix}")
            if source.exists():
                os.replace(
                    source,
                    target.with_name(f".{target.name}.{nonce}.quarantine{suffix}"),
                )


class ProjectionRepository:
    """Disposable generation-based read models built from canonical account truth."""

    def __init__(
        self,
        database: CanonicalSQLite,
        canonical: HistoryRepository,
        *,
        pipeline: ProjectionPipeline | None = None,
    ) -> None:
        self.database = database
        self.canonical = canonical
        self.pipeline = pipeline or DeterministicProjectionPipeline()

    @classmethod
    def create(
        cls,
        path: str | Path,
        canonical: HistoryRepository,
        *,
        pipeline: ProjectionPipeline | None = None,
    ) -> "ProjectionRepository":
        migrations_dir = Path(__file__).with_name("projection_sql")

        def _open() -> CanonicalSQLite:
            database = CanonicalSQLite(path)
            MigrationRunner(
                database,
                migrations_dir=migrations_dir,
                lock_path=database.path.parent / ".projection-migration.lock",
                backups_dir=database.path.parent / "projection-backups",
            ).run()
            return database

        try:
            database = _open()
        except (sqlite3.DatabaseError, MigrationChecksumError):
            # The read model is rebuildable derived state. Quarantine an
            # unreadable or schema-drifted file and recreate it empty; canonical
            # catch-up repopulates it. Canonical corruption is never recovered
            # this way.
            _quarantine_projection_files(Path(path))
            database = _open()
        return cls(database, canonical, pipeline=pipeline)

    def reset(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM projection_work_applied")
            connection.execute("DELETE FROM projection_change_log")
            connection.execute("DELETE FROM projection_analytics")
            connection.execute("DELETE FROM projection_lpg_edges")
            connection.execute("DELETE FROM projection_lpg_nodes")
            connection.execute("DELETE FROM projection_message_analysis")
            connection.execute("DELETE FROM projection_messages")
            connection.execute("DELETE FROM conversation_summaries")
            connection.execute("DELETE FROM projection_accounts")

    def _projection_authority(
        self, account_id: str
    ) -> tuple[int, dict[str, Any] | None]:
        """Read the canonical activation pointer that makes a projection generation visible."""
        with self.canonical.database.read() as connection:
            connection.execute("BEGIN")
            head = connection.execute(
                """SELECT canonical_revision,view_revision FROM account_heads
                   WHERE creator_account_id=?""",
                (account_id,),
            ).fetchone()
            if head is None:
                return 0, None
            intent = connection.execute(
                """SELECT generation_id,target_canonical_revision,
                          activated_view_revision,projection_committed_at
                     FROM projection_activation_intents
                    WHERE creator_account_id=? AND state='activated'
                      AND activated_view_revision=?
                    ORDER BY target_canonical_revision DESC LIMIT 1""",
                (account_id, int(head[1])),
            ).fetchone()
        if intent is None or intent[0] is None or intent[2] is None:
            return int(head[0]), None
        return int(head[0]), {
            "generation_id": str(intent[0]),
            "projected_revision": int(intent[1]),
            "read_revision": int(intent[2]),
            "generated_at": intent[3],
        }

    @staticmethod
    def _readable_projection_account(
        connection: sqlite3.Connection,
        account_id: str,
        authority: dict[str, Any] | None,
    ) -> sqlite3.Row | None:
        """Return only the projection generation activated by canonical SQLite."""
        if authority is None:
            return None
        row = connection.execute(
            """SELECT generation_id,projected_revision,read_revision,generated_at,status,
                      projection_slot
                 FROM projection_accounts
                WHERE creator_account_id=? AND generation_id=?""",
            (account_id, authority["generation_id"]),
        ).fetchone()
        if row is None:
            return None
        if (
            str(row[0]) != authority["generation_id"]
            or int(row[1]) != authority["projected_revision"]
            or int(row[2]) != authority["read_revision"]
        ):
            return None
        return row

    @staticmethod
    def _projection_state_document(
        canonical_revision: int,
        account: sqlite3.Row | None,
        *,
        missing_reason: str,
    ) -> dict[str, Any]:
        if account is None:
            return {
                "status": "unavailable",
                "canonical_revision": canonical_revision,
                "projected_revision": 0,
                "projected_at": None,
                "reason": missing_reason,
            }
        projected = int(account[1])
        stored_status = str(account[4])
        if stored_status == "degraded":
            status = "degraded"
            reason = "projection_degraded"
        elif projected == canonical_revision:
            status = "current"
            reason = None
        else:
            status = "pending"
            reason = "projection_lag"
        return {
            "status": status,
            "canonical_revision": canonical_revision,
            "projected_revision": projected,
            "projected_at": account[3],
            "reason": reason,
        }

    def state(self, account_id: str) -> dict[str, Any]:
        canonical_revision, authority = self._projection_authority(account_id)
        with self.database.read() as connection:
            connection.execute("BEGIN")
            raw_account = connection.execute(
                "SELECT 1 FROM projection_accounts WHERE creator_account_id=?",
                (account_id,),
            ).fetchone()
            account = self._readable_projection_account(connection, account_id, authority)
        return self._projection_state_document(
            canonical_revision,
            account,
            missing_reason=(
                "projection_missing" if raw_account is None else "projection_activation_pending"
            ),
        )

    def pending_accounts(self) -> list[str]:
        """Return accounts whose durable projection/activation work needs recovery."""
        with self.canonical.database.read() as connection:
            rows = connection.execute(
                """SELECT creator_account_id FROM projection_work
                     WHERE completed_at IS NULL
                   UNION
                   SELECT creator_account_id FROM projection_activation_intents
                     WHERE state='pending'
                   ORDER BY creator_account_id"""
            ).fetchall()
        return [str(row[0]) for row in rows]

    def active_generation(self, account_id: str) -> dict[str, Any] | None:
        _, authority = self._projection_authority(account_id)
        with self.database.read() as connection:
            connection.execute("BEGIN")
            row = self._readable_projection_account(connection, account_id, authority)
        if row is None:
            return None
        return {
            "generation_id": row[0],
            "projected_revision": int(row[1]),
            "read_revision": int(row[2]),
            "generated_at": row[3],
        }

    @staticmethod
    def _conversation_coverage(
        member: sqlite3.Row | tuple[Any, ...] | None,
        *,
        generation_exists: bool,
        generation_closed_at: str | None,
    ) -> dict[str, Any]:
        if member is None or member[0] is None:
            return {
                "status": "unknown" if not generation_exists else "partial",
                "boundary": None,
                "earliest_available_at": None if member is None else member[1],
                "latest_acquired_at": None if member is None else member[2],
                "data_as_of": generation_closed_at,
                "reason_code": "not_observed",
            }
        complete = member[2] is not None
        return {
            "status": "complete" if complete else "partial",
            "boundary": "history_start",
            "earliest_available_at": member[1],
            "latest_acquired_at": member[2],
            "data_as_of": generation_closed_at or member[0],
            "reason_code": None if complete else "head_not_reconciled",
        }

    @staticmethod
    def _metric(
        value: int | None,
        *,
        basis: str,
        observed_range: dict[str, str | None],
        complete_range: dict[str, str | None] | None,
        sample_size: int,
        as_of: str,
        projection_revision: int,
    ) -> dict[str, Any]:
        return {
            "value": value,
            "basis": basis,
            "observed_range": observed_range,
            "complete_range": complete_range,
            "sample_size": sample_size,
            "as_of": as_of,
            "projection_revision": projection_revision,
        }

    def _ensure_activation_intent(
        self, account_id: str, target_revision: int, now: str
    ) -> None:
        with self.canonical.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO projection_activation_intents(
                       creator_account_id,target_canonical_revision,state,requested_at
                   ) VALUES (?,?,'pending',?)""",
                (account_id, target_revision, now),
            )

    def _activate_committed_projection(
        self,
        account_id: str,
        *,
        target_revision: int,
        generation_id: str,
        read_revision: int,
        committed_at: str,
    ) -> None:
        """Converge the canonical activation intent after projection commit/restart."""
        with self.canonical.database.transaction() as connection:
            head = connection.execute(
                "SELECT view_revision FROM account_heads WHERE creator_account_id=?",
                (account_id,),
            ).fetchone()
            if head is None:
                raise RuntimeError("projection activation account is missing")
            current_view = int(head[0])
            if current_view > read_revision:
                raise RuntimeError("projection activation would regress the Bridge revision")
            connection.execute(
                """UPDATE account_heads SET view_revision=MAX(view_revision,?),updated_at=?
                   WHERE creator_account_id=?""",
                (read_revision, committed_at, account_id),
            )
            connection.execute(
                """UPDATE projection_work SET completed_at=?
                   WHERE creator_account_id=? AND completed_at IS NULL
                     AND canonical_revision<=?""",
                (committed_at, account_id, target_revision),
            )
            connection.execute(
                """UPDATE projection_activation_intents
                      SET state='superseded'
                    WHERE creator_account_id=? AND state='pending'
                      AND target_canonical_revision<?""",
                (account_id, target_revision),
            )
            connection.execute(
                """INSERT INTO projection_activation_intents(
                       creator_account_id,target_canonical_revision,state,requested_at,
                       generation_id,activated_view_revision,projection_committed_at,activated_at
                   ) VALUES (?,?,'activated',?,?,?,?,?)
                   ON CONFLICT(creator_account_id,target_canonical_revision) DO UPDATE SET
                       state='activated',generation_id=excluded.generation_id,
                       activated_view_revision=excluded.activated_view_revision,
                       projection_committed_at=excluded.projection_committed_at,
                       activated_at=excluded.activated_at""",
                (
                    account_id,
                    target_revision,
                    committed_at,
                    generation_id,
                    read_revision,
                    committed_at,
                    committed_at,
                ),
            )

    @staticmethod
    def _delete_projection_conversations(
        connection: sqlite3.Connection,
        account_id: str,
        projection_slot: int,
        conversation_ids: list[str],
    ) -> None:
        if not conversation_ids:
            return
        placeholders = ",".join("?" for _ in conversation_ids)
        parameters: list[Any] = [account_id, projection_slot, *conversation_ids]
        for table in (
            "projection_lpg_edges",
            "projection_lpg_nodes",
            "projection_message_analysis",
            "projection_messages",
            "conversation_summaries",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE creator_account_id=? AND projection_slot=? "
                f"AND conversation_id IN ({placeholders})",
                parameters,
            )

    @staticmethod
    def _insert_pipeline_material(
        connection: sqlite3.Connection,
        account_id: str,
        projection_slot: int,
        batch: Any,
    ) -> None:
        connection.executemany(
            """INSERT INTO projection_message_analysis(
                   creator_account_id,projection_slot,conversation_id,message_id,source_hash,
                   analysis_status,sentiment,analyzer_id,document_json
               ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                (
                    account_id,
                    projection_slot,
                    item.conversation_id,
                    item.message_id,
                    item.source_hash,
                    item.status,
                    item.sentiment,
                    item.analyzer_id,
                    item.document_json,
                )
                for item in batch.analyses
            ),
        )
        connection.executemany(
            """INSERT INTO projection_lpg_nodes(
                   creator_account_id,projection_slot,conversation_id,node_id,node_kind,entity_id,
                   document_json
               ) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(creator_account_id,projection_slot,node_id) DO UPDATE SET
                   conversation_id=excluded.conversation_id,
                   node_kind=excluded.node_kind,entity_id=excluded.entity_id,
                   document_json=excluded.document_json""",
            (
                (
                    account_id,
                    projection_slot,
                    item.conversation_id,
                    item.node_id,
                    item.node_kind,
                    item.entity_id,
                    item.document_json,
                )
                for item in batch.nodes
            ),
        )
        connection.executemany(
            """INSERT INTO projection_lpg_edges(
                   creator_account_id,projection_slot,conversation_id,edge_id,source_node_id,
                   target_node_id,relationship,document_json
               ) VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(creator_account_id,projection_slot,edge_id) DO UPDATE SET
                   conversation_id=excluded.conversation_id,
                   source_node_id=excluded.source_node_id,
                   target_node_id=excluded.target_node_id,
                   relationship=excluded.relationship,document_json=excluded.document_json""",
            (
                (
                    account_id,
                    projection_slot,
                    item.conversation_id,
                    item.edge_id,
                    item.source_node_id,
                    item.target_node_id,
                    item.relationship,
                    item.document_json,
                )
                for item in batch.edges
            ),
        )

    def _project_conversation_entities(
        self,
        canonical_connection: sqlite3.Connection,
        projection_connection: sqlite3.Connection,
        account_id: str,
        projection_slot: int,
        conversations: list[CanonicalProjectionConversation],
    ) -> None:
        if not conversations:
            return
        self._insert_pipeline_material(
            projection_connection,
            account_id,
            projection_slot,
            self.pipeline.project(conversations, []),
        )
        conversation_ids = [item.conversation_id for item in conversations]
        placeholders = ",".join("?" for _ in conversation_ids)
        cursor = canonical_connection.execute(
            f"""SELECT chat_id,message_id,sender_platform_user_id,text,sent_at,direction
                  FROM account_messages
                 WHERE creator_account_id=? AND is_deleted=0
                   AND chat_id IN ({placeholders})
                 ORDER BY chat_id,sent_at,message_id""",
            (account_id, *conversation_ids),
        )
        while rows := cursor.fetchmany(PROJECTION_BATCH_SIZE):
            messages = [
                CanonicalProjectionMessage(
                    conversation_id=str(row[0]),
                    message_id=str(row[1]),
                    sender_platform_user_id=str(row[2]),
                    text=str(row[3]),
                    sent_at=str(row[4]),
                    direction=row[5],
                )
                for row in rows
            ]
            batch = self.pipeline.project(conversations, messages)
            analysis_by_message = {
                item.message_id: item for item in batch.analyses
            }
            if (
                len(analysis_by_message) != len(batch.analyses)
                or set(analysis_by_message) != {item.message_id for item in messages}
            ):
                raise RuntimeError(
                    "projection pipeline did not analyze each canonical message exactly once"
                )
            projection_connection.executemany(
                """INSERT INTO projection_messages(
                       creator_account_id,projection_slot,conversation_id,message_id,text,sent_at,
                       direction,sentiment
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    (
                        account_id,
                        projection_slot,
                        item.conversation_id,
                        item.message_id,
                        item.text,
                        item.sent_at,
                        item.direction,
                        analysis_by_message[item.message_id].sentiment,
                    )
                    for item in messages
                ),
            )
            self._insert_pipeline_material(
                projection_connection, account_id, projection_slot, batch
            )

    def _write_conversation_summaries(
        self,
        canonical_connection: sqlite3.Connection,
        projection_connection: sqlite3.Connection,
        account_id: str,
        projection_slot: int,
        chat_rows: list[sqlite3.Row],
        *,
        active_generation: str | None,
        generation_closed_at: str | None,
    ) -> None:
        if not chat_rows:
            return
        ids = [str(row[0]) for row in chat_rows]
        placeholders = ",".join("?" for _ in ids)
        latest_by_chat = {
            str(row[0]): row[1:]
            for row in projection_connection.execute(
                f"""WITH ranked AS (
                        SELECT conversation_id,message_id,text,sent_at,direction,sentiment,
                               ROW_NUMBER() OVER (
                                   PARTITION BY conversation_id
                                   ORDER BY sent_at DESC,message_id DESC
                               ) AS position
                           FROM projection_messages
                          WHERE creator_account_id=? AND projection_slot=?
                            AND conversation_id IN ({placeholders})
                    )
                    SELECT conversation_id,message_id,text,sent_at,direction,sentiment
                      FROM ranked WHERE position=1""",
                (account_id, projection_slot, *ids),
            )
        }
        members: dict[str, sqlite3.Row] = {}
        if active_generation is not None:
            members = {
                str(row[0]): row[1:]
                for row in canonical_connection.execute(
                    f"""SELECT conversation_id,history_started_at,earliest_observed_at,
                                head_reconciled_through
                           FROM coverage_members
                          WHERE creator_account_id=? AND generation_id=?
                            AND conversation_id IN ({placeholders})""",
                    (account_id, active_generation, *ids),
                )
            }
        documents: list[tuple[str, int, str, str]] = []
        for chat in chat_rows:
            conversation_id = str(chat[0])
            latest = latest_by_chat.get(conversation_id)
            document = {
                "conversation_id": conversation_id,
                "platform_user_id": chat[1],
                "display_name": chat[2],
                "unread_count": 0,
                "last_message_at": None if latest is None else latest[2],
                "latest_message": None
                if latest is None
                else {
                    "message_id": latest[0],
                    "text": latest[1],
                    "sent_at": latest[2],
                    "direction": latest[3],
                    "sentiment": latest[4],
                },
                "coverage": self._conversation_coverage(
                    members.get(conversation_id),
                    generation_exists=active_generation is not None,
                    generation_closed_at=generation_closed_at,
                ),
            }
            documents.append((account_id, projection_slot, conversation_id, _json(document)))
        projection_connection.executemany(
            """INSERT INTO conversation_summaries(
                   creator_account_id,projection_slot,conversation_id,document_json
               ) VALUES (?,?,?,?)
               ON CONFLICT(creator_account_id,projection_slot,conversation_id) DO UPDATE SET
                   document_json=excluded.document_json""",
            documents,
        )

    def _process_conversation_ids(
        self,
        canonical_connection: sqlite3.Connection,
        projection_connection: sqlite3.Connection,
        account_id: str,
        projection_slot: int,
        conversation_ids: list[str],
        rebuild_ids: list[str],
        *,
        active_generation: str | None,
        generation_closed_at: str | None,
    ) -> None:
        self._delete_projection_conversations(
            projection_connection, account_id, projection_slot, rebuild_ids
        )
        if not conversation_ids:
            return
        placeholders = ",".join("?" for _ in conversation_ids)
        chat_rows = canonical_connection.execute(
            f"""SELECT chat_id,platform_user_id,display_name
                  FROM account_chats
                 WHERE creator_account_id=? AND is_deleted=0
                   AND chat_id IN ({placeholders}) ORDER BY chat_id""",
            (account_id, *conversation_ids),
        ).fetchall()
        rebuild = set(rebuild_ids)
        conversations = [
            CanonicalProjectionConversation(str(row[0]), row[1], row[2])
            for row in chat_rows
            if str(row[0]) in rebuild
        ]
        if conversations:
            # projection_messages has an FK to the bounded conversation read
            # model; publish provisional parents inside this still-invisible
            # generation transaction, then replace their latest-message tail.
            self._write_conversation_summaries(
                canonical_connection,
                projection_connection,
                account_id,
                projection_slot,
                [row for row in chat_rows if str(row[0]) in rebuild],
                active_generation=active_generation,
                generation_closed_at=generation_closed_at,
            )
        self._project_conversation_entities(
            canonical_connection,
            projection_connection,
            account_id,
            projection_slot,
            conversations,
        )
        self._write_conversation_summaries(
            canonical_connection,
            projection_connection,
            account_id,
            projection_slot,
            chat_rows,
            active_generation=active_generation,
            generation_closed_at=generation_closed_at,
        )

    @staticmethod
    def _clear_projection_slot(
        connection: sqlite3.Connection, account_id: str, projection_slot: int
    ) -> None:
        for table in (
            "projection_lpg_edges",
            "projection_lpg_nodes",
            "projection_message_analysis",
            "projection_messages",
            "conversation_summaries",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE creator_account_id=? AND projection_slot=?",
                (account_id, projection_slot),
            )
        connection.execute(
            """DELETE FROM projection_analytics
                WHERE creator_account_id=? AND projection_slot=?""",
            (account_id, projection_slot),
        )

    @classmethod
    def _clone_projection_slot(
        cls,
        connection: sqlite3.Connection,
        account_id: str,
        source_slot: int,
        target_slot: int,
    ) -> None:
        """Seed the second slot with one bounded-memory, set-based SQLite copy."""
        cls._clear_projection_slot(connection, account_id, target_slot)
        connection.execute(
            """INSERT INTO conversation_summaries(
                   creator_account_id,projection_slot,conversation_id,document_json
               ) SELECT creator_account_id,?,conversation_id,document_json
                   FROM conversation_summaries
                  WHERE creator_account_id=? AND projection_slot=?""",
            (target_slot, account_id, source_slot),
        )
        connection.execute(
            """INSERT INTO projection_messages(
                   creator_account_id,projection_slot,conversation_id,message_id,text,sent_at,
                   direction,sentiment
               ) SELECT creator_account_id,?,conversation_id,message_id,text,sent_at,
                        direction,sentiment
                   FROM projection_messages
                  WHERE creator_account_id=? AND projection_slot=?""",
            (target_slot, account_id, source_slot),
        )
        connection.execute(
            """INSERT INTO projection_message_analysis(
                   creator_account_id,projection_slot,conversation_id,message_id,source_hash,
                   analysis_status,sentiment,analyzer_id,document_json
               ) SELECT creator_account_id,?,conversation_id,message_id,source_hash,
                        analysis_status,sentiment,analyzer_id,document_json
                   FROM projection_message_analysis
                  WHERE creator_account_id=? AND projection_slot=?""",
            (target_slot, account_id, source_slot),
        )
        connection.execute(
            """INSERT INTO projection_lpg_nodes(
                   creator_account_id,projection_slot,conversation_id,node_id,node_kind,entity_id,
                   document_json
               ) SELECT creator_account_id,?,conversation_id,node_id,node_kind,entity_id,
                        document_json
                   FROM projection_lpg_nodes
                  WHERE creator_account_id=? AND projection_slot=?""",
            (target_slot, account_id, source_slot),
        )
        connection.execute(
            """INSERT INTO projection_lpg_edges(
                   creator_account_id,projection_slot,conversation_id,edge_id,source_node_id,
                   target_node_id,relationship,document_json
               ) SELECT creator_account_id,?,conversation_id,edge_id,source_node_id,
                        target_node_id,relationship,document_json
                   FROM projection_lpg_edges
                  WHERE creator_account_id=? AND projection_slot=?""",
            (target_slot, account_id, source_slot),
        )
        connection.execute(
            """INSERT INTO projection_analytics(
                   creator_account_id,projection_slot,document_json
               ) SELECT creator_account_id,?,document_json
                   FROM projection_analytics
                  WHERE creator_account_id=? AND projection_slot=?""",
            (target_slot, account_id, source_slot),
        )
        connection.execute(
            """INSERT OR IGNORE INTO projection_work_applied(
                   creator_account_id,projection_slot,work_id,applied_at
               ) SELECT creator_account_id,?,work_id,applied_at
                   FROM projection_work_applied
                  WHERE creator_account_id=? AND projection_slot=?""",
            (target_slot, account_id, source_slot),
        )

    def catch_up(self, account_id: str) -> dict[str, Any] | None:
        """Replay durable work into the inactive slot, then canonically activate it."""
        canonical_connection = self.canonical.database.connect()
        try:
            canonical_connection.execute("BEGIN")
            work = canonical_connection.execute(
                """SELECT MAX(canonical_revision) FROM projection_work
                    WHERE creator_account_id=? AND completed_at IS NULL""",
                (account_id,),
            ).fetchone()
            target_revision = None if work is None or work[0] is None else int(work[0])
            pending_intent = canonical_connection.execute(
                """SELECT MAX(target_canonical_revision)
                     FROM projection_activation_intents
                    WHERE creator_account_id=? AND state='pending'""",
                (account_id,),
            ).fetchone()
            pending_target = None if pending_intent is None else pending_intent[0]
            head = canonical_connection.execute(
                """SELECT canonical_revision,view_revision FROM account_heads
                    WHERE creator_account_id=?""",
                (account_id,),
            ).fetchone()
            current_view = 0 if head is None else int(head[1])
            activated = canonical_connection.execute(
                """SELECT generation_id FROM projection_activation_intents
                    WHERE creator_account_id=? AND state='activated'
                      AND activated_view_revision=?
                    ORDER BY target_canonical_revision DESC LIMIT 1""",
                (account_id, current_view),
            ).fetchone()
            active_generation_id = None if activated is None else activated[0]

            with self.database.read() as connection:
                slots = connection.execute(
                    """SELECT generation_id,projected_revision,read_revision,generated_at,
                              status,projection_slot
                         FROM projection_accounts WHERE creator_account_id=?
                        ORDER BY projection_slot""",
                    (account_id,),
                ).fetchall()
            active_projection = next(
                (
                    row
                    for row in slots
                    if active_generation_id is not None
                    and str(row[0]) == str(active_generation_id)
                    and int(row[2]) == current_view
                ),
                None,
            )

            recovery_target = target_revision if target_revision is not None else pending_target
            if recovery_target is not None:
                committed = max(
                    (
                        row
                        for row in slots
                        if int(row[1]) >= int(recovery_target)
                        and int(row[2]) >= current_view
                    ),
                    key=lambda row: (int(row[1]), int(row[2])),
                    default=None,
                )
                if committed is not None and (
                    int(committed[2]) > current_view
                    or (
                        active_projection is not None
                        and int(active_projection[1]) >= int(recovery_target)
                    )
                ):
                    canonical_connection.rollback()
                    self._activate_committed_projection(
                        account_id,
                        target_revision=int(committed[1]),
                        generation_id=str(committed[0]),
                        read_revision=int(committed[2]),
                        committed_at=str(committed[3]),
                    )
                    return self.snapshot(account_id)

            if target_revision is None:
                canonical_connection.rollback()
                return None

            active_slot = None if active_projection is None else int(active_projection[5])
            if active_slot is not None:
                build_slot = 1 - active_slot
            elif slots:
                build_slot = int(max(slots, key=lambda row: int(row[1]))[5])
            else:
                build_slot = 0
            base_projection = next(
                (row for row in slots if int(row[5]) == build_slot), None
            )
            seed_from_active = base_projection is None and active_projection is not None
            if seed_from_active:
                base_revision = int(active_projection[1])
            else:
                base_revision = 0 if base_projection is None else int(base_projection[1])
            replay = canonical_connection.execute(
                """SELECT MAX(CASE WHEN work_kind='reseed' THEN 1 ELSE 0 END),
                          MAX(CASE WHEN work_kind='coverage' AND conversation_id IS NULL
                                   THEN 1 ELSE 0 END)
                     FROM projection_work
                    WHERE creator_account_id=? AND canonical_revision>?
                      AND canonical_revision<=?""",
                (account_id, base_revision, target_revision),
            ).fetchone()
            full_reseed = (base_projection is None and not seed_from_active) or bool(replay[0])
            has_global_coverage = bool(replay[1])

            now = _iso(utc_now())
            self._ensure_activation_intent(account_id, target_revision, now)
            generation_id = str(uuid4())
            next_view = current_view + 1
            coverage_head = canonical_connection.execute(
                """SELECT active_generation_id FROM account_coverage_heads
                   WHERE creator_account_id=?""",
                (account_id,),
            ).fetchone()
            active_generation = None if coverage_head is None else coverage_head[0]
            generation_closed_at = None
            coverage_state = None
            if active_generation is not None:
                generation = canonical_connection.execute(
                    """SELECT state,closed_at FROM coverage_generations
                       WHERE creator_account_id=? AND generation_id=?""",
                    (account_id, active_generation),
                ).fetchone()
                if generation is not None:
                    coverage_state = str(generation[0])
                    generation_closed_at = generation[1]

            with self.database.transaction() as projection_connection:
                projection_connection.execute(
                    """DELETE FROM projection_change_log
                        WHERE creator_account_id=? AND projection_slot=?
                          AND read_revision>=?""",
                    (account_id, build_slot, next_view),
                )
                projection_connection.execute(
                    """INSERT INTO projection_accounts(
                           creator_account_id,projection_slot,generation_id,projected_revision,
                           read_revision,generated_at,status
                       ) VALUES (?,?,?,?,?,?,'current')
                       ON CONFLICT(creator_account_id,projection_slot) DO UPDATE SET
                           generation_id=excluded.generation_id,
                           projected_revision=excluded.projected_revision,
                           read_revision=excluded.read_revision,
                           generated_at=excluded.generated_at,status='current'""",
                    (
                        account_id,
                        build_slot,
                        generation_id,
                        target_revision,
                        next_view,
                        now,
                    ),
                )
                projection_connection.execute(
                    "DROP TABLE IF EXISTS temp.projection_touched_conversations"
                )
                if seed_from_active and not full_reseed:
                    self._clone_projection_slot(
                        projection_connection,
                        account_id,
                        int(active_projection[5]),
                        build_slot,
                    )
                projection_connection.execute(
                    """CREATE TEMP TABLE projection_touched_conversations(
                           conversation_id TEXT PRIMARY KEY,
                           rebuild_entities INTEGER NOT NULL CHECK (rebuild_entities IN (0,1))
                       ) WITHOUT ROWID"""
                )
                cursor = canonical_connection.execute(
                    """SELECT work_id,work_kind,conversation_id
                       FROM projection_work
                      WHERE creator_account_id=? AND canonical_revision>?
                        AND canonical_revision<=? ORDER BY work_id""",
                    (account_id, base_revision, target_revision),
                )
                while rows := cursor.fetchmany(PROJECTION_BATCH_SIZE):
                    projection_connection.executemany(
                        """INSERT OR IGNORE INTO projection_work_applied(
                               creator_account_id,projection_slot,work_id,applied_at
                           ) VALUES (?,?,?,?)""",
                        [
                            (account_id, build_slot, int(row[0]), now)
                            for row in rows
                        ],
                    )
                    projection_connection.executemany(
                        """INSERT INTO projection_touched_conversations(
                               conversation_id,rebuild_entities
                           ) VALUES (?,?)
                           ON CONFLICT(conversation_id) DO UPDATE SET
                               rebuild_entities=MAX(
                                   rebuild_entities,excluded.rebuild_entities
                               )""",
                        [
                            (str(row[2]), 1 if row[1] in {"entity", "reseed"} else 0)
                            for row in rows
                            if row[2] is not None
                        ],
                    )

                if full_reseed:
                    self._clear_projection_slot(
                        projection_connection, account_id, build_slot
                    )
                    chat_cursor = canonical_connection.execute(
                        """SELECT chat_id FROM account_chats
                           WHERE creator_account_id=? AND is_deleted=0 ORDER BY chat_id""",
                        (account_id,),
                    )
                    while chat_rows := chat_cursor.fetchmany(CONVERSATION_BATCH_SIZE):
                        ids = [str(row[0]) for row in chat_rows]
                        self._process_conversation_ids(
                            canonical_connection,
                            projection_connection,
                            account_id,
                            build_slot,
                            ids,
                            ids,
                            active_generation=active_generation,
                            generation_closed_at=generation_closed_at,
                        )
                    change_kind = "reseed"
                else:
                    touched_cursor = projection_connection.execute(
                        """SELECT conversation_id,rebuild_entities
                           FROM projection_touched_conversations ORDER BY conversation_id"""
                    )
                    while touched := touched_cursor.fetchmany(CONVERSATION_BATCH_SIZE):
                        ids = [str(row[0]) for row in touched]
                        rebuild_ids = [str(row[0]) for row in touched if int(row[1])]
                        self._process_conversation_ids(
                            canonical_connection,
                            projection_connection,
                            account_id,
                            build_slot,
                            ids,
                            rebuild_ids,
                            active_generation=active_generation,
                            generation_closed_at=generation_closed_at,
                        )
                    change_kind = "incremental"
                    if has_global_coverage:
                        chat_cursor = canonical_connection.execute(
                            """SELECT chat_id FROM account_chats
                               WHERE creator_account_id=? AND is_deleted=0 ORDER BY chat_id""",
                            (account_id,),
                        )
                        while chat_rows := chat_cursor.fetchmany(CONVERSATION_BATCH_SIZE):
                            ids = [str(row[0]) for row in chat_rows]
                            self._process_conversation_ids(
                                canonical_connection,
                                projection_connection,
                                account_id,
                                build_slot,
                                ids,
                                [],
                                active_generation=active_generation,
                                generation_closed_at=generation_closed_at,
                            )
                        change_kind = "coverage_refresh"

                counts = canonical_connection.execute(
                    """SELECT COUNT(*),SUM(direction='inbound'),SUM(direction='outbound'),
                              MIN(sent_at),MAX(sent_at)
                       FROM account_messages
                      WHERE creator_account_id=? AND is_deleted=0""",
                    (account_id,),
                ).fetchone()
                chat_count = int(
                    canonical_connection.execute(
                        """SELECT COUNT(*) FROM account_chats
                           WHERE creator_account_id=? AND is_deleted=0""",
                        (account_id,),
                    ).fetchone()[0]
                )
                observed_range = {"start": counts[3], "end": counts[4]}
                basis = "complete" if coverage_state == "complete" else "synced_subset"
                complete_range = dict(observed_range) if basis == "complete" else None
                total_messages = int(counts[0] or 0)
                analytics = {
                    "total_conversations": self._metric(
                        chat_count, basis=basis, observed_range=observed_range,
                        complete_range=complete_range, sample_size=chat_count, as_of=now,
                        projection_revision=target_revision,
                    ),
                    "total_messages": self._metric(
                        total_messages, basis=basis, observed_range=observed_range,
                        complete_range=complete_range, sample_size=total_messages, as_of=now,
                        projection_revision=target_revision,
                    ),
                    "inbound_messages": self._metric(
                        int(counts[1] or 0), basis=basis, observed_range=observed_range,
                        complete_range=complete_range, sample_size=total_messages, as_of=now,
                        projection_revision=target_revision,
                    ),
                    "outbound_messages": self._metric(
                        int(counts[2] or 0), basis=basis, observed_range=observed_range,
                        complete_range=complete_range, sample_size=total_messages, as_of=now,
                        projection_revision=target_revision,
                    ),
                }
                projection_connection.execute(
                    """INSERT INTO projection_analytics(
                           creator_account_id,projection_slot,document_json
                       ) VALUES (?,?,?)
                       ON CONFLICT(creator_account_id,projection_slot) DO UPDATE SET
                           document_json=excluded.document_json""",
                    (account_id, build_slot, _json(analytics)),
                )
                touched_count = int(
                    projection_connection.execute(
                        "SELECT COUNT(*) FROM projection_touched_conversations"
                    ).fetchone()[0]
                )
                touched_ids = None
                if touched_count <= 100:
                    touched_ids = [
                        str(row[0])
                        for row in projection_connection.execute(
                            """SELECT conversation_id
                               FROM projection_touched_conversations ORDER BY conversation_id"""
                        )
                    ]
                projection_connection.execute(
                    """INSERT INTO projection_change_log(
                           creator_account_id,projection_slot,read_revision,generation_id,
                           projected_revision,change_kind,touched_conversations_json,committed_at
                       ) VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        account_id,
                        build_slot,
                        next_view,
                        generation_id,
                        target_revision,
                        change_kind,
                        _json({"count": touched_count, "conversation_ids": touched_ids}),
                        now,
                    ),
                )
            canonical_connection.rollback()
        finally:
            canonical_connection.close()

        self._activate_committed_projection(
            account_id,
            target_revision=target_revision,
            generation_id=generation_id,
            read_revision=next_view,
            committed_at=now,
        )
        return self.snapshot(account_id)

    def conversation_exists(self, account_id: str, conversation_id: str) -> bool:
        _, authority = self._projection_authority(account_id)
        with self.database.read() as connection:
            connection.execute("BEGIN")
            account = self._readable_projection_account(connection, account_id, authority)
            if account is None:
                return False
            return connection.execute(
                """SELECT 1 FROM conversation_summaries
                   WHERE creator_account_id=? AND projection_slot=?
                     AND conversation_id=?""",
                (account_id, int(account[5]), conversation_id),
            ).fetchone() is not None

    def message_rows(
        self,
        account_id: str,
        conversation_id: str,
        *,
        before: tuple[str, str] | None,
        limit: int,
        expected_generation: str | None = None,
        expected_revision: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
        _, authority = self._projection_authority(account_id)
        with self.database.read() as connection:
            connection.execute("BEGIN")
            account = self._readable_projection_account(connection, account_id, authority)
            if account is None:
                raise LookupError("projection_unavailable")
            if (
                (expected_generation is not None and account[0] != expected_generation)
                or (expected_revision is not None and int(account[1]) != expected_revision)
            ):
                raise ProjectionCursorStale("cursor_stale")
            parameters: list[Any] = [account_id, int(account[5]), conversation_id]
            predicate = ""
            if before is not None:
                predicate = " AND (sent_at < ? OR (sent_at = ? AND message_id < ?))"
                parameters.extend([before[0], before[0], before[1]])
            parameters.append(limit + 1)
            rows = connection.execute(
                f"""SELECT message_id,text,sent_at,direction,sentiment
                    FROM projection_messages
                    WHERE creator_account_id=? AND projection_slot=?
                      AND conversation_id=? {predicate}
                    ORDER BY sent_at DESC,message_id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = [
            {
                "message_id": row[0],
                "text": row[1],
                "sent_at": row[2],
                "direction": row[3],
                "sentiment": row[4],
            }
            for row in reversed(selected)
        ]
        return items, has_more, {
            "generation_id": account[0],
            "projected_revision": int(account[1]),
            "read_revision": int(account[2]),
            "generated_at": account[3],
        }

    def snapshot(self, account_id: str) -> dict[str, Any]:
        coverage = self.canonical.coverage(account_id)
        live = self.canonical.live_freshness(account_id)
        canonical_revision, authority = self._projection_authority(account_id)
        with self.database.read() as connection:
            connection.execute("BEGIN")
            raw_account = connection.execute(
                "SELECT 1 FROM projection_accounts WHERE creator_account_id=?",
                (account_id,),
            ).fetchone()
            account = self._readable_projection_account(connection, account_id, authority)
            if account is None:
                conversations = []
                analytics_row = None
            else:
                conversations = [
                    json.loads(row[0])
                    for row in connection.execute(
                        """SELECT document_json FROM conversation_summaries
                           WHERE creator_account_id=? AND projection_slot=?
                           ORDER BY conversation_id""",
                        (account_id, int(account[5])),
                    )
                ]
                analytics_row = connection.execute(
                    """SELECT document_json FROM projection_analytics
                        WHERE creator_account_id=? AND projection_slot=?""",
                    (account_id, int(account[5])),
                ).fetchone()
        projection = self._projection_state_document(
            canonical_revision,
            account,
            missing_reason=(
                "projection_missing" if raw_account is None else "projection_activation_pending"
            ),
        )
        now = _iso(utc_now())
        if analytics_row is None:
            empty_range = {"start": None, "end": None}
            analytics = {
                name: self._metric(
                    None,
                    basis="synced_subset",
                    observed_range=empty_range,
                    complete_range=None,
                    sample_size=0,
                    as_of=now,
                    projection_revision=0,
                )
                for name in (
                    "total_conversations",
                    "total_messages",
                    "inbound_messages",
                    "outbound_messages",
                )
            }
        else:
            analytics = json.loads(analytics_row[0])
        return {
            "creator_account_id": account_id,
            "view_revision": 0 if account is None else int(account[2]),
            "generated_at": now if account is None else account[3],
            "conversations": conversations,
            "analytics": analytics,
            "coverage": coverage,
            "projection": projection,
            "live_freshness": live,
        }
