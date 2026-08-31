"""Truthful Creator Vault export construction and copy-state disclosure."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from app.persistence.database import CanonicalSQLite
from app.persistence.managed_recovery import (
    managed_recovery_created_at,
    managed_recovery_is_current,
)
from app.persistence.retention import utc_now


class CreatorVaultExportError(RuntimeError):
    """Raised when a truthful Creator Vault export cannot be constructed."""


@dataclass(frozen=True, slots=True)
class CreatorVaultExport:
    manifest: dict[str, Any]
    conversations: list[dict[str, Any]]
    messages: list[dict[str, Any]]

    def document(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "conversations": self.conversations,
            "messages": self.messages,
        }


class CreatorVaultExporter:
    """Build a state-derived export without overstating deletion outside Product control."""

    def __init__(
        self,
        database: CanonicalSQLite,
        *,
        managed_recovery_roots: Iterable[str | Path] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database = database
        self._managed_recovery_roots = (
            None
            if managed_recovery_roots is None
            else tuple(Path(root) for root in managed_recovery_roots)
        )
        self._clock = clock

    def build(self, creator_account_id: str) -> CreatorVaultExport:
        if not creator_account_id.strip():
            raise ValueError("creator_account_id is required")
        generated_at = self._aware(self._clock())

        with self.database.read() as connection:
            policy_row = connection.execute(
                """SELECT vault_enabled,policy_type,finite_horizon_days,activated_at,
                          policy_revision,indefinite_gate_state
                   FROM archive_policies WHERE creator_account_id=?""",
                (creator_account_id,),
            ).fetchone()
            policy = self._policy_document(policy_row)

            vault_membership_count = int(connection.execute(
                """SELECT COUNT(*) FROM archive_membership a
                   JOIN account_messages m
                     ON m.creator_account_id=a.creator_account_id
                    AND m.message_id=a.message_id
                   WHERE a.creator_account_id=? AND a.vault_purpose=1 AND m.is_deleted=0""",
                (creator_account_id,),
            ).fetchone()[0])
            if not policy["enabled"] and vault_membership_count:
                raise CreatorVaultExportError(
                    "disabled archive policy has vault-governed records"
                )

            messages = [] if not policy["enabled"] else [
                dict(row)
                for row in connection.execute(
                    """SELECT m.message_id,m.chat_id AS conversation_id,
                              m.sender_platform_user_id,m.text,m.sent_at,m.direction,
                              m.lifecycle_origin,m.lifecycle_started_at,
                              a.source_event_at,a.vault_policy_revision
                       FROM account_messages m
                       JOIN archive_membership a
                         ON a.creator_account_id=m.creator_account_id
                        AND a.message_id=m.message_id
                       WHERE m.creator_account_id=? AND m.is_deleted=0
                         AND a.vault_purpose=1
                       ORDER BY a.source_event_at,m.message_id""",
                    (creator_account_id,),
                )
            ]
            conversations = [] if not messages else [
                dict(row)
                for row in connection.execute(
                    """SELECT c.chat_id AS conversation_id,c.record_kind,
                              c.platform_user_id,c.display_name,c.upstream_updated_at,
                              c.lifecycle_origin,c.lifecycle_started_at
                       FROM account_chats c
                       WHERE c.creator_account_id=? AND c.is_deleted=0
                         AND EXISTS (
                           SELECT 1 FROM account_messages m
                           JOIN archive_membership a
                             ON a.creator_account_id=m.creator_account_id
                            AND a.message_id=m.message_id
                           WHERE m.creator_account_id=c.creator_account_id
                             AND m.chat_id=c.chat_id AND m.is_deleted=0
                             AND a.vault_purpose=1
                         )
                       ORDER BY c.chat_id""",
                    (creator_account_id,),
                )
            ]
            barrier_row = connection.execute(
                """SELECT COUNT(*) AS barrier_count,
                          COALESCE(MAX(deletion_revision),0) AS latest_revision
                   FROM deletion_barriers WHERE creator_account_id=?""",
                (creator_account_id,),
            ).fetchone()
            barrier_count = int(barrier_row["barrier_count"])
            latest_revision = int(barrier_row["latest_revision"])

        payload = {
            "conversations": conversations,
            "messages": messages,
        }
        recovery = self._managed_recovery_state(generated_at)
        manifest = {
            "schema_version": 1,
            "export_type": "creator_vault",
            "creator_account_id": creator_account_id,
            "generated_at": generated_at.isoformat(),
            "vault_policy": policy,
            "content": {
                "included": ["vault_messages", "parent_conversations"],
                "excluded": [
                    "working_only_messages",
                    "analytics_workspace",
                    "operational_state",
                    "deletion_barriers",
                    "managed_recovery_files",
                ],
                "conversation_count": len(conversations),
                "message_count": len(messages),
                "sha256": self._digest(payload),
            },
            "deletion_state": {
                "barrier_count": barrier_count,
                "latest_deletion_revision": latest_revision,
            },
            "copy_domains": {
                "live_vault": {
                    "managed_by_product": True,
                    "record_count": len(messages),
                    "contains_exported_records": bool(messages),
                },
                "managed_recovery": recovery,
                "this_export_after_delivery": {
                    "managed_by_product": False,
                    "observable_by_product": False,
                    "managed_vault_deletion_applies": False,
                },
                "other_external_copies": {
                    "managed_by_product": False,
                    "observable_by_product": False,
                    "managed_vault_deletion_applies": False,
                },
            },
        }
        return CreatorVaultExport(manifest, conversations, messages)

    @staticmethod
    def _policy_document(row: Any) -> dict[str, Any]:
        if row is None:
            return {
                "enabled": False,
                "policy_type": "disabled",
                "finite_horizon_days": None,
                "activated_at": None,
                "policy_revision": 0,
                "indefinite_gate_state": "closed",
            }
        return {
            "enabled": bool(row["vault_enabled"]),
            "policy_type": str(row["policy_type"]),
            "finite_horizon_days": row["finite_horizon_days"],
            "activated_at": row["activated_at"],
            "policy_revision": int(row["policy_revision"]),
            "indefinite_gate_state": str(row["indefinite_gate_state"]),
        }

    def _managed_recovery_state(self, observed_at: datetime) -> dict[str, Any]:
        if self._managed_recovery_roots is None:
            return {
                "managed_by_product": True,
                "inspection_complete": False,
                "recognized_cohort_count": 0,
                "within_retention_cohort_count": 0,
                "expired_cohort_count": 0,
                "unrecognized_file_count": 0,
                "copies_observed": False,
                "copies_may_remain": True,
            }

        recognized = 0
        current = 0
        expired = 0
        unknown = 0
        for root in self._managed_recovery_roots:
            if not root.exists():
                continue
            candidates = [root] if root.is_file() else sorted(root.iterdir())
            for candidate in candidates:
                if not candidate.is_file() or candidate.name.endswith(
                    (".manifest.json", ".key.dpapi")
                ):
                    continue
                created_at = managed_recovery_created_at(candidate)
                if created_at is None:
                    unknown += 1
                    continue
                recognized += 1
                if managed_recovery_is_current(candidate, now=observed_at):
                    current += 1
                else:
                    expired += 1
        return {
            "managed_by_product": True,
            "inspection_complete": unknown == 0,
            "recognized_cohort_count": recognized,
            "within_retention_cohort_count": current,
            "expired_cohort_count": expired,
            "unrecognized_file_count": unknown,
            "copies_observed": recognized > 0,
            "copies_may_remain": recognized > 0 or unknown > 0,
        }

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("export clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()
