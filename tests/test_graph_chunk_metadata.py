"""Cover exact-generation chunk metadata reads and unchanged payload checks."""

from dataclasses import replace
from pathlib import Path
import shutil

import pytest

from app.analytics import shared_graph
from app.analytics.opaque_refs import account_ref
from app.analytics.validation_receipt import content_stamp
from sqlcipher3.dbapi2 import SQLITE_DENY, SQLITE_OK, SQLITE_READ
from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, insert_message, make_fixture,
)
from tests.test_shared_graph import fixture


def active_proof(fixture):
    with fixture.stores.database.read() as db:
        row = db.execute(
            "SELECT * FROM projection_generations WHERE status='active'"
        ).fetchone()
        proof = fixture.stores.projections._trusted_graph_segment_proof(db, row)
    assert proof is not None and proof.segments
    return proof


class ObservedConnection:
    def __init__(self, connection):
        self.connection, self.metadata = connection, []

    def execute(self, sql, parameters=()):
        if 'graph_segment_chunks' in sql and 'record_count' in sql:
            self.metadata.append((sql, parameters))
        return self.connection.execute(sql, parameters)


def test_metadata_query_is_covered_and_does_not_select_payload(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    proof = active_proof(fixture)
    with fixture.stores.database.read() as db:
        def authorize(action, table, column, *unused):
            if (action == SQLITE_READ and table == 'graph_segment_chunks'
                    and column == 'canonical_bytes'):
                return SQLITE_DENY
            return SQLITE_OK
        db.set_authorizer(authorize)
        observed = ObservedConnection(db)
        assert shared_graph.verified_segment_chunks_complete(
            observed, account_ref(ACCOUNT), proof
        )
        db.set_authorizer(None)
        assert len(observed.metadata) == 1
        sql, parameters = observed.metadata[0]
        assert parameters == (proof.generation_id, account_ref(ACCOUNT))
        plan = ' '.join(row[3] for row in db.execute(
            'EXPLAIN QUERY PLAN ' + sql, parameters
        ))
        assert 'COVERING INDEX graph_segment_chunk_headers' in plan


def test_metadata_reads_only_the_selected_generation(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'metadata-new-message', NOW)
        advance(db)
    fixture.pipeline.project_account(ACCOUNT)
    proof = active_proof(fixture)
    with fixture.stores.database.read() as db:
        observed = ObservedConnection(db)
        assert shared_graph.verified_segment_chunks_complete(
            observed, account_ref(ACCOUNT), proof
        )
        sql, parameters = observed.metadata[0]
        selected = {row['segment_id'] for row in db.execute(sql, parameters)}
        all_ids = {row[0] for row in db.execute(
            'SELECT segment_id FROM graph_segment_chunks WHERE creator_account_id=?',
            (account_ref(ACCOUNT),)
        )}
        assert selected == {item.segment_id for item in proof.segments}
        assert all_ids > selected
        assert not shared_graph.verified_segment_chunks_complete(
            db, account_ref('another-account'), proof
        )
        assert not shared_graph.verified_segment_chunks_complete(
            db, account_ref(ACCOUNT), replace(proof, generation_id='absent')
        )


@pytest.mark.parametrize('field,value', [
    ('kind', 'other'), ('count', -1), ('chunk_digest', None),
    ('chunk_digest', '0' * 64), ('segment_id', 'absent'),
])
def test_metadata_rejects_a_mismatched_proof(fixture, field, value):
    fixture.pipeline.project_account(ACCOUNT)
    proof = active_proof(fixture)
    changed = replace(proof.segments[0], **{field: value})
    invalid = replace(proof, segments=(changed, *proof.segments[1:]))
    with fixture.stores.database.read() as db:
        assert not shared_graph.verified_segment_chunks_complete(
            db, account_ref(ACCOUNT), invalid
        )
        assert not shared_graph.verified_segment_chunks_complete(
            db, account_ref(ACCOUNT), replace(proof, segments=())
        )


def test_opening_a_chunk_still_rejects_changed_payload(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    proof = active_proof(fixture)
    item = proof.segments[0]
    with fixture.stores.database.read() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DROP TRIGGER graph_segment_chunks_immutable')
        try:
            db.execute(
                'UPDATE graph_segment_chunks SET canonical_bytes=? '
                'WHERE creator_account_id=? AND segment_id=?',
                (b'changed payload', account_ref(ACCOUNT), item.segment_id),
            )
            assert shared_graph.verified_segment_chunks_complete(
                db, account_ref(ACCOUNT), proof
            )
            assert shared_graph.verified_segment_chunk(
                db, account_ref(ACCOUNT), proof, item.kind, item.bucket
            ) is None
        finally:
            db.rollback()


def test_dropping_the_index_invalidates_the_schema_bound_proof(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        row = db.execute(
            "SELECT * FROM projection_generations WHERE status='active'"
        ).fetchone()
        trusted = fixture.stores.projections._trusted_graph_segment_proof
        assert trusted(db, row) is not None
        db.execute('BEGIN IMMEDIATE')
        db.execute('DROP INDEX graph_segment_chunk_headers')
        assert trusted(db, row) is None
        db.rollback()
        assert trusted(db, row) is not None


def test_metadata_index_upgrade_preserves_the_published_graph(tmp_path):
    from app.analytics.database import ProjectionsDatabase
    from app.analytics.pipeline import AnalyticsPipeline
    from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore

    f = make_fixture(tmp_path/'canonical', backend='memory')
    catalog = tmp_path/'catalog'
    catalog.mkdir()
    source = Path(__file__).parents[1]/'app/analytics/sql'
    for migration in sorted(source.glob('*.sql'))[:17]:
        shutil.copy2(migration, catalog/migration.name)
    legacy = ProjectionsDatabase(tmp_path/'legacy.sqlite3', migrations_dir=catalog)
    options = dict(activation=f.repositories.projection_activation,
                   canonical_identity_reader=f.source.read_identity)
    store = SQLiteAnalyticsProjectionStore(legacy, **options)
    pipeline = AnalyticsPipeline(f.source, projections=store,
                                 enrichment=f.pipeline.enrichment, clock=lambda: NOW)
    try:
        expected = pipeline.project_account(ACCOUNT).artifact
        with legacy.read() as db:
            chunks = [tuple(row) for row in db.execute(
                'SELECT * FROM graph_segment_chunks ORDER BY segment_id'
            )]
            assert chunks
        upgraded = ProjectionsDatabase(legacy.path)
        assert upgraded.migration_runner.last_backup_path is not None
        with upgraded.read() as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 19
            assert content_stamp(db) is not None
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
            assert [tuple(row) for row in db.execute(
                'SELECT * FROM graph_segment_chunks ORDER BY segment_id'
            )] == chunks
            assert db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='graph_segment_chunk_headers'"
            ).fetchone()
        with upgraded.open_detached(
            upgraded.migration_runner.last_backup_path, read_only=True
        ) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 17
            assert [tuple(row) for row in db.execute(
                'SELECT * FROM graph_segment_chunks ORDER BY segment_id'
            )] == chunks
        reopened = SQLiteAnalyticsProjectionStore(upgraded, **options)
        assert reopened.get_artifact(ACCOUNT) == expected
    finally:
        cleanup(f)
