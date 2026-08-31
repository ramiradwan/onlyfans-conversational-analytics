from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.persistence.backup import backup_canonical_database
from app.persistence.database import CanonicalSQLite
from app.persistence.managed_recovery import prune_managed_recovery_files
from app.persistence.migrations import MigrationRunner
from app.persistence.retention import CreatorVaultRetention, RetentionPolicyError
from app.persistence.retention_restore import restore_canonical_with_deletion_barriers

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
    sent_at: datetime = NOW - timedelta(days=2),
    origin: str = "ordinary",
) -> int:
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
               VALUES (?,?,'full',?,'Participant',?,'chat-hash',1,1,'chat-event',0,?,?,?)""",
            (ACCOUNT, chat_id, sender_id, now, now, origin, now),
        )
        return connection.execute(
            """INSERT OR IGNORE INTO account_messages(
                   creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
                   direction,upstream_updated_at,content_hash,winning_stream_epoch,
                   winning_source_seq,winning_event_id,is_deleted,updated_at,
                   lifecycle_origin,lifecycle_started_at)
               VALUES (?,?,?,?,?,?,'inbound',NULL,'message-hash',1,1,'message-event',0,?,?,?)""",
            (ACCOUNT, message_id, chat_id, sender_id, "private text", sent_at.isoformat(),
             now, origin, now),
        ).rowcount


def get_message(db: CanonicalSQLite, message_id: str = "message-1"):
    with db.read() as connection:
        return connection.execute(
            "SELECT * FROM account_messages WHERE creator_account_id=? AND message_id=?",
            (ACCOUNT, message_id),
        ).fetchone()


def test_vault_disabled_by_default(tmp_path: Path) -> None:
    retention = CreatorVaultRetention(database(tmp_path / "canonical.sqlite3"), clock=lambda: NOW)
    policy = retention.policy(ACCOUNT)
    assert policy.enabled is False
    assert policy.policy_type == "disabled"


def test_existing_rows_are_working_purpose_until_explicit_vault_enable(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db)
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    with db.read() as connection:
        before = connection.execute(
            "SELECT working_purpose,vault_purpose FROM archive_membership WHERE creator_account_id=?",
            (ACCOUNT,),
        ).fetchone()
    assert tuple(before) == (1, 0)
    retention.set_policy(ACCOUNT, "finite", finite_horizon_days=365, creator_action_ref="enable")
    with db.read() as connection:
        after = connection.execute(
            "SELECT working_purpose,vault_purpose FROM archive_membership WHERE creator_account_id=?",
            (ACCOUNT,),
        ).fetchone()
    assert tuple(after) == (1, 1)


def test_indefinite_policy_fails_closed_and_can_be_exercised_with_explicit_gate(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    with pytest.raises(RetentionPolicyError, match="gate is closed"):
        CreatorVaultRetention(db, clock=lambda: NOW).set_policy(
            ACCOUNT, "indefinite_until_delete", creator_action_ref="request"
        )
    policy = CreatorVaultRetention(db, indefinite_gate=lambda: True, clock=lambda: NOW).set_policy(
        ACCOUNT, "indefinite_until_delete", creator_action_ref="test-gate"
    )
    assert policy.enabled and policy.indefinite_gate_open


def test_finite_expiry_uses_original_source_time_and_stale_replay_is_blocked(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    source_time = NOW - timedelta(days=45)
    seed_message(db, sent_at=source_time)
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    retention.set_policy(ACCOUNT, "finite", finite_horizon_days=30, creator_action_ref="finite")
    assert get_message(db) is None
    assert retention.enforce(ACCOUNT, now=NOW) == []
    assert seed_message(db, sent_at=source_time) == 0
    assert get_message(db) is None
    assert retention.barriers(ACCOUNT)[0]["provenance"] == "retention_expiry"


def test_shortening_finite_policy_enforces_new_boundary_before_return(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db, message_id="old", chat_id="old-chat", sent_at=NOW - timedelta(days=120))
    seed_message(db, message_id="young", chat_id="young-chat", sent_at=NOW - timedelta(days=10))
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    retention.set_policy(ACCOUNT, "finite", finite_horizon_days=365, creator_action_ref="long")

    retention.set_policy(ACCOUNT, "finite", finite_horizon_days=30, creator_action_ref="short")

    assert get_message(db, "old") is None
    assert get_message(db, "young") is not None
    assert any(row["scope_key"] == "old" and row["provenance"] == "retention_expiry"
               for row in retention.barriers(ACCOUNT))


@pytest.mark.parametrize(
    "stale_path",
    [
        "passive-capture",
        "history-acquisition",
        "durable-outbox-retry",
        "snapshot-repair",
        "reconnect",
        "reinstall",
        "projection-rebuild",
        "migration-recovery",
        "stale-extension",
        "stale-local-runtime",
    ],
)
def test_durable_barrier_blocks_ordinary_reentry(tmp_path: Path, stale_path: str) -> None:
    db = database(tmp_path / f"{stale_path}.sqlite3")
    seed_message(db)
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    retention.delete_message(ACCOUNT, "message-1")
    assert seed_message(db) == 0
    assert get_message(db) is None


def test_selective_and_delete_all_scopes(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db, message_id="m1", chat_id="c1", sender_id="p1")
    seed_message(db, message_id="m2", chat_id="c2", sender_id="p2")
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    retention.delete_conversation(ACCOUNT, "c1")
    assert get_message(db, "m1") is None
    assert get_message(db, "m2") is not None
    retention.delete_all(ACCOUNT)
    assert get_message(db, "m2") is None


def test_barrier_survives_reopen_and_vault_disable_reenable(tmp_path: Path) -> None:
    path = tmp_path / "canonical.sqlite3"
    db = database(path)
    seed_message(db)
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    retention.set_policy(ACCOUNT, "finite", finite_horizon_days=365, creator_action_ref="enable-1")
    retention.delete_message(ACCOUNT, "message-1")
    retention.set_policy(ACCOUNT, "disabled", creator_action_ref="disable")
    retention.set_policy(ACCOUNT, "finite", finite_horizon_days=365, creator_action_ref="enable-2")
    reopened = CanonicalSQLite(path)
    assert seed_message(reopened) == 0
    assert get_message(reopened) is None
    assert CreatorVaultRetention(reopened, clock=lambda: NOW).barriers(ACCOUNT)


def test_unlink_preserves_or_deletes_archive_when_explicitly_requested(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db)
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    policy = retention.set_policy(
        ACCOUNT, "finite", finite_horizon_days=365, creator_action_ref="enable"
    )

    preserved = retention.unlink(ACCOUNT, preserve_archive=True)
    assert preserved.revision == policy.revision
    assert get_message(db) is not None

    deletion_revision = retention.unlink(ACCOUNT, preserve_archive=False)
    assert isinstance(deletion_revision, int)
    assert get_message(db) is None


def test_explicit_creator_import_is_a_new_lifecycle(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db)
    retention = CreatorVaultRetention(db, clock=lambda: NOW)
    retention.set_policy(ACCOUNT, "finite", finite_horizon_days=365, creator_action_ref="enable")
    retention.delete_message(ACCOUNT, "message-1")
    retention.import_message(
        ACCOUNT,
        action_ref="import-1",
        conversation={"chat_id": "chat-1", "platform_user_id": "participant-1"},
        message={
            "message_id": "message-1", "chat_id": "chat-1",
            "sender_platform_user_id": "participant-1", "text": "imported",
            "sent_at": (NOW - timedelta(days=2)).isoformat(), "direction": "inbound",
        },
    )
    assert get_message(db)["lifecycle_origin"] == "creator_import"
    assert seed_message(db) == 0
    assert get_message(db)["text"] == "imported"


def test_delete_queues_projection_reseed(tmp_path: Path) -> None:
    db = database(tmp_path / "canonical.sqlite3")
    seed_message(db)
    CreatorVaultRetention(db, clock=lambda: NOW).delete_message(ACCOUNT, "message-1")
    with db.read() as connection:
        rows = connection.execute(
            "SELECT work_kind FROM projection_work WHERE creator_account_id=?", (ACCOUNT,)
        ).fetchall()
    assert [row[0] for row in rows] == ["reseed"]


def test_restore_reconciles_current_deletion_barrier_before_publication(tmp_path: Path) -> None:
    path = tmp_path / "canonical.sqlite3"
    db = database(path)
    seed_message(db)
    backup = tmp_path / "before-delete.backup"
    backup_canonical_database(db, backup)
    CreatorVaultRetention(db, clock=lambda: NOW).delete_message(ACCOUNT, "message-1")
    restore_canonical_with_deletion_barriers(backup, path, overwrite=True)
    restored = CanonicalSQLite(path)
    assert get_message(restored) is None
    assert CreatorVaultRetention(restored, clock=lambda: NOW).barriers(ACCOUNT)


def test_managed_recovery_helper_prunes_complete_cohort_after_thirty_days(tmp_path: Path) -> None:
    old = NOW - timedelta(days=31)
    recent = NOW - timedelta(days=1)
    for name, created in (("old.backup", old), ("recent.backup", recent)):
        backup = tmp_path / name
        backup.write_text("encrypted", encoding="utf-8")
        backup.with_name(name + ".manifest.json").write_text(
            json.dumps({"created_at": created.isoformat()}), encoding="utf-8"
        )
        backup.with_name(name + ".key.dpapi").write_text("wrapped", encoding="utf-8")
    removed = prune_managed_recovery_files(tmp_path, now=NOW)
    assert {path.name for path in removed} == {
        "old.backup", "old.backup.manifest.json", "old.backup.key.dpapi"
    }
    assert (tmp_path / "recent.backup").exists()
