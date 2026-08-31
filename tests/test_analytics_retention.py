from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.analytics.database import ProjectionsDatabase
from app.analytics.identity import canonical_identity
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.projection_store import CLEAR_PIPELINE_REVISION
from app.analytics.historical_derivation import PARTICIPANT_ANALYTICS_MAX_DAYS
from app.analytics.retention_store import RetentionBoundSQLiteAnalyticsProjectionStore
from app.models.analytics import AnalyticsProjection, WindowScope
from app.persistence.factory import create_canonical_repositories


ACCOUNT = "account-a"
NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def seed_messages(path: Path, messages: list[tuple[str, datetime]]):
    repositories = create_canonical_repositories("sqlite", canonical_path=path)
    with repositories.database.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO account_heads(creator_account_id,updated_at) VALUES (?,?)",
            (ACCOUNT, NOW.isoformat()),
        )
        connection.execute(
            """INSERT OR IGNORE INTO account_chats(
                   creator_account_id,chat_id,record_kind,platform_user_id,display_name,
                   upstream_updated_at,content_hash,winning_stream_epoch,winning_source_seq,
                   winning_event_id,is_deleted,updated_at)
               VALUES (?, 'chat-1', 'full', 'participant-1', 'Participant', ?,
                       'chat-hash', 1, 1, 'chat-event', 0, ?)""",
            (ACCOUNT, NOW.isoformat(), NOW.isoformat()),
        )
        for index, (message_id, sent_at) in enumerate(messages, start=1):
            connection.execute(
                """INSERT OR IGNORE INTO account_messages(
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
        connection.execute(
            "UPDATE account_heads SET canonical_revision=1,updated_at=? WHERE creator_account_id=?",
            (NOW.isoformat(), ACCOUNT),
        )
    return repositories


def identity_reader(repositories):
    def read(account_id: str):
        if not repositories.ingestion.account_exists(account_id):
            return None
        return canonical_identity(repositories.ingestion.account_read_model(account_id))

    return read


def test_pipeline_excludes_messages_at_or_beyond_ninety_days(tmp_path: Path) -> None:
    repositories = seed_messages(
        tmp_path / "canonical.sqlite3",
        [
            ("expired", NOW - timedelta(days=90)),
            ("recent", NOW - timedelta(days=1)),
        ],
    )
    result = AnalyticsPipeline(repositories.ingestion, clock=lambda: NOW).rebuild_account(
        ACCOUNT
    )

    projection = result.artifact.projection
    assert PARTICIPANT_ANALYTICS_MAX_DAYS == 90
    assert projection.window.scope is WindowScope.ALL_TIME
    assert [item.sent_at for item in projection.message_enrichments] == [
        NOW - timedelta(days=1)
    ]
    assert projection.creator_metrics.message_count == 1
    assert projection.graph.node_counts_by_kind.get("message") == 1


def test_sqlite_generation_expires_from_original_source_time_without_clock_reset(
    tmp_path: Path,
) -> None:
    source_at = NOW - timedelta(days=89)
    repositories = seed_messages(
        tmp_path / "canonical.sqlite3",
        [("message-1", source_at)],
    )
    clock = MutableClock(NOW)
    database = ProjectionsDatabase(tmp_path / "analytics-projections.sqlite3")
    store = RetentionBoundSQLiteAnalyticsProjectionStore(
        database,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        clock=clock,
    )
    pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=store,
        graph=store.graph,
        clock=clock,
    )

    first = pipeline.project_account(ACCOUNT)
    active = database.active_generation(ACCOUNT)
    assert first.changed and active is not None
    first_generation = active.generation_id
    due_at = source_at + timedelta(days=90)
    assert store.retention_due_at(ACCOUNT) == due_at
    with database.read() as connection:
        row = connection.execute(
            "SELECT document_json FROM analytics_projections WHERE generation_id=?",
            (first_generation,),
        ).fetchone()
    persisted = AnalyticsProjection.model_validate_json(row["document_json"])
    assert persisted.window.scope is WindowScope.ALL_TIME
    assert min(item.sent_at for item in persisted.message_enrichments) == source_at

    clock.value = due_at
    assert store.get(ACCOUNT) is None
    with database.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM projection_generations WHERE generation_id=?",
            (first_generation,),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM analytics_projections WHERE generation_id=?",
            (first_generation,),
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM graph_nodes WHERE generation_id=?",
            (first_generation,),
        ).fetchone() is None

    rebuilt = pipeline.rebuild_account(ACCOUNT)
    assert rebuilt.artifact.projection.message_enrichments == []
    assert rebuilt.artifact.projection.conversation_metrics == []
    assert rebuilt.artifact.projection.creator_metrics.message_count == 0
    assert rebuilt.artifact.projection.graph.node_counts_by_kind.get("message", 0) == 0
    assert store.retention_due_at(ACCOUNT) is None


def test_mixed_age_generation_rebuild_keeps_recent_source_after_oldest_expires(
    tmp_path: Path,
) -> None:
    old_source_at = NOW - timedelta(days=89)
    recent_source_at = NOW - timedelta(days=1)
    repositories = seed_messages(
        tmp_path / "canonical.sqlite3",
        [("old", old_source_at), ("recent", recent_source_at)],
    )
    clock = MutableClock(NOW)
    database = ProjectionsDatabase(tmp_path / "analytics-projections.sqlite3")
    store = RetentionBoundSQLiteAnalyticsProjectionStore(
        database,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        clock=clock,
    )
    pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=store,
        graph=store.graph,
        clock=clock,
    )

    initial = pipeline.project_account(ACCOUNT)
    assert [item.sent_at for item in initial.artifact.projection.message_enrichments] == [
        old_source_at,
        recent_source_at,
    ]
    assert store.retention_due_at(ACCOUNT) == old_source_at + timedelta(days=90)

    clock.value = old_source_at + timedelta(days=90)
    assert store.get(ACCOUNT) is None

    rebuilt = pipeline.rebuild_account(ACCOUNT)
    projection = rebuilt.artifact.projection
    assert [item.sent_at for item in projection.message_enrichments] == [recent_source_at]
    assert projection.creator_metrics.message_count == 1
    assert projection.graph.node_counts_by_kind.get("message") == 1
    assert store.retention_due_at(ACCOUNT) == recent_source_at + timedelta(days=90)


def test_analytics_workspace_persists_no_raw_message_text(tmp_path: Path) -> None:
    marker_id = "private-marker-7f6d39b6"
    raw_text = f"text-{marker_id}"
    repositories = seed_messages(
        tmp_path / "canonical.sqlite3",
        [(marker_id, NOW - timedelta(days=1))],
    )
    database = ProjectionsDatabase(tmp_path / "analytics-projections.sqlite3")
    store = RetentionBoundSQLiteAnalyticsProjectionStore(
        database,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        clock=lambda: NOW,
    )
    pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=store,
        graph=store.graph,
        clock=lambda: NOW,
    )
    pipeline.project_account(ACCOUNT)

    audited_tables = (
        "projection_generations",
        "analytics_projections",
        "graph_nodes",
        "graph_edges",
        "graph_partition_stats",
        "graph_algorithm_metrics",
    )
    with database.read() as connection:
        for table in audited_tables:
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            for row in rows:
                for value in row:
                    if isinstance(value, str):
                        assert raw_text not in value, (table, raw_text)


def test_pre_bounded_generation_fails_closed_and_is_scrubbed_on_access(
    tmp_path: Path,
) -> None:
    repositories = seed_messages(
        tmp_path / "canonical.sqlite3",
        [("message-1", NOW - timedelta(days=1))],
    )
    database = ProjectionsDatabase(tmp_path / "analytics-projections.sqlite3")
    from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore

    legacy = SQLiteAnalyticsProjectionStore(
        database,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
    )
    legacy_pipeline = AnalyticsPipeline(
        repositories.ingestion,
        projections=legacy,
        graph=legacy.graph,
        clock=lambda: NOW,
    )
    # Simulate a pre-bounded artifact identity while retaining otherwise valid rows.
    legacy_pipeline.pipeline_revision = legacy_pipeline.pipeline_revision.replace(
        "analytics.pipeline.v3+", "analytics.pipeline.v2+"
    )
    first = legacy_pipeline.project_account(ACCOUNT)
    legacy_generation = database.active_generation(ACCOUNT)
    assert first.changed and legacy_generation is not None

    bounded = RetentionBoundSQLiteAnalyticsProjectionStore(
        database,
        activation=repositories.projection_activation,
        canonical_identity_reader=identity_reader(repositories),
        clock=lambda: NOW,
        reconcile=False,
    )
    cleared = bounded.get(ACCOUNT)
    assert cleared is not None
    assert cleared.pipeline_revision == CLEAR_PIPELINE_REVISION
    assert cleared.message_enrichments == []
    assert cleared.conversation_metrics == []
    assert cleared.creator_metrics.participant_count == 0
    assert cleared.creator_metrics.message_count == 0
    assert cleared.graph.node_count == 0
    assert cleared.graph.edge_count == 0
    with database.read() as connection:
        assert connection.execute(
            "SELECT 1 FROM projection_generations WHERE generation_id=?",
            (legacy_generation.generation_id,),
        ).fetchone() is None
