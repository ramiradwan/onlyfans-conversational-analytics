"""Creator-controlled archive lifecycle and durable deletion barriers."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from app.persistence import sqlite_api as sqlite3
from app.persistence.database import CanonicalSQLite

WORKING_RAW_MAX_DAYS = 30
PARTICIPANT_ANALYTICS_MAX_DAYS = 90
MANAGED_RECOVERY_MAX_DAYS = 30
PolicyType = Literal["disabled", "finite", "export_and_delete", "indefinite_until_delete"]
ScopeKind = Literal["account", "conversation", "message", "participant"]


class RetentionPolicyError(ValueError):
    """Raised when an archive operation violates a lifecycle invariant."""


@dataclass(frozen=True, slots=True)
class ArchivePolicy:
    creator_account_id: str
    enabled: bool
    policy_type: PolicyType
    finite_horizon_days: int | None
    revision: int
    indefinite_gate_open: bool


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def production_indefinite_gate_open() -> bool:
    return os.getenv("OFCA_CREATOR_VAULT_INDEFINITE_GATE", "closed") == "open"


class CreatorVaultRetention:
    """Canonical archive policy, expiry, import, and deletion operations."""

    def __init__(
        self,
        database: CanonicalSQLite,
        *,
        indefinite_gate: Callable[[], bool] = production_indefinite_gate_open,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database = database
        self._indefinite_gate = indefinite_gate
        self._clock = clock

    def policy(self, creator_account_id: str) -> ArchivePolicy:
        now = _iso(self._clock())
        with self.database.transaction() as connection:
            self._ensure_policy(connection, creator_account_id, now)
            row = connection.execute(
                "SELECT * FROM archive_policies WHERE creator_account_id=?",
                (creator_account_id,),
            ).fetchone()
            assert row is not None
            return self._policy(row)

    def set_policy(
        self,
        creator_account_id: str,
        policy_type: PolicyType,
        *,
        finite_horizon_days: int | None = None,
        creator_action_ref: str,
    ) -> ArchivePolicy:
        if not creator_action_ref.strip():
            raise RetentionPolicyError("creator action reference is required")
        if policy_type == "finite":
            if finite_horizon_days is None or finite_horizon_days <= 0:
                raise RetentionPolicyError("finite archive policy requires a positive horizon")
        elif finite_horizon_days is not None:
            raise RetentionPolicyError("only finite archive policy accepts a horizon")
        gate_open = self._indefinite_gate()
        if policy_type == "indefinite_until_delete" and not gate_open:
            raise RetentionPolicyError("indefinite archive policy production gate is closed")
        current = self._clock()
        now = _iso(current)
        with self.database.transaction() as connection:
            self._ensure_policy(connection, creator_account_id, now)
            revision = int(connection.execute(
                "SELECT policy_revision FROM archive_policies WHERE creator_account_id=?",
                (creator_account_id,),
            ).fetchone()[0]) + 1
            enabled = policy_type != "disabled"
            connection.execute(
                """UPDATE archive_policies
                   SET vault_enabled=?,policy_type=?,finite_horizon_days=?,activated_at=?,
                       creator_action_ref=?,policy_revision=?,indefinite_gate_state=?,updated_at=?
                   WHERE creator_account_id=?""",
                (int(enabled), policy_type, finite_horizon_days, now if enabled else None,
                 creator_action_ref, revision,
                 "open" if policy_type == "indefinite_until_delete" else "closed",
                 now, creator_account_id),
            )
            if enabled:
                connection.execute(
                    """INSERT INTO archive_membership(
                           creator_account_id,message_id,source_event_at,working_purpose,
                           vault_purpose,vault_policy_revision,updated_at)
                       SELECT creator_account_id,message_id,sent_at,1,1,?,?
                       FROM account_messages WHERE creator_account_id=? AND is_deleted=0
                       ON CONFLICT(creator_account_id,message_id) DO UPDATE SET
                           vault_purpose=1,vault_policy_revision=excluded.vault_policy_revision,
                           updated_at=excluded.updated_at""",
                    (revision, now, creator_account_id),
                )
            else:
                connection.execute(
                    """UPDATE archive_membership SET vault_purpose=0,
                       vault_policy_revision=NULL,updated_at=? WHERE creator_account_id=?""",
                    (now, creator_account_id),
                )
            if policy_type == "finite":
                self._enforce_in_transaction(connection, creator_account_id, current)
            row = connection.execute(
                "SELECT * FROM archive_policies WHERE creator_account_id=?",
                (creator_account_id,),
            ).fetchone()
            assert row is not None
            return self._policy(row)

    def unlink(self, creator_account_id: str, *, preserve_archive: bool):
        if preserve_archive:
            return self.policy(creator_account_id)
        return self.delete_all(creator_account_id, provenance="unlink_delete")

    def delete_message(self, creator_account_id: str, message_id: str, *, provenance: str = "creator_delete") -> int:
        return self._delete_scope(creator_account_id, "message", message_id, provenance)

    def delete_conversation(self, creator_account_id: str, conversation_id: str, *, provenance: str = "creator_delete") -> int:
        return self._delete_scope(creator_account_id, "conversation", conversation_id, provenance)

    def delete_participant(self, creator_account_id: str, participant_id: str, *, provenance: str = "creator_delete") -> int:
        return self._delete_scope(creator_account_id, "participant", participant_id, provenance)

    def delete_all(self, creator_account_id: str, *, provenance: str = "creator_delete") -> int:
        return self._delete_scope(creator_account_id, "account", "*", provenance)

    def enforce(self, creator_account_id: str, *, now: datetime | None = None) -> list[str]:
        current = now or self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        with self.database.transaction() as connection:
            return self._enforce_in_transaction(connection, creator_account_id, current)

    def _enforce_in_transaction(
        self, connection: sqlite3.Connection, creator_account_id: str, current: datetime
    ) -> list[str]:
        expired: list[str] = []
        self._ensure_policy(connection, creator_account_id, _iso(current))
        row = connection.execute(
            "SELECT * FROM archive_policies WHERE creator_account_id=?",
            (creator_account_id,),
        ).fetchone()
        assert row is not None
        policy = self._policy(row)
        if policy.policy_type == "indefinite_until_delete" and not self._indefinite_gate():
            raise RetentionPolicyError("indefinite archive policy production gate is closed")
        messages = connection.execute(
            """SELECT m.message_id,a.source_event_at,a.working_purpose,a.vault_purpose
               FROM archive_membership a JOIN account_messages m
               ON m.creator_account_id=a.creator_account_id AND m.message_id=a.message_id
               WHERE a.creator_account_id=? AND m.is_deleted=0""",
            (creator_account_id,),
        ).fetchall()
        for message in messages:
            source_at = datetime.fromisoformat(message["source_event_at"])
            working_valid = bool(message["working_purpose"]) and (
                source_at + timedelta(days=WORKING_RAW_MAX_DAYS) > current
            )
            vault_valid = False
            if bool(message["vault_purpose"]) and policy.enabled:
                if policy.policy_type == "indefinite_until_delete":
                    vault_valid = True
                elif policy.policy_type == "finite":
                    assert policy.finite_horizon_days is not None
                    vault_valid = source_at + timedelta(days=policy.finite_horizon_days) > current
            if not working_valid and not vault_valid:
                self._record_barrier(connection, creator_account_id, "message",
                                     str(message["message_id"]), "retention_expiry", current)
                self._purge_scope(connection, creator_account_id, "message",
                                  str(message["message_id"]))
                expired.append(str(message["message_id"]))
        cutoff = _iso(current - timedelta(days=MANAGED_RECOVERY_MAX_DAYS))
        connection.execute("DELETE FROM raw_ingest_events WHERE creator_account_id=? AND committed_at<?",
                           (creator_account_id, cutoff))
        connection.execute("DELETE FROM snapshot_uploads WHERE creator_account_id=? AND created_at<?",
                           (creator_account_id, cutoff))
        connection.execute("DELETE FROM committed_snapshots WHERE creator_account_id=? AND committed_at<?",
                           (creator_account_id, cutoff))
        if expired:
            self._queue_reseed(connection, creator_account_id, _iso(current))
        return expired

    def import_message(self, creator_account_id: str, *, action_ref: str,
                       message: dict[str, Any], conversation: dict[str, Any]) -> None:
        if not action_ref.strip():
            raise RetentionPolicyError("creator import action reference is required")
        now = _iso(self._clock())
        with self.database.transaction() as connection:
            self._ensure_policy(connection, creator_account_id, now)
            enabled = connection.execute(
                "SELECT vault_enabled FROM archive_policies WHERE creator_account_id=?",
                (creator_account_id,),
            ).fetchone()
            if enabled is None or not bool(enabled[0]):
                raise RetentionPolicyError("creator import requires an enabled archive policy")
            chat_id = str(conversation["chat_id"])
            chat = {
                "chat_id": chat_id,
                "record_kind": conversation.get("record_kind", "full"),
                "platform_user_id": conversation.get("platform_user_id"),
                "display_name": conversation.get("display_name"),
                "updated_at": conversation.get("updated_at", now),
            }
            connection.execute(
                """INSERT INTO account_chats(
                       creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                       upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                       winning_event_id,is_deleted,updated_at,lifecycle_origin,lifecycle_started_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,?,'creator_import',?)
                   ON CONFLICT(creator_account_id,chat_id) DO NOTHING""",
                (creator_account_id, chat_id, chat["record_kind"], chat["platform_user_id"],
                 chat["display_name"], chat["updated_at"], _hash(chat), 0, 0,
                 f"creator-import:{action_ref}", now, now),
            )
            clean = dict(message)
            clean.pop("record_kind", None)
            connection.execute(
                """INSERT INTO account_messages(
                       creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                       direction,upstream_updated_at,content_hash,winning_stream_epoch,
                       winning_source_seq,winning_event_id,is_deleted,updated_at,
                       lifecycle_origin,lifecycle_started_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,'creator_import',?)""",
                (creator_account_id, clean["message_id"], chat_id,
                 clean["sender_platform_user_id"], clean["text"], clean["sent_at"],
                 clean["direction"], clean.get("upstream_updated_at"), _hash(clean), 0, 0,
                 f"creator-import:{action_ref}", now, now),
            )
            self._queue_reseed(connection, creator_account_id, now)

    def barriers(self, creator_account_id: str) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            return [dict(row) for row in connection.execute(
                """SELECT * FROM deletion_barriers WHERE creator_account_id=?
                   ORDER BY deletion_revision,scope_kind,scope_key""", (creator_account_id,))]

    def _delete_scope(self, creator_account_id: str, scope_kind: ScopeKind,
                      scope_key: str, provenance: str) -> int:
        if not provenance.strip():
            raise RetentionPolicyError("deletion provenance is required")
        now = self._clock()
        with self.database.transaction() as connection:
            self._ensure_policy(connection, creator_account_id, _iso(now))
            revision = self._record_barrier(connection, creator_account_id, scope_kind,
                                            scope_key, provenance, now)
            self._purge_scope(connection, creator_account_id, scope_kind, scope_key)
            self._queue_reseed(connection, creator_account_id, _iso(now))
            return revision

    @staticmethod
    def _ensure_policy(connection: sqlite3.Connection, creator_account_id: str, now: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO account_heads(creator_account_id,updated_at) VALUES (?,?)",
            (creator_account_id, now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO archive_policies(creator_account_id,updated_at) VALUES (?,?)",
            (creator_account_id, now),
        )

    @staticmethod
    def _record_barrier(connection: sqlite3.Connection, creator_account_id: str,
                        scope_kind: ScopeKind, scope_key: str, provenance: str,
                        now: datetime) -> int:
        revision = int(connection.execute(
            "SELECT COALESCE(MAX(deletion_revision),0)+1 FROM deletion_barriers WHERE creator_account_id=?",
            (creator_account_id,),
        ).fetchone()[0])
        connection.execute(
            """INSERT INTO deletion_barriers(
                   creator_account_id,scope_kind,scope_key,deletion_revision,deleted_at,provenance)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(creator_account_id,scope_kind,scope_key) DO UPDATE SET
                   deletion_revision=excluded.deletion_revision,deleted_at=excluded.deleted_at,
                   provenance=excluded.provenance""",
            (creator_account_id, scope_kind, scope_key, revision, _iso(now), provenance),
        )
        return revision

    @staticmethod
    def _purge_scope(connection: sqlite3.Connection, creator_account_id: str,
                     scope_kind: ScopeKind, scope_key: str) -> None:
        if scope_kind == "account":
            message_ids = [str(row[0]) for row in connection.execute(
                "SELECT message_id FROM account_messages WHERE creator_account_id=?",
                (creator_account_id,))]
        elif scope_kind == "conversation":
            message_ids = [str(row[0]) for row in connection.execute(
                "SELECT message_id FROM account_messages WHERE creator_account_id=? AND chat_id=?",
                (creator_account_id, scope_key))]
        elif scope_kind == "participant":
            message_ids = [str(row[0]) for row in connection.execute(
                "SELECT message_id FROM account_messages WHERE creator_account_id=? AND sender_platform_user_id=?",
                (creator_account_id, scope_key))]
        else:
            message_ids = [scope_key]
        if scope_kind == "account":
            connection.execute("DELETE FROM raw_ingest_events WHERE creator_account_id=?", (creator_account_id,))
            connection.execute("DELETE FROM snapshot_uploads WHERE creator_account_id=?", (creator_account_id,))
            connection.execute("DELETE FROM committed_snapshots WHERE creator_account_id=?", (creator_account_id,))
        else:
            snapshot_ids: set[tuple[str, str, str]] = set()
            if message_ids:
                marks = ",".join("?" for _ in message_ids)
                for row in connection.execute(
                    f"""SELECT DISTINCT agent_installation_id,agent_stream_id,snapshot_id
                        FROM snapshot_message_records WHERE creator_account_id=?
                        AND message_id IN ({marks})""", (creator_account_id, *message_ids)):
                    snapshot_ids.add((str(row[0]), str(row[1]), str(row[2])))
            if scope_kind == "conversation":
                for row in connection.execute(
                    """SELECT DISTINCT agent_installation_id,agent_stream_id,snapshot_id
                       FROM snapshot_chat_records WHERE creator_account_id=? AND chat_id=?""",
                    (creator_account_id, scope_key)):
                    snapshot_ids.add((str(row[0]), str(row[1]), str(row[2])))
            for installation_id, stream_id, snapshot_id in snapshot_ids:
                connection.execute(
                    """DELETE FROM snapshot_uploads WHERE creator_account_id=?
                       AND agent_installation_id=? AND agent_stream_id=? AND snapshot_id=?""",
                    (creator_account_id, installation_id, stream_id, snapshot_id))
                connection.execute(
                    """DELETE FROM committed_snapshots WHERE creator_account_id=?
                       AND agent_installation_id=? AND agent_stream_id=? AND snapshot_id=?""",
                    (creator_account_id, installation_id, stream_id, snapshot_id))
        if message_ids:
            marks = ",".join("?" for _ in message_ids)
            connection.execute(
                f"DELETE FROM account_messages WHERE creator_account_id=? AND message_id IN ({marks})",
                (creator_account_id, *message_ids))
        if scope_kind == "conversation":
            connection.execute("DELETE FROM account_chats WHERE creator_account_id=? AND chat_id=?",
                               (creator_account_id, scope_key))
        elif scope_kind == "account":
            connection.execute("DELETE FROM account_messages WHERE creator_account_id=?", (creator_account_id,))
            connection.execute("DELETE FROM account_chats WHERE creator_account_id=?", (creator_account_id,))

    @staticmethod
    def _queue_reseed(connection: sqlite3.Connection, creator_account_id: str, now: str) -> None:
        row = connection.execute(
            "SELECT canonical_revision FROM account_heads WHERE creator_account_id=?",
            (creator_account_id,),
        ).fetchone()
        if row is not None:
            connection.execute(
                """INSERT OR IGNORE INTO projection_work(
                       creator_account_id,canonical_revision,work_kind,conversation_id,created_at)
                   VALUES (?,?,'reseed',NULL,?)""",
                (creator_account_id, int(row[0]), now),
            )

    @staticmethod
    def _policy(row: sqlite3.Row) -> ArchivePolicy:
        return ArchivePolicy(
            creator_account_id=str(row["creator_account_id"]),
            enabled=bool(row["vault_enabled"]),
            policy_type=str(row["policy_type"]),  # type: ignore[arg-type]
            finite_horizon_days=(None if row["finite_horizon_days"] is None
                                 else int(row["finite_horizon_days"])),
            revision=int(row["policy_revision"]),
            indefinite_gate_open=row["indefinite_gate_state"] == "open",
        )
