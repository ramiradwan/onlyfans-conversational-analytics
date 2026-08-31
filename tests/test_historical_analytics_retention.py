from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.analytics.database import ProjectionsDatabase
from app.analytics.historical_derivation import HistoricalDerivationProvenance
from app.analytics.identity import canonical_identity
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.retention_store import (
    MAX_RETENTION_TIMER_SECONDS,
    RetentionBoundSQLiteAnalyticsProjectionStore,
)
from app.persistence.factory import create_canonical_repositories


ACCOUNT = "account-a"
NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _seed_account(path: Path, messages: list[tuple[str, datetime]]):
    repositories = create_canonical_repositories("sqlite", canonical_path=path)
    with repositories.database.transaction() as connection:
        connection.execute(
            "INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,?,?)",
            (ACCOUNT, 1, NOW.isoformat()),
        )
        connection.execute(
            """INSERT INTO account_chats(
                   creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                   upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                   winning_event_id,is_deleted,updated_at)
               VALUES (?, 'chat-1', 'full', 'participant-1', 'Participant', ?,
                       'chat-hash', 1, 1, 'chat-event', 0, ?)""",
            (ACCOUNT, NOW.isoformat(), NOW.isoformat()),
        )
        for index, (message_id, sent_at) in enumerate(messages, start=1):
            _insert_message(connection, message_id, sent_at, index=index)
    return repositories


def _insert_message(connection, message_id: str, sent_at: datetime, *, index: int) -> None:
    connection.execute(
        """INSERT INTO account_messages(
               creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,
               direction,upstream_updated_at,content_hash,winning_stream_epoch,
               winning_source_seq,winning_event_id,is_deleted,updated_at)
           VALUES (?,?, 'chat-1','participant-1',?,?, 'inbound',NULL,?,1,?,?,0,?)""",
        (
            ACCOUNT,
            message_id,
            f"text-{message_id}",
            sent_at.isoformat(),
            f"hash-{message_id}",
            index,
            f"event-{message_id}",
            NOW.isoformat(),
        ),
    )


def _identity_reader(repositories):
    def read(account_id: str):
        if not repositories.ingestion.account_exists(account_id):
            return None
        return canonical_identity(repositories.ingestion.account_read_model(account_id))

    return read


def _store(repositories, path: Path, clock: MutableClock):
    database = ProjectionsDatabase(path)
    store = RetentionBoundSQLiteAnalyticsProjectionStore(
        database,
        activation=repositories.projection_activation,
        canonical_identity_reader=_identity_reader(repositories),
        clock=clock,
    )
    pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=store,
        graph=store.graph,
        clock=clock,
    )
    return database, store, pipeline


def _active_provenance(
    store: RetentionBoundSQLiteAnalyticsProjectionStore,
) -> HistoricalDerivationProvenance:
    provenance = store.historical_derivation(ACCOUNT)
    assert provenance is not None
    return provenance


def _assert_canonical_witness_unchanged(database: ProjectionsDatabase) -> None:
    generation = database.active_generation(ACCOUNT)
    assert generation is not None
    assert json.loads(generation.canonical_high_water_json) == {
        "content_digest": generation.canonical_content_digest,
        "view_revision": generation.canonical_revision,
    }


def test_historical_ingestion_of_old_message_does_not_start_new_retention_period(
    tmp_path: Path,
) -> None:
    recent_at = NOW - timedelta(days=3)
    historical_at = NOW - timedelta(days=120)
    repositories = _seed_account(
        tmp_path / "canonical.sqlite3",
        [("recent", recent_at)],
    )
    clock = MutableClock(NOW)
    database, store, pipeline = _store(
        repositories,
        tmp_path / "analytics.sqlite3",
        clock,
    )
    try:
        assert pipeline.project_account(ACCOUNT).changed
        timer = store._retention_timers.get(ACCOUNT)
        assert timer is not None
        assert timer.interval <= MAX_RETENTION_TIMER_SECONDS

        with repositories.database.transaction() as connection:
            _insert_message(connection, "historical", historical_at, index=2)
            connection.execute(
                "UPDATE account_heads SET canonical_revision=2,updated_at=? WHERE creator_account_id=?",
                (NOW.isoformat(), ACCOUNT),
            )

        refreshed = pipeline.project_account(ACCOUNT)
        projection = refreshed.artifact.projection
        assert refreshed.changed
        assert [item.sent_at for item in projection.message_enrichments] == [recent_at]
        assert projection.creator_metrics.message_count == 1

        provenance = _active_provenance(store)
        active = database.active_generation(ACCOUNT)
        assert active is not None
        assert provenance.derived_at == active.started_at.astimezone(timezone.utc)
        assert provenance.source_message_count == 1
        assert provenance.source_time_start == recent_at
        assert provenance.source_time_end == recent_at
        assert provenance.retention_due_at == recent_at + timedelta(days=90)
        assert provenance.retention_due_at != provenance.derived_at + timedelta(days=90)
        _assert_canonical_witness_unchanged(database)
    finally:
        store.close_retention_scheduler()


def test_reanalysis_does_not_refresh_source_time_retention_due_at(tmp_path: Path) -> None:
    source_at = NOW - timedelta(days=89)
    repositories = _seed_account(
        tmp_path / "canonical.sqlite3",
        [("source", source_at)],
    )
    clock = MutableClock(NOW)
    database, store, pipeline = _store(
        repositories,
        tmp_path / "analytics.sqlite3",
        clock,
    )
    try:
        assert pipeline.project_account(ACCOUNT).changed
        first_generation = database.active_generation(ACCOUNT)
        first_provenance = _active_provenance(store)
        due_at = source_at + timedelta(days=90)
        assert first_generation is not None
        assert first_provenance.derived_at == first_generation.started_at.astimezone(
            timezone.utc
        )
        assert first_provenance.retention_due_at == due_at

        clock.value = NOW + timedelta(hours=12)
        rebuilt = pipeline.rebuild_account(ACCOUNT)
        second_generation = database.active_generation(ACCOUNT)
        second_provenance = _active_provenance(store)

        assert rebuilt.changed
        assert second_generation is not None
        assert second_generation.generation_id != first_generation.generation_id
        assert second_provenance.derived_at == second_generation.started_at.astimezone(
            timezone.utc
        )
        assert second_provenance.derived_at >= first_provenance.derived_at
        assert second_provenance.source_time_start == source_at
        assert second_provenance.retention_due_at == due_at
        assert store.retention_due_at(ACCOUNT) == due_at
        assert second_provenance.retention_due_at != (
            second_provenance.derived_at + timedelta(days=90)
        )
        _assert_canonical_witness_unchanged(database)
    finally:
        store.close_retention_scheduler()


def test_regeneration_after_projection_loss_uses_only_authorized_source_material(
    tmp_path: Path,
) -> None:
    historical_at = NOW - timedelta(days=140)
    recent_at = NOW - timedelta(days=2)
    repositories = _seed_account(
        tmp_path / "canonical.sqlite3",
        [("historical", historical_at), ("recent", recent_at)],
    )
    clock = MutableClock(NOW)
    first_database, first_store, first_pipeline = _store(
        repositories,
        tmp_path / "analytics-before-loss.sqlite3",
        clock,
    )
    try:
        first = first_pipeline.project_account(ACCOUNT).artifact.projection
        assert [item.sent_at for item in first.message_enrichments] == [recent_at]
        assert _active_provenance(first_store).source_time_start == recent_at
        _assert_canonical_witness_unchanged(first_database)
    finally:
        first_store.close_retention_scheduler()

    regenerated_database, regenerated_store, regenerated_pipeline = _store(
        repositories,
        tmp_path / "analytics-after-loss.sqlite3",
        clock,
    )
    try:
        regenerated = regenerated_pipeline.rebuild_account(ACCOUNT).artifact.projection
        assert [item.sent_at for item in regenerated.message_enrichments] == [recent_at]
        assert regenerated.creator_metrics.message_count == 1
        assert regenerated.graph.node_counts_by_kind.get("message") == 1

        provenance = _active_provenance(regenerated_store)
        assert provenance.source_message_count == 1
        assert provenance.source_time_start == recent_at
        assert provenance.source_time_end == recent_at
        assert provenance.retention_due_at == recent_at + timedelta(days=90)
        _assert_canonical_witness_unchanged(regenerated_database)
    finally:
        regenerated_store.close_retention_scheduler()
