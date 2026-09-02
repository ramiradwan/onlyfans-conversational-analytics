from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.analytics.factory import create_analytics_stores
from app.analytics.identity import canonical_identity
from app.analytics.opaque_refs import message_ref
from app.analytics.pipeline import AnalyticsPipeline
from app.persistence.backup import backup_canonical_database, backup_projections_database
from app.persistence.factory import create_canonical_repositories
from app.persistence.retention import CreatorVaultRetention
from app.persistence.retention_restore import restore_backup_pair_with_deletion_barriers

ACCOUNT = "restore-account"
TARGET_PARTICIPANT = "participant-deleted"
SURVIVOR_PARTICIPANT = "participant-survivor"
TARGET_CHAT = "chat-deleted"
SURVIVOR_CHAT = "chat-survivor"
TARGET_MESSAGE = "message-deleted"
SURVIVOR_MESSAGE = "message-survivor"
NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


def _seed_account(path: Path, *, sent_at: datetime = NOW - timedelta(days=2)):
    repositories = create_canonical_repositories("sqlite", canonical_path=path)
    assert repositories.database is not None
    now = NOW.isoformat()
    with repositories.database.transaction() as connection:
        connection.execute("INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,?,?)", (ACCOUNT, 1, now))
        for chat_id, participant_id in ((TARGET_CHAT, TARGET_PARTICIPANT), (SURVIVOR_CHAT, SURVIVOR_PARTICIPANT)):
            connection.execute("""INSERT INTO account_chats(creator_account_id,chat_id,record_kind,platform_user_id,display_name,upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,winning_event_id,is_deleted,updated_at) VALUES (?,?,'full',?,'Participant',?,'chat-hash',1,1,?,0,?)""", (ACCOUNT, chat_id, participant_id, now, f"chat-event:{chat_id}", now))
        for index, (message_id, chat_id, participant_id) in enumerate(((TARGET_MESSAGE, TARGET_CHAT, TARGET_PARTICIPANT), (SURVIVOR_MESSAGE, SURVIVOR_CHAT, SURVIVOR_PARTICIPANT)), start=1):
            connection.execute("""INSERT INTO account_messages(creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,direction,upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,winning_event_id,is_deleted,updated_at) VALUES (?,?,?,?,?,?, 'inbound',NULL,?,1,?,?,0,?)""", (ACCOUNT, message_id, chat_id, participant_id, f"text:{message_id}", sent_at.isoformat(), f"message-hash:{message_id}", index, f"message-event:{message_id}", now))
    return repositories


def _identity_reader(repositories):
    def read(account_id: str):
        if not repositories.ingestion.account_exists(account_id):
            return None
        return canonical_identity(repositories.ingestion.account_read_model(account_id))
    return read


def _analytics(repositories, path: Path):
    # The store measures the retention window against its own clock, so it takes
    # the same frozen time as the pipeline. Left on wall time, a source time this
    # file states as an offset from NOW expires once NOW is far enough past.
    stores = create_analytics_stores("sqlite", projections_path=path, activation=repositories.projection_activation, canonical_identity_reader=_identity_reader(repositories), retention_clock=lambda: NOW)
    pipeline = AnalyticsPipeline(repositories.ingestion, projections=stores.projections, graph=stores.graph, clock=lambda: NOW)
    return stores, pipeline


def _close(stores) -> None:
    close = getattr(stores.projections, "close_retention_scheduler", None)
    if close is not None:
        close()


@pytest.mark.parametrize("delete_scope", ["message", "participant"])
def test_old_projection_cohort_cannot_resurrect_deleted_workspace_data(tmp_path: Path, delete_scope: str) -> None:
    canonical_path = tmp_path / "canonical.sqlite3"
    projections_path = tmp_path / "analytics.sqlite3"
    repositories = _seed_account(canonical_path)
    stores, pipeline = _analytics(repositories, projections_path)
    target_ref = message_ref(ACCOUNT, TARGET_CHAT, TARGET_MESSAGE)
    survivor_ref = message_ref(ACCOUNT, SURVIVOR_CHAT, SURVIVOR_MESSAGE)
    try:
        initial = pipeline.project_account(ACCOUNT).artifact.projection
        assert {item.message_ref for item in initial.message_enrichments} == {target_ref, survivor_ref}
        assert repositories.database is not None and stores.database is not None
        canonical_backup = tmp_path / "before-delete.canonical.backup"
        projections_backup = tmp_path / "before-delete.analytics.backup"
        backup_canonical_database(repositories.database, canonical_backup)
        backup_projections_database(stores.database, projections_backup)
        retention = CreatorVaultRetention(repositories.database, clock=lambda: NOW)
        if delete_scope == "message": retention.delete_message(ACCOUNT, TARGET_MESSAGE)
        else: retention.delete_participant(ACCOUNT, TARGET_PARTICIPANT)
    finally:
        _close(stores)
    _, restored_projection = restore_backup_pair_with_deletion_barriers(canonical_backup, canonical_path, projections_backup=projections_backup, projections_destination=projections_path, overwrite=True, now=NOW)
    assert restored_projection is None and not projections_path.exists()
    restored_repositories = create_canonical_repositories("sqlite", canonical_path=canonical_path)
    with restored_repositories.database.read() as connection:
        assert connection.execute("SELECT 1 FROM account_messages WHERE creator_account_id=? AND message_id=?", (ACCOUNT, TARGET_MESSAGE)).fetchone() is None
        barrier = connection.execute("SELECT scope_kind,scope_key FROM deletion_barriers WHERE creator_account_id=? ORDER BY deletion_revision DESC LIMIT 1", (ACCOUNT,)).fetchone()
        assert barrier is not None and barrier["scope_kind"] == delete_scope
        work = connection.execute("SELECT work_kind FROM projection_work WHERE creator_account_id=? ORDER BY created_at", (ACCOUNT,)).fetchall()
        assert [row["work_kind"] for row in work] == ["reseed"]
    restored_stores, restored_pipeline = _analytics(restored_repositories, projections_path)
    try:
        assert restored_stores.projections.get(ACCOUNT) is None
        restored_pipeline.project_account(ACCOUNT)
        active = restored_stores.projections.get(ACCOUNT)
        assert active is not None
        active_refs = {item.message_ref for item in active.message_enrichments}
        assert target_ref not in active_refs and active_refs == {survivor_ref}
        assert active.creator_metrics.message_count == 1
    finally:
        _close(restored_stores)


def test_projection_restore_rejects_source_time_expired_generation(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    repositories = _seed_account(source_dir / "canonical.sqlite3", sent_at=NOW - timedelta(days=89))
    stores, pipeline = _analytics(repositories, source_dir / "analytics.sqlite3")
    try:
        pipeline.project_account(ACCOUNT)
        assert repositories.database is not None and stores.database is not None
        canonical_backup = tmp_path / "canonical.backup"
        projections_backup = tmp_path / "analytics.backup"
        backup_canonical_database(repositories.database, canonical_backup)
        backup_projections_database(stores.database, projections_backup)
    finally:
        _close(stores)
    restored_canonical = tmp_path / "restored" / "canonical.sqlite3"
    restored_projections = tmp_path / "restored" / "analytics.sqlite3"
    _, projection_manifest = restore_backup_pair_with_deletion_barriers(canonical_backup, restored_canonical, projections_backup=projections_backup, projections_destination=restored_projections, now=NOW + timedelta(days=2))
    assert projection_manifest is None and restored_canonical.exists() and not restored_projections.exists()


def test_projection_restore_keeps_matching_unexpired_generation(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    repositories = _seed_account(source_dir / "canonical.sqlite3")
    stores, pipeline = _analytics(repositories, source_dir / "analytics.sqlite3")
    try:
        expected = pipeline.project_account(ACCOUNT).artifact.projection
        assert repositories.database is not None and stores.database is not None
        canonical_backup = tmp_path / "canonical.backup"
        projections_backup = tmp_path / "analytics.backup"
        backup_canonical_database(repositories.database, canonical_backup)
        expected_projection_manifest = backup_projections_database(stores.database, projections_backup)
    finally:
        _close(stores)
    restored_canonical = tmp_path / "restored" / "canonical.sqlite3"
    restored_projections = tmp_path / "restored" / "analytics.sqlite3"
    _, projection_manifest = restore_backup_pair_with_deletion_barriers(canonical_backup, restored_canonical, projections_backup=projections_backup, projections_destination=restored_projections, now=NOW)
    assert projection_manifest == expected_projection_manifest
    restored_repositories = create_canonical_repositories("sqlite", canonical_path=restored_canonical)
    restored_stores, _ = _analytics(restored_repositories, restored_projections)
    try:
        assert restored_stores.projections.get(ACCOUNT) == expected
    finally:
        _close(restored_stores)
