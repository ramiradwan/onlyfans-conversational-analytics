"""Check encrypted fragment ownership, cleanup, migration, and restart."""

from datetime import timedelta
import shutil
from pathlib import Path

import pytest

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.conversation_reuse import ConversationFragment
from app.analytics.database import ProjectionsDatabase
from app.analytics.factory import create_analytics_stores
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
from app.persistence import sqlite_api
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cleanup, cold_equal, make_fixture


def rows(fixture):
    with fixture.stores.database.read() as db:
        return [dict(row) for row in db.execute('SELECT * FROM conversation_fragments')]


def test_only_completed_generations_supply_fragment_hits(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path)
    try:
        candidate = fixture.pipeline.build_candidate(ACCOUNT)
        records = rows(fixture)
        assert len(records) == 3
        record = records[0]
        args = (ACCOUNT, record['conversation_ref'], record['input_digest'], record['config_digest'])
        assert fixture.stores.projections.load_conversation_fragment(*args) is None
        fixture.pipeline.publish_candidate(candidate)
        assert fixture.stores.projections.load_conversation_fragment(*args) is not None
        monkeypatch.setattr(fixture.repositories.projection_activation, 'get', lambda _: None)
        assert fixture.stores.projections.load_conversation_fragment(*args) is None
    finally:
        cleanup(fixture)


def test_retirement_removes_fragments_and_active_rows_are_immutable(tmp_path):
    fixture = make_fixture(tmp_path)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        old_ids = {r['generation_id'] for r in rows(fixture)}
        with pytest.raises(sqlite_api.IntegrityError):
            with fixture.stores.database.transaction() as db:
                db.execute("UPDATE conversation_fragments SET document_json='{}'")
        with fixture.repositories.database.transaction() as db:
            db.execute("DELETE FROM account_messages WHERE chat_id='chat-1'")
            db.execute("DELETE FROM account_chats WHERE chat_id='chat-1'")
            advance(db)
        result = fixture.pipeline.project_account(ACCOUNT)
        records = rows(fixture)
        assert len(records) == 2
        assert not old_ids.intersection(r['generation_id'] for r in records)
        assert all('Thanks pricing' not in r['document_json'] for r in records)
        cold_equal(fixture, result.artifact)
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('changed', [False, True])
def test_restart_reuses_only_a_still_valid_generation(tmp_path, changed):
    fixture = make_fixture(tmp_path)
    replacement = None
    try:
        fixture.pipeline.project_account(ACCOUNT)
        cleanup(fixture)
        fixture.source.loaded.clear()
        if changed:
            with fixture.repositories.database.transaction() as db:
                db.execute("UPDATE account_messages SET text='synthetic new value' WHERE message_id='m-1-1'")
                advance(db)
        replacement = create_analytics_stores('sqlite', projections_path=tmp_path/'analytics.sqlite3',
            activation=fixture.repositories.projection_activation,
            canonical_identity_reader=fixture.source.read_identity, retention_clock=lambda: NOW)
        pipeline = AnalyticsPipeline(fixture.source, projections=replacement.projections,
            enrichment=fixture.pipeline.enrichment, clock=lambda: NOW)
        result = pipeline.rebuild_account(ACCOUNT)
        assert fixture.source.loaded == (['chat-0', 'chat-1', 'chat-2'] if changed else [])
        assert [a.calls for a in fixture.analyzers] == ([18, 18, 18] if changed else [9, 9, 9])
        cold_equal(fixture, result.artifact)
    finally:
        cleanup(fixture)
        if replacement:
            replacement.projections.close_retention_scheduler()


def test_expiry_removes_optional_fragments_without_extending_source_lifetime(tmp_path):
    fixture = make_fixture(tmp_path)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        assert len(rows(fixture)) == 3
        fixture.clock.now = NOW + timedelta(days=91)
        assert fixture.stores.projections.enforce_retention(ACCOUNT)
        assert rows(fixture) == []
    finally:
        cleanup(fixture)


def test_additive_migration_preserves_the_published_graph(tmp_path):
    fixture = make_fixture(tmp_path)
    try:
        catalog = tmp_path/'catalog'
        catalog.mkdir()
        source = Path(__file__).parents[1]/'app/analytics/sql'
        for path in sorted(source.glob('*.sql'))[:5]:
            shutil.copy2(path, catalog/path.name)
        legacy = ProjectionsDatabase(tmp_path/'legacy.sqlite3', migrations_dir=catalog)
        store = SQLiteAnalyticsProjectionStore(legacy, activation=fixture.repositories.projection_activation,
            canonical_identity_reader=fixture.source.read_identity)
        pipeline = AnalyticsPipeline(fixture.source, projections=store, clock=lambda: NOW,
                                     reuse_conversations=False)
        first = pipeline.project_account(ACCOUNT).artifact
        upgraded = ProjectionsDatabase(tmp_path/'legacy.sqlite3')
        with upgraded.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 6
            assert db.execute('SELECT COUNT(*) FROM conversation_fragments').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM graph_nodes').fetchone()[0] == len(first.nodes)
        current = SQLiteAnalyticsProjectionStore(upgraded, activation=fixture.repositories.projection_activation,
            canonical_identity_reader=fixture.source.read_identity)
        assert current.get_artifact(ACCOUNT) == first
    finally:
        cleanup(fixture)
