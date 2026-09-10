"""Thin test adapter translating independent model commands to production calls.

Drives HistoryRepository.begin_snapshot, add_snapshot_chunk, commit_snapshot,
and commit_delta, the exact canonical authority invoked by transport manager.
Normalizes results and InvariantViolation exceptions into a standard outcome.
Provides an observation seam over canonical SQLite for invariant verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from app.persistence.database import CanonicalSQLite
from app.persistence.history import (
    HistoryRepository,
    IngestResult,
    InvariantViolation,
    StreamKey,
)
from app.protocol.common import (
    ChatDeleteChange,
    ChatUpsertChange,
    CoverageConversationHeadReconciled,
    CoverageConversationHistoryStarted,
    CoverageGenerationClosed,
    CoverageGenerationStarted,
    CoverageInventoryEnded,
    CoverageInventoryMember,
    CoverageObservedChange,
    MessageDeleteChange,
    MessageUpsertChange,
    RawChat,
    RawMessage,
)
from app.protocol.payloads import (
    IngestDeltaPayload,
    IngestSnapshotBeginPayload,
    IngestSnapshotChunkPayload,
    IngestSnapshotCommitPayload,
    SnapshotRecordCounts,
)
from tests.state_models.brain_ingestion_model import (
    ModelChatDeleteCommand,
    ModelChatUpsertCommand,
    ModelCoverageObservedCommand,
    ModelMessageDeleteCommand,
    ModelMessageUpsertCommand,
    ModelSnapshotBeginCommand,
    ModelSnapshotChunkCommand,
    ModelSnapshotCommitCommand,
    ModelStreamKey,
)


@dataclass(frozen=True, slots=True)
class ProductionTransitionOutcome:
    disposition: Literal["accepted", "duplicate", "gap", "rejected"]
    committed_source_seq: int
    code: str | None = None
    retryable: bool = False
    snapshot_id: UUID | None = None
    next_expected_chunk_index: int | None = None
    snapshot_committed: bool = False
    canonical_revision: int | None = None
    invariant_violation: str | None = None


@dataclass(frozen=True)
class ProductionStateSnapshot:
    checkpoint: int | None
    canonical_revision: int
    active_chats: dict[str, dict[str, Any]]
    active_messages: dict[str, dict[str, Any]]
    tombstones: frozenset[tuple[str, str]]
    pending_snapshot: dict[str, Any] | None
    staged_record_counts: dict[str, int]
    coverage: dict[str, Any]


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _canonical_time(value: str | None) -> str | None:
    parsed = _parse_iso(value)
    return None if parsed is None else parsed.isoformat()


def _to_stream_key(key: ModelStreamKey) -> StreamKey:
    return StreamKey(
        creator_account_id=key.creator_account_id,
        agent_installation_id=key.agent_installation_id,
        agent_stream_id=key.agent_stream_id,
    )

class ProductionBrainAdapter:
    """Thin adapter over HistoryRepository for stateful verification."""

    def __init__(
        self,
        history: HistoryRepository,
        database: CanonicalSQLite,
        connection_id: UUID | None = None,
        fencing_token: str = "fence-production-adapter",
    ) -> None:
        self.history = history
        self.database = database
        self.connection_id = connection_id or uuid4()
        self.fencing_token = fencing_token

    @staticmethod
    def _observe_coverage(conn: Any, account_id: str) -> dict[str, Any]:
        """Read the public coverage projection on the existing observation connection."""
        head = conn.execute(
            """SELECT active_generation_id,last_complete_generation_id
               FROM account_coverage_heads WHERE creator_account_id=?""",
            (account_id,),
        ).fetchone()
        configured = conn.execute(
            """SELECT desired_state,effective_state FROM history_settings
               WHERE creator_account_id=?""",
            (account_id,),
        ).fetchone()
        desired = None if configured is None else configured[0]
        effective = None if configured is None else configured[1]
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
        generation = conn.execute(
            """SELECT generation_id,state,as_of,closed_at,reason_code
               FROM coverage_generations
               WHERE creator_account_id=? AND generation_id=?""",
            (account_id, head[0]),
        ).fetchone()
        counts = conn.execute(
            """SELECT COUNT(*),SUM(CASE WHEN history_started_at IS NOT NULL
                   AND head_reconciled_through IS NOT NULL THEN 1 ELSE 0 END)
               FROM coverage_members WHERE creator_account_id=? AND generation_id=?""",
            (account_id, head[0]),
        ).fetchone()
        state = generation[1]
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
            completed = conn.execute(
                """SELECT as_of FROM coverage_generations
                   WHERE creator_account_id=? AND generation_id=?""",
                (account_id, head[1]),
            ).fetchone()
            if completed is not None:
                complete_as_of = completed[0]
        return {
            "status": "complete" if state == "complete" else "partial",
            "phase": phase,
            "generation_id": generation[0],
            "as_of": generation[2],
            "discovered_conversations": int(counts[0] or 0),
            "complete_conversations": int(counts[1] or 0),
            "complete_as_of": complete_as_of,
            "reason": reason,
        }

    def observe_state(self, key: ModelStreamKey) -> ProductionStateSnapshot:
        """Narrow test observation seam over SQLite tables for state comparison."""
        prod_key = _to_stream_key(key)
        with self.database.read() as conn:
            checkpoint_row = conn.execute(
                """SELECT committed_source_seq FROM ingest_checkpoints
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?""",
                prod_key.sql(),
            ).fetchone()
            cp = None if checkpoint_row is None else int(checkpoint_row[0])
            revision_row = conn.execute(
                "SELECT canonical_revision FROM account_heads WHERE creator_account_id=?",
                (key.creator_account_id,),
            ).fetchone()
            rev = 0 if revision_row is None else int(revision_row[0])
            pending_row = conn.execute(
                """SELECT snapshot_id,starting_checkpoint,through_seq,chunk_count,
                          expected_chats,expected_messages,expected_coverage_evidence,next_chunk_index FROM snapshot_uploads
                   WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=?
                     AND state='staging' ORDER BY created_at DESC LIMIT 1""",
                prod_key.sql(),
            ).fetchone()
            if pending_row is None:
                pending = None
            else:
                chunk_rows = conn.execute(
                    """SELECT chunk_index,entity_kind,fingerprint FROM snapshot_chunks
                       WHERE creator_account_id=? AND agent_installation_id=? AND agent_stream_id=? AND snapshot_id=?
                       ORDER BY chunk_index""", (*prod_key.sql(), pending_row[0])).fetchall()
                pending = {"snapshot_id": UUID(pending_row[0]), "starting_checkpoint": None if pending_row[1] is None else int(pending_row[1]),
                           "through_seq": int(pending_row[2]), "chunk_count": int(pending_row[3]),
                           "expected": (int(pending_row[4]), int(pending_row[5]), int(pending_row[6])),
                           "next_chunk_index": int(pending_row[7]),
                           "chunks": tuple((int(row[0]), str(row[1]), str(row[2])) for row in chunk_rows)}
            chat_rows = conn.execute(
                """SELECT chat_id, record_kind, platform_user_id, display_name, upstream_updated_at
                   FROM account_chats
                   WHERE creator_account_id = ? AND is_deleted = 0
                   ORDER BY chat_id""",
                (key.creator_account_id,),
            ).fetchall()
            active_chats = {
                str(row[0]): {
                    "chat_id": str(row[0]),
                    "record_kind": str(row[1]),
                    "platform_user_id": None if row[2] is None else str(row[2]),
                    "display_name": None if row[3] is None else str(row[3]),
                    "updated_at": _canonical_time(None if row[4] is None else str(row[4])),
                }
                for row in chat_rows
            }

            msg_rows = conn.execute(
                """SELECT message_id, chat_id, sender_platform_user_id, text, sent_at, direction
                   FROM account_messages
                   WHERE creator_account_id = ? AND is_deleted = 0
                   ORDER BY message_id""",
                (key.creator_account_id,),
            ).fetchall()
            active_messages = {
                str(row[0]): {
                    "message_id": str(row[0]),
                    "chat_id": str(row[1]),
                    "sender_platform_user_id": str(row[2]),
                    "text": str(row[3]),
                    "sent_at": _canonical_time(str(row[4])),
                    "direction": str(row[5]),
                }
                for row in msg_rows
            }

            tombstone_rows = conn.execute(
                """SELECT entity_kind, entity_id FROM entity_tombstones
                   WHERE creator_account_id = ?""",
                (key.creator_account_id,),
            ).fetchall()
            tombstones = frozenset((str(row[0]), str(row[1])) for row in tombstone_rows)

            staged_chats_count = conn.execute(
                """SELECT COUNT(*) FROM snapshot_chat_records
                   WHERE creator_account_id = ? AND agent_installation_id = ? AND agent_stream_id = ?""",
                prod_key.sql(),
            ).fetchone()[0]
            staged_msgs_count = conn.execute(
                """SELECT COUNT(*) FROM snapshot_message_records
                   WHERE creator_account_id = ? AND agent_installation_id = ? AND agent_stream_id = ?""",
                prod_key.sql(),
            ).fetchone()[0]
            staged_cov_count = conn.execute(
                """SELECT COUNT(*) FROM snapshot_coverage_records
                   WHERE creator_account_id = ? AND agent_installation_id = ? AND agent_stream_id = ?""",
                prod_key.sql(),
            ).fetchone()[0]

            staged_record_counts = {
                "chats": int(staged_chats_count),
                "messages": int(staged_msgs_count),
                "coverage_evidence": int(staged_cov_count),
            }
            coverage = self._observe_coverage(conn, key.creator_account_id)

        return ProductionStateSnapshot(
            checkpoint=cp,
            canonical_revision=rev,
            active_chats=active_chats,
            active_messages=active_messages,
            tombstones=tombstones,
            pending_snapshot=pending,
            staged_record_counts=staged_record_counts,
            coverage=coverage,
        )

    def begin_snapshot(
        self,
        key: ModelStreamKey,
        cmd: ModelSnapshotBeginCommand,
    ) -> ProductionTransitionOutcome:
        prod_key = _to_stream_key(key)
        payload = IngestSnapshotBeginPayload(
            connection_id=self.connection_id,
            fencing_token=self.fencing_token,
            creator_account_id=key.creator_account_id,
            agent_installation_id=key.agent_installation_id,
            agent_stream_id=key.agent_stream_id,
            snapshot_id=cmd.snapshot_id,
            frame_kind="begin",
            through_seq=cmd.through_seq,
            chunk_count=cmd.chunk_count,
            record_counts=SnapshotRecordCounts(
                chats=cmd.expected_chats,
                messages=cmd.expected_messages,
                coverage_evidence=cmd.expected_coverage,
            ),
            max_frame_bytes=524288,
        )
        try:
            res = self.history.begin_snapshot(prod_key, payload)
            return self._normalize_ingest_result(res)
        except InvariantViolation as exc:
            cp = self.history.checkpoint(prod_key) or 0
            return ProductionTransitionOutcome(
                disposition="rejected",
                committed_source_seq=cp,
                code="invariant_failed",
                invariant_violation=str(exc),
            )

    def add_snapshot_chunk(
        self,
        key: ModelStreamKey,
        cmd: ModelSnapshotChunkCommand,
    ) -> ProductionTransitionOutcome:
        prod_key = _to_stream_key(key)
        payload = IngestSnapshotChunkPayload(
            connection_id=self.connection_id,
            fencing_token=self.fencing_token,
            creator_account_id=key.creator_account_id,
            agent_installation_id=key.agent_installation_id,
            agent_stream_id=key.agent_stream_id,
            snapshot_id=cmd.snapshot_id,
            frame_kind="chunk",
            chunk_index=cmd.chunk_index,
            entity_kind=cmd.entity_kind,
            records=cmd.records,
        )
        try:
            res = self.history.add_snapshot_chunk(prod_key, payload)
            return self._normalize_ingest_result(res)
        except InvariantViolation as exc:
            cp = self.history.checkpoint(prod_key) or 0
            return ProductionTransitionOutcome(
                disposition="rejected",
                committed_source_seq=cp,
                code="invariant_failed",
                invariant_violation=str(exc),
            )

    def commit_snapshot(
        self,
        key: ModelStreamKey,
        cmd: ModelSnapshotCommitCommand,
    ) -> ProductionTransitionOutcome:
        prod_key = _to_stream_key(key)
        payload = IngestSnapshotCommitPayload(
            connection_id=self.connection_id,
            fencing_token=self.fencing_token,
            creator_account_id=key.creator_account_id,
            agent_installation_id=key.agent_installation_id,
            agent_stream_id=key.agent_stream_id,
            snapshot_id=cmd.snapshot_id,
            frame_kind="commit",
            chunk_count=cmd.chunk_count,
        )
        try:
            res = self.history.commit_snapshot(prod_key, payload)
            return self._normalize_ingest_result(res)
        except InvariantViolation as exc:
            cp = self.history.checkpoint(prod_key) or 0
            return ProductionTransitionOutcome(
                disposition="rejected",
                committed_source_seq=cp,
                code="invariant_failed",
                invariant_violation=str(exc),
            )

    def commit_delta(
        self,
        key: ModelStreamKey,
        cmd: Any,
    ) -> ProductionTransitionOutcome:
        prod_key = _to_stream_key(key)
        change_obj = self._translate_change(cmd)
        payload = IngestDeltaPayload(
            connection_id=self.connection_id,
            fencing_token=self.fencing_token,
            creator_account_id=key.creator_account_id,
            agent_installation_id=key.agent_installation_id,
            event_id=cmd.event_id,
            agent_stream_id=key.agent_stream_id,
            source_seq=cmd.source_seq,
            acquisition_origin=cmd.acquisition_origin,  # type: ignore[arg-type]
            change=change_obj,
        )
        try:
            res = self.history.commit_delta(prod_key, payload)
            return self._normalize_ingest_result(res)
        except InvariantViolation as exc:
            cp = self.history.checkpoint(prod_key) or 0
            return ProductionTransitionOutcome(
                disposition="rejected",
                committed_source_seq=cp,
                code="invariant_failed",
                invariant_violation=str(exc),
            )

    def _translate_change(self, cmd: Any) -> Any:
        if isinstance(cmd, ModelChatUpsertCommand):
            return ChatUpsertChange(
                type="chat.upsert",
                chat=RawChat(
                    record_kind=cmd.record_kind,
                    chat_id=cmd.chat_id,
                    platform_user_id=cmd.platform_user_id,
                    display_name=cmd.display_name,
                    updated_at=_parse_iso(cmd.updated_at),  # type: ignore[arg-type]
                ),
            )
        if isinstance(cmd, ModelMessageUpsertCommand):
            return MessageUpsertChange(
                type="message.upsert",
                message=RawMessage(
                    message_id=cmd.message_id,
                    chat_id=cmd.chat_id,
                    sender_platform_user_id=cmd.sender_platform_user_id,
                    text=cmd.text,
                    sent_at=_parse_iso(cmd.sent_at),  # type: ignore[arg-type]
                    direction=cmd.direction,
                ),
            )
        if isinstance(cmd, ModelChatDeleteCommand):
            return ChatDeleteChange(
                type="chat.delete",
                chat_id=cmd.chat_id,
            )
        if isinstance(cmd, ModelMessageDeleteCommand):
            return MessageDeleteChange(
                type="message.delete",
                message_id=cmd.message_id,
                chat_id=cmd.chat_id,
            )
        if isinstance(cmd, ModelCoverageObservedCommand):
            return CoverageObservedChange(
                type="coverage.observed",
                evidence=self._translate_coverage_evidence(cmd),
            )
        raise ValueError(f"Unknown delta command {type(cmd)!r}")

    def _translate_coverage_evidence(self, cmd: ModelCoverageObservedCommand) -> Any:
        ev = cmd.evidence_payload
        t = cmd.coverage_type
        if t == "generation.started":
            return CoverageGenerationStarted(
                type="generation.started",
                generation_id=ev["generation_id"],
                as_of=_parse_iso(ev["as_of"]),  # type: ignore[arg-type]
                authorization_revision=ev["authorization_revision"],
            )
        if t == "inventory.member":
            return CoverageInventoryMember(
                type="inventory.member",
                generation_id=ev["generation_id"],
                conversation_id=ev["conversation_id"],
            )
        if t == "inventory.ended":
            return CoverageInventoryEnded(
                type="inventory.ended",
                generation_id=ev["generation_id"],
                observed_at=_parse_iso(ev["observed_at"]),  # type: ignore[arg-type]
            )
        if t == "conversation.history_started":
            return CoverageConversationHistoryStarted(
                type="conversation.history_started",
                generation_id=ev["generation_id"],
                conversation_id=ev["conversation_id"],
                earliest_observed_at=_parse_iso(ev.get("earliest_observed_at")),
                observed_at=_parse_iso(ev["observed_at"]),  # type: ignore[arg-type]
            )
        if t == "conversation.head_reconciled":
            return CoverageConversationHeadReconciled(
                type="conversation.head_reconciled",
                generation_id=ev["generation_id"],
                conversation_id=ev["conversation_id"],
                reconciled_through=_parse_iso(ev["reconciled_through"]),  # type: ignore[arg-type]
            )
        if t == "generation.closed":
            return CoverageGenerationClosed(
                type="generation.closed",
                generation_id=ev["generation_id"],
                closed_at=_parse_iso(ev["closed_at"]),  # type: ignore[arg-type]
            )
        raise ValueError(f"Unknown coverage evidence type {t!r}")

    def _normalize_ingest_result(self, res: IngestResult) -> ProductionTransitionOutcome:
        return ProductionTransitionOutcome(
            disposition=res.status,
            committed_source_seq=res.committed_source_seq,
            code=res.code,
            retryable=res.retryable,
            snapshot_id=res.snapshot_id,
            next_expected_chunk_index=res.next_expected_chunk_index,
            snapshot_committed=res.snapshot_committed,
            canonical_revision=res.canonical_revision,
            invariant_violation=None,
        )
