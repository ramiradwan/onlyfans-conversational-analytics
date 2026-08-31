from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.persistence.archive_export import CreatorVaultExporter
from app.persistence.backup import backup_canonical_database
from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner
from app.persistence.retention import CreatorVaultRetention

ACCOUNT = "creator-account"
NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


def database(path: Path) -> CanonicalSQLite:
    value = CanonicalSQLite(path)
    MigrationRunner(value).run()
    return value


def seed_message(
    db: CanonicalSQLite,
    *,
    message_id: str = "message-1",
    chat_id: str = "chat-1",
    sender_id: str = "participant-1",
    text: str = "private text",
) -> None:
    now = NOW.isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO account_heads(creator_account_id,updated_at) VALUES (?,?)",
            (ACCOUNT, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO account_chats(
                   creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                   upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                   winning_event_id,is_deleted,updated_at,lifecycle_origin,lifecycle_started_at)
               VALUES (?,?,'full',?,'Participant',?,'chat-hash',1,1,'chat-event',0,?,'ordinary',?)""",
            (ACCOUNT, chat_id, sender_id, now, now, now),
        )
        connection.execute(
            """INSERT OR IGNORE INTO account_messages(
                   creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                   direction,upstream_updated_at,content_hash,winning_stream_epoch,
                   winning_source_seq,winning_event_id,is_deleted,updated_at,
                   lifecycle_origin,lifecycle_started_at)
               VALUES (?,?,?,?,?,?,'inbound',NULL,'message-hash',1,1,'message-event',0,?,
                       'ordinary',?)""",
            (
                ACCOUNT,
                message_id,
                chat_id,
                sender_id,
                text,
                (NOW - timedelta(days=2)).isoformat(),
                now,
                now,
            ),
        )


def content_digest(export) -> str:
    payload = {
        "conversations": export.conversations,
        "messages": export.messages,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_active_vault_export_manifest_is_bound_to_exported_archive_state(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db)
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    retention.set_policy(
        ACCOUNT,
        "finite",
        finite_horizon_days=365,
        creator_action_ref="enable-vault",
    )

    export = CreatorVaultExporter(
        db,
        managed_recovery_roots=[],
        clock=lambda: NOW,
    ).build(ACCOUNT)
    manifest = export.manifest

    assert manifest["export_type"] == "creator_vault"
    assert manifest["vault_policy"] == {
        "enabled": True,
        "policy_type": "finite",
        "finite_horizon_days": 365,
        "activated_at": NOW.isoformat(),
        "policy_revision": 1,
        "indefinite_gate_state": "closed",
    }
    assert manifest["content"]["included"] == [
        "vault_messages",
        "parent_conversations",
    ]
    assert manifest["content"]["message_count"] == 1
    assert manifest["content"]["conversation_count"] == 1
    assert manifest["content"]["sha256"] == content_digest(export)
    assert export.messages[0]["text"] == "private text"
    assert "content_hash" not in export.messages[0]
    assert "winning_event_id" not in export.messages[0]
    assert manifest["copy_domains"]["live_vault"] == {
        "managed_by_product": True,
        "record_count": 1,
        "contains_exported_records": True,
    }
    assert manifest["copy_domains"]["managed_recovery"]["copies_may_remain"] is False


def test_export_after_creator_deletion_reports_empty_live_vault_without_external_delete_claim(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db)
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    retention.set_policy(
        ACCOUNT,
        "finite",
        finite_horizon_days=365,
        creator_action_ref="enable-vault",
    )
    exporter = CreatorVaultExporter(
        db,
        managed_recovery_roots=[],
        clock=lambda: NOW,
    )

    before = exporter.build(ACCOUNT)
    assert before.manifest["content"]["message_count"] == 1

    retention.delete_all(ACCOUNT)
    after = exporter.build(ACCOUNT)

    assert after.messages == []
    assert after.conversations == []
    assert after.manifest["content"]["message_count"] == 0
    assert after.manifest["copy_domains"]["live_vault"]["record_count"] == 0
    assert after.manifest["deletion_state"]["barrier_count"] == 1
    assert after.manifest["deletion_state"]["latest_deletion_revision"] == 1
    assert after.manifest["copy_domains"]["this_export_after_delivery"] == {
        "managed_by_product": False,
        "observable_by_product": False,
        "managed_vault_deletion_applies": False,
    }
    assert after.manifest["copy_domains"]["other_external_copies"] == {
        "managed_by_product": False,
        "observable_by_product": False,
        "managed_vault_deletion_applies": False,
    }


def test_export_disclosure_tracks_actual_product_managed_recovery_state(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db)
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    retention.set_policy(
        ACCOUNT,
        "finite",
        finite_horizon_days=365,
        creator_action_ref="enable-vault",
    )

    recovery = tmp_path / "managed-recovery"
    recovery.mkdir()
    backup = recovery / "canonical.backup"
    backup_canonical_database(db, backup)
    retention.delete_all(ACCOUNT)

    observed_at = datetime.now(timezone.utc)
    exporter = CreatorVaultExporter(
        db,
        managed_recovery_roots=[recovery],
        clock=lambda: observed_at,
    )
    with_copy = exporter.build(ACCOUNT)
    recovery_state = with_copy.manifest["copy_domains"]["managed_recovery"]

    assert with_copy.manifest["content"]["message_count"] == 0
    assert recovery_state["inspection_complete"] is True
    assert recovery_state["recognized_cohort_count"] == 1
    assert recovery_state["within_retention_cohort_count"] == 1
    assert recovery_state["copies_observed"] is True
    assert recovery_state["copies_may_remain"] is True

    for member in recovery.iterdir():
        member.unlink()
    without_copy = exporter.build(ACCOUNT)
    updated_state = without_copy.manifest["copy_domains"]["managed_recovery"]
    assert updated_state["recognized_cohort_count"] == 0
    assert updated_state["copies_observed"] is False
    assert updated_state["copies_may_remain"] is False


def test_export_without_recovery_inventory_fails_closed_on_copy_disclosure(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db)
    CreatorVaultRetention(db, clock=lambda: NOW).set_policy(
        ACCOUNT,
        "finite",
        finite_horizon_days=365,
        creator_action_ref="enable-vault",
    )

    recovery_state = CreatorVaultExporter(db, clock=lambda: NOW).build(ACCOUNT).manifest[
        "copy_domains"
    ]["managed_recovery"]

    assert recovery_state["inspection_complete"] is False
    assert recovery_state["copies_observed"] is False
    assert recovery_state["copies_may_remain"] is True
