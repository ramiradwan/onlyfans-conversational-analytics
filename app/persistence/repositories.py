"""SQLite implementations of the existing canonical repository contracts."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.persistence.database import CanonicalSQLite
from app.protocol import AgentConfigDocumentResponse
from app.services.agent_configuration import (
    AgentConfigRepository,
    ConfigInstallationRecord,
    config_document_digest,
)
from app.services.command_execution import (
    CommandRecord,
    CommandRepository,
    CommandResultReceipt,
    CommandResultRecord,
    CommandTransition,
)
from app.transport.ingestion import (
    AccountReadModel,
    IngestionRepository,
    RawStreamState,
    StoredEvent,
    StoredSnapshot,
    StreamKey,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("persisted timestamps must include a timezone")
    return value.isoformat()


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _key(key: StreamKey) -> tuple[str, str, str]:
    return (
        key.creator_account_id,
        str(key.agent_installation_id),
        str(key.agent_stream_id),
    )


class SQLiteIngestionRepository(IngestionRepository):
    """Relational canonical ingestion state with atomic read-model replacement."""

    def __init__(self, database: CanonicalSQLite) -> None:
        self.database = database

    def checkpoint(self, key: StreamKey) -> int | None:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT committed_source_seq FROM ingest_checkpoints
                WHERE creator_account_id = ? AND agent_installation_id = ?
                  AND agent_stream_id = ?
                """,
                _key(key),
            ).fetchone()
            return None if row is None else int(row[0])

    def stream(self, key: StreamKey) -> RawStreamState | None:
        parameters = _key(key)
        with self.database.read() as connection:
            checkpoint = connection.execute(
                """
                SELECT committed_source_seq FROM ingest_checkpoints
                WHERE creator_account_id = ? AND agent_installation_id = ?
                  AND agent_stream_id = ?
                """,
                parameters,
            ).fetchone()
            if checkpoint is None:
                return None
            state = RawStreamState(checkpoint=int(checkpoint[0]))
            for row in connection.execute(
                """
                SELECT chat_id, document_json FROM canonical_chats
                WHERE creator_account_id = ? AND agent_installation_id = ?
                  AND agent_stream_id = ?
                ORDER BY chat_id
                """,
                parameters,
            ):
                state.chats[row["chat_id"]] = _load(row["document_json"])
            for row in connection.execute(
                """
                SELECT message_id, document_json FROM canonical_messages
                WHERE creator_account_id = ? AND agent_installation_id = ?
                  AND agent_stream_id = ?
                ORDER BY message_id
                """,
                parameters,
            ):
                state.messages[row["message_id"]] = _load(row["document_json"])
            for row in connection.execute(
                """
                SELECT event_id, source_seq, fingerprint, event_json
                FROM raw_ingest_events
                WHERE creator_account_id = ? AND agent_installation_id = ?
                  AND agent_stream_id = ?
                ORDER BY source_seq
                """,
                parameters,
            ):
                event_id = UUID(row["event_id"])
                state.events_by_id[event_id] = StoredEvent(
                    event_id=event_id,
                    source_seq=int(row["source_seq"]),
                    fingerprint=row["fingerprint"],
                    payload=_load(row["event_json"]),
                )
                state.event_ids_by_seq[int(row["source_seq"])] = event_id
            for row in connection.execute(
                """
                SELECT snapshot_id, through_seq, fingerprint
                FROM raw_ingest_snapshots
                WHERE creator_account_id = ? AND agent_installation_id = ?
                  AND agent_stream_id = ?
                ORDER BY committed_at, snapshot_id
                """,
                parameters,
            ):
                snapshot_id = UUID(row["snapshot_id"])
                state.snapshots_by_id[snapshot_id] = StoredSnapshot(
                    snapshot_id=snapshot_id,
                    through_seq=int(row["through_seq"]),
                    fingerprint=row["fingerprint"],
                )
            return state

    def account_read_model(self, creator_account_id: str) -> AccountReadModel:
        with self.database.read() as connection:
            return self._account_read_model(connection, creator_account_id)

    @staticmethod
    def _account_read_model(
        connection: sqlite3.Connection, creator_account_id: str
    ) -> AccountReadModel:
        row = connection.execute(
            "SELECT view_revision FROM account_read_models WHERE creator_account_id = ?",
            (creator_account_id,),
        ).fetchone()
        if row is None:
            return AccountReadModel()
        account = AccountReadModel(view_revision=int(row[0]))
        chats = connection.execute(
            """
            SELECT conversation_id, document_json FROM read_model_chats
            WHERE creator_account_id = ? ORDER BY conversation_id
            """,
            (creator_account_id,),
        ).fetchall()
        for chat in chats:
            conversation = _load(chat["document_json"])
            conversation["messages"] = []
            account.conversations[chat["conversation_id"]] = conversation
        for message in connection.execute(
            """
            SELECT conversation_id, document_json FROM read_model_messages
            WHERE creator_account_id = ? ORDER BY conversation_id, ordinal
            """,
            (creator_account_id,),
        ):
            account.conversations[message["conversation_id"]]["messages"].append(
                _load(message["document_json"])
            )
        return account

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
        parameters = _key(key)
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT committed_source_seq FROM ingest_checkpoints
                WHERE creator_account_id = ? AND agent_installation_id = ?
                  AND agent_stream_id = ?
                """,
                parameters,
            ).fetchone()
            current_checkpoint = 0 if row is None else int(row[0])
            if current_checkpoint != expected_checkpoint:
                return False

            current_account = self._account_read_model(connection, key.creator_account_id)
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

            self._allocate_view_revision(
                connection,
                key.creator_account_id,
                current_account,
                account,
            )

            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT OR IGNORE INTO ingest_streams (
                    creator_account_id, agent_installation_id, agent_stream_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (*parameters, now),
            )
            self._store_deduplication(connection, key, stream, now)
            self._replace_canonical_state(connection, key, stream)
            self._replace_read_model(connection, key.creator_account_id, account)
            connection.execute(
                """
                INSERT INTO ingest_checkpoints (
                    creator_account_id, agent_installation_id, agent_stream_id,
                    committed_source_seq, committed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (creator_account_id, agent_installation_id, agent_stream_id)
                DO UPDATE SET committed_source_seq = excluded.committed_source_seq,
                              committed_at = excluded.committed_at
                """,
                (*parameters, stream.checkpoint, now),
            )
        return True

    @staticmethod
    def _allocate_view_revision(
        connection: sqlite3.Connection,
        creator_account_id: str,
        current: AccountReadModel,
        replacement: AccountReadModel,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO account_read_models (
                creator_account_id, view_revision
            ) VALUES (?, 0)
            """,
            (creator_account_id,),
        )
        if replacement.view_revision == current.view_revision:
            return
        allocated = connection.execute(
            """
            UPDATE account_read_models
            SET view_revision = view_revision + 1
            WHERE creator_account_id = ? AND view_revision = ?
            RETURNING view_revision
            """,
            (creator_account_id, current.view_revision),
        ).fetchone()
        if allocated is None or int(allocated[0]) != replacement.view_revision:
            raise RuntimeError("Could not allocate the next contiguous view revision")

    @staticmethod
    def _store_deduplication(
        connection: sqlite3.Connection,
        key: StreamKey,
        stream: RawStreamState,
        committed_at: str,
    ) -> None:
        parameters = _key(key)
        for event in stream.events_by_id.values():
            connection.execute(
                """
                INSERT OR IGNORE INTO raw_ingest_events (
                    creator_account_id, agent_installation_id, agent_stream_id,
                    event_id, source_seq, fingerprint, event_json, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *parameters,
                    str(event.event_id),
                    event.source_seq,
                    event.fingerprint,
                    _json(event.payload) if event.payload is not None else None,
                    committed_at,
                ),
            )
        snapshot_json = _json(
            {"through_seq": stream.checkpoint, "chats": stream.chats, "messages": stream.messages}
        )
        for snapshot in stream.snapshots_by_id.values():
            connection.execute(
                """
                INSERT OR IGNORE INTO raw_ingest_snapshots (
                    creator_account_id, agent_installation_id, agent_stream_id,
                    snapshot_id, through_seq, fingerprint, snapshot_json, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *parameters,
                    str(snapshot.snapshot_id),
                    snapshot.through_seq,
                    snapshot.fingerprint,
                    snapshot_json,
                    committed_at,
                ),
            )

    @staticmethod
    def _replace_canonical_state(
        connection: sqlite3.Connection, key: StreamKey, stream: RawStreamState
    ) -> None:
        parameters = _key(key)
        connection.execute(
            """
            DELETE FROM canonical_messages
            WHERE creator_account_id = ? AND agent_installation_id = ?
              AND agent_stream_id = ?
            """,
            parameters,
        )
        connection.execute(
            """
            DELETE FROM canonical_chats
            WHERE creator_account_id = ? AND agent_installation_id = ?
              AND agent_stream_id = ?
            """,
            parameters,
        )
        connection.executemany(
            """
            INSERT INTO canonical_chats (
                creator_account_id, agent_installation_id, agent_stream_id,
                chat_id, document_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [(*parameters, chat_id, _json(chat)) for chat_id, chat in stream.chats.items()],
        )
        connection.executemany(
            """
            INSERT INTO canonical_messages (
                creator_account_id, agent_installation_id, agent_stream_id,
                message_id, chat_id, document_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (*parameters, message_id, message["chat_id"], _json(message))
                for message_id, message in stream.messages.items()
            ],
        )

    @staticmethod
    def _replace_read_model(
        connection: sqlite3.Connection,
        creator_account_id: str,
        account: AccountReadModel,
    ) -> None:
        row = connection.execute(
            """
            SELECT view_revision FROM account_read_models
            WHERE creator_account_id = ?
            """,
            (creator_account_id,),
        ).fetchone()
        if row is None or int(row[0]) != account.view_revision:
            raise RuntimeError("Read-model content does not match its allocated revision")
        connection.execute(
            "DELETE FROM read_model_messages WHERE creator_account_id = ?",
            (creator_account_id,),
        )
        connection.execute(
            "DELETE FROM read_model_chats WHERE creator_account_id = ?",
            (creator_account_id,),
        )
        for conversation_id, original in account.conversations.items():
            conversation = deepcopy(original)
            messages = conversation.pop("messages", [])
            connection.execute(
                """
                INSERT INTO read_model_chats (
                    creator_account_id, conversation_id, document_json
                ) VALUES (?, ?, ?)
                """,
                (creator_account_id, conversation_id, _json(conversation)),
            )
            connection.executemany(
                """
                INSERT INTO read_model_messages (
                    creator_account_id, conversation_id, message_id, ordinal, document_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        creator_account_id,
                        conversation_id,
                        message["message_id"],
                        index,
                        _json(message),
                    )
                    for index, message in enumerate(messages)
                ],
            )

    def reset(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM account_read_models")
            connection.execute("DELETE FROM ingest_streams")


class SQLiteAgentConfigRepository(AgentConfigRepository):
    """Immutable configuration documents and required/applied installation state."""

    def __init__(self, database: CanonicalSQLite) -> None:
        self.database = database

    @staticmethod
    def _sequence(revision: str) -> int:
        prefix, separator, value = revision.rpartition("-")
        if not separator or prefix != "config" or not value.isdigit():
            raise ValueError("Config revisions must use the monotonic config-N form")
        return int(value)

    def add_document(self, document: AgentConfigDocumentResponse) -> None:
        with self.database.transaction() as connection:
            self._insert_document(connection, document)

    def publish_document(self, document: AgentConfigDocumentResponse) -> None:
        """Insert and require a publication in one SQLite transaction."""
        with self.database.transaction() as connection:
            self._insert_document(connection, document)
            self._require(connection, document.creator_account_id, document.config_revision)

    def _insert_document(
        self, connection: sqlite3.Connection, document: AgentConfigDocumentResponse
    ) -> None:
        existing = connection.execute(
            """
            SELECT 1 FROM config_documents
            WHERE creator_account_id = ? AND config_revision = ?
            """,
            (document.creator_account_id, document.config_revision),
        ).fetchone()
        if existing is not None:
            raise ValueError(
                f"Configuration revision {document.config_revision} is immutable"
            )
        if document.digest != config_document_digest(document):
            raise ValueError("Configuration digest does not match its immutable content")
        sequence = self._sequence(document.config_revision)
        last = connection.execute(
            """
            SELECT MAX(revision_sequence) FROM config_documents
            WHERE creator_account_id = ?
            """,
            (document.creator_account_id,),
        ).fetchone()[0]
        if last is not None and sequence <= int(last):
            raise ValueError("Configuration revision must increase monotonically")
        connection.execute(
            """
            INSERT INTO config_documents (
                creator_account_id, config_revision, revision_sequence,
                config_schema_version, digest, etag, issued_at, document_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document.creator_account_id,
                document.config_revision,
                sequence,
                document.config_schema_version,
                document.digest,
                document.etag,
                _timestamp(document.issued_at),
                document.model_dump_json(),
            ),
        )

    def document(
        self, creator_account_id: str, revision: str
    ) -> AgentConfigDocumentResponse | None:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT document_json FROM config_documents
                WHERE creator_account_id = ? AND config_revision = ?
                """,
                (creator_account_id, revision),
            ).fetchone()
            return (
                None
                if row is None
                else AgentConfigDocumentResponse.model_validate_json(row[0])
            )

    def required_document(self, creator_account_id: str) -> AgentConfigDocumentResponse:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT document_json
                FROM config_required
                JOIN config_documents USING (creator_account_id, config_revision)
                WHERE creator_account_id = ?
                """,
                (creator_account_id,),
            ).fetchone()
            if row is None:
                raise LookupError(
                    f"No required Agent configuration for {creator_account_id}"
                )
            return AgentConfigDocumentResponse.model_validate_json(row[0])

    def next_revision(self, creator_account_id: str) -> str:
        with self.database.read() as connection:
            last = connection.execute(
                """
                SELECT MAX(revision_sequence) FROM config_documents
                WHERE creator_account_id = ?
                """,
                (creator_account_id,),
            ).fetchone()[0]
            return f"config-{(0 if last is None else int(last)) + 1}"

    def set_required(self, creator_account_id: str, revision: str) -> None:
        with self.database.transaction() as connection:
            self._set_required(connection, creator_account_id, revision)

    @staticmethod
    def _set_required(
        connection: sqlite3.Connection, creator_account_id: str, revision: str
    ) -> None:
        known = connection.execute(
            """
            SELECT 1 FROM config_documents
            WHERE creator_account_id = ? AND config_revision = ?
            """,
            (creator_account_id, revision),
        ).fetchone()
        if known is None:
            raise LookupError(f"Unknown configuration revision {revision}")
        connection.execute(
            """
            INSERT INTO config_required (creator_account_id, config_revision)
            VALUES (?, ?)
            ON CONFLICT (creator_account_id)
            DO UPDATE SET config_revision = excluded.config_revision
            """,
            (creator_account_id, revision),
        )

    def installation(
        self, creator_account_id: str, agent_installation_id: UUID
    ) -> ConfigInstallationRecord | None:
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT required_config_revision, applied_config_revision, last_failure
                FROM config_installations
                WHERE creator_account_id = ? AND agent_installation_id = ?
                """,
                (creator_account_id, str(agent_installation_id)),
            ).fetchone()
            if row is None:
                return None
            return ConfigInstallationRecord(
                creator_account_id=creator_account_id,
                agent_installation_id=agent_installation_id,
                required_config_revision=row["required_config_revision"],
                applied_config_revision=row["applied_config_revision"],
                last_failure=row["last_failure"],
            )

    def save_installation(self, record: ConfigInstallationRecord) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO config_installations (
                    creator_account_id, agent_installation_id,
                    required_config_revision, applied_config_revision, last_failure
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (creator_account_id, agent_installation_id)
                DO UPDATE SET required_config_revision = excluded.required_config_revision,
                              applied_config_revision = excluded.applied_config_revision,
                              last_failure = excluded.last_failure
                """,
                (
                    record.creator_account_id,
                    str(record.agent_installation_id),
                    record.required_config_revision,
                    record.applied_config_revision,
                    record.last_failure,
                ),
            )

    def require_for_account(self, creator_account_id: str, revision: str) -> None:
        with self.database.transaction() as connection:
            self._require(connection, creator_account_id, revision)

    def _require(
        self, connection: sqlite3.Connection, creator_account_id: str, revision: str
    ) -> None:
        self._set_required(connection, creator_account_id, revision)
        connection.execute(
            """
            UPDATE config_installations SET required_config_revision = ?
            WHERE creator_account_id = ?
            """,
            (revision, creator_account_id),
        )

    def reset(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM config_installations")
            connection.execute("DELETE FROM config_required")
            connection.execute("DELETE FROM config_documents")


class SQLiteCommandRepository(CommandRepository):
    """Copy-isolated command audit records persisted as one transaction per save."""

    def __init__(self, database: CanonicalSQLite) -> None:
        self.database = database

    def get(self, command_id: UUID) -> CommandRecord | None:
        with self.database.read() as connection:
            return self._get(connection, command_id)

    @staticmethod
    def _get(connection: sqlite3.Connection, command_id: UUID) -> CommandRecord | None:
        row = connection.execute(
            "SELECT * FROM commands WHERE command_id = ?", (str(command_id),)
        ).fetchone()
        if row is None:
            return None
        transitions = [
            CommandTransition(
                state=item["state"],
                occurred_at=_datetime(item["occurred_at"]),
                detail=item["detail"],
            )
            for item in connection.execute(
                """
                SELECT state, occurred_at, detail FROM command_transitions
                WHERE command_id = ? ORDER BY transition_index
                """,
                (str(command_id),),
            )
        ]
        result_row = connection.execute(
            "SELECT * FROM command_results WHERE command_id = ?", (str(command_id),)
        ).fetchone()
        result = (
            None
            if result_row is None
            else CommandResultRecord(
                result_id=UUID(result_row["result_id"]),
                status=result_row["status"],
                completed_at=_datetime(result_row["completed_at"]),
                output=_load(result_row["output_json"]),
                error=_load(result_row["error_json"]),
                recorded_at=_datetime(result_row["recorded_at"]),
            )
        )
        receipts = [
            CommandResultReceipt(
                result_id=UUID(item["result_id"]),
                received_at=_datetime(item["received_at"]),
                duplicate=bool(item["duplicate"]),
                late=bool(item["late"]),
            )
            for item in connection.execute(
                """
                SELECT result_id, received_at, duplicate, late
                FROM command_result_receipts
                WHERE command_id = ? ORDER BY receipt_index
                """,
                (str(command_id),),
            )
        ]
        return CommandRecord(
            command_id=command_id,
            creator_account_id=row["creator_account_id"],
            action=_load(row["action_json"]),
            deadline=_datetime(row["deadline"]),
            idempotency_policy=row["idempotency_policy"],
            issued_at=_datetime(row["issued_at"]),
            state=row["state"],
            connection_id=(
                None if row["connection_id"] is None else UUID(row["connection_id"])
            ),
            fencing_token=row["fencing_token"],
            delivery_attempts=int(row["delivery_attempts"]),
            failure_reason=row["failure_reason"],
            result=result,
            result_apply_count=int(row["result_apply_count"]),
            transitions=transitions,
            receipts=receipts,
        )

    def save(self, record: CommandRecord) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO commands (
                    command_id, creator_account_id, action_json, deadline,
                    idempotency_policy, issued_at, state, connection_id,
                    fencing_token, delivery_attempts, failure_reason, result_apply_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (command_id) DO UPDATE SET
                    creator_account_id = excluded.creator_account_id,
                    action_json = excluded.action_json,
                    deadline = excluded.deadline,
                    idempotency_policy = excluded.idempotency_policy,
                    issued_at = excluded.issued_at,
                    state = excluded.state,
                    connection_id = excluded.connection_id,
                    fencing_token = excluded.fencing_token,
                    delivery_attempts = excluded.delivery_attempts,
                    failure_reason = excluded.failure_reason,
                    result_apply_count = excluded.result_apply_count
                """,
                (
                    str(record.command_id),
                    record.creator_account_id,
                    _json(record.action),
                    _timestamp(record.deadline),
                    record.idempotency_policy,
                    _timestamp(record.issued_at),
                    record.state,
                    None if record.connection_id is None else str(record.connection_id),
                    record.fencing_token,
                    record.delivery_attempts,
                    record.failure_reason,
                    record.result_apply_count,
                ),
            )
            connection.execute(
                "DELETE FROM command_transitions WHERE command_id = ?",
                (str(record.command_id),),
            )
            connection.executemany(
                """
                INSERT INTO command_transitions (
                    command_id, transition_index, state, occurred_at, detail
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(record.command_id),
                        index,
                        transition.state,
                        _timestamp(transition.occurred_at),
                        transition.detail,
                    )
                    for index, transition in enumerate(record.transitions)
                ],
            )
            connection.execute(
                "DELETE FROM command_results WHERE command_id = ?",
                (str(record.command_id),),
            )
            if record.result is not None:
                connection.execute(
                    """
                    INSERT INTO command_results (
                        command_id, result_id, status, completed_at,
                        output_json, error_json, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(record.command_id),
                        str(record.result.result_id),
                        record.result.status,
                        _timestamp(record.result.completed_at),
                        (
                            None
                            if record.result.output is None
                            else _json(record.result.output)
                        ),
                        (
                            None
                            if record.result.error is None
                            else _json(record.result.error)
                        ),
                        _timestamp(record.result.recorded_at),
                    ),
                )
            connection.execute(
                "DELETE FROM command_result_receipts WHERE command_id = ?",
                (str(record.command_id),),
            )
            connection.executemany(
                """
                INSERT INTO command_result_receipts (
                    command_id, receipt_index, result_id, received_at, duplicate, late
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(record.command_id),
                        index,
                        str(receipt.result_id),
                        _timestamp(receipt.received_at),
                        int(receipt.duplicate),
                        int(receipt.late),
                    )
                    for index, receipt in enumerate(record.receipts)
                ],
            )

    def list(self, creator_account_id: str | None = None) -> list[CommandRecord]:
        with self.database.read() as connection:
            if creator_account_id is None:
                rows = connection.execute(
                    "SELECT command_id FROM commands ORDER BY rowid"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT command_id FROM commands
                    WHERE creator_account_id = ? ORDER BY rowid
                    """,
                    (creator_account_id,),
                ).fetchall()
            return [
                record
                for row in rows
                if (record := self._get(connection, UUID(row["command_id"]))) is not None
            ]

    def reset(self) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM commands")
