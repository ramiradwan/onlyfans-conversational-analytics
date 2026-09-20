"""Check bounded graph-reference pages without relaxing stored-content checks."""

from dataclasses import replace
import json
import zlib

import pytest

from app.analytics.conversation_graph_sql import graph_records
from app.analytics.conversation_page_sql import load_pages
from app.analytics.conversation_pages import (
    ConversationPage, GRAPH_REFERENCE_ENCODING, PAGE_RECORDS,
    checked_page_sets, page_digest, restore_pages, unpack_page,
)
from app.analytics.compact_graph import CompactArtifact, CompactGraph
from app.analytics.opaque_refs import account_ref, conversation_ref
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cold_equal, insert_message
from tests.test_shared_conversation_pages import fixture


def selected(db):
    row = db.execute('SELECT * FROM conversation_page_sets WHERE conversation_ref=?',
                     (conversation_ref(ACCOUNT, 'chat-0'),)).fetchone()
    return load_pages(db, row['generation_id'], row['creator_account_id'],
                      row['conversation_ref'], row['input_digest'], row['config_digest'])


def test_graph_pages_store_only_ids_and_restore_exact_records(fixture):
    result = fixture.pipeline.project_account(ACCOUNT).artifact
    with fixture.stores.database.read() as db:
        packed = selected(db)
        assert packed.header.encoding == GRAPH_REFERENCE_ENCODING
        for page in packed.pages:
            values = json.loads(unpack_page(page.data))
            if page.kind in ('node', 'edge'):
                assert all(isinstance(key, str) and len(key) == 67 for key in values)
        findings, metrics, graph, entries = restore_pages(packed, lambda: None)
        assert metrics.message_count == len(findings) == 257
        assert entries
        expected = {node.node_id: node for node in result.nodes}
        assert all(expected[node.node_id] == node for node in graph.materialize()[0])
    cold_equal(fixture, result)


@pytest.mark.parametrize('references', [False, True])
def test_encoding_switch_reuses_without_source_or_inference(fixture, references):
    store = fixture.stores.projections
    store.reuse_graph_page_references = not references
    fixture.pipeline.project_account(ACCOUNT)
    before = [analyzer.calls for analyzer in fixture.analyzers]
    store.reuse_graph_page_references = references
    fixture.source.loaded.clear()
    result = fixture.pipeline.rebuild_account(ACCOUNT).artifact
    assert fixture.source.loaded == []
    assert [analyzer.calls for analyzer in fixture.analyzers] == before
    with fixture.stores.database.read() as db:
        assert selected(db).header.encoding == (GRAPH_REFERENCE_ENCODING if references else 'zlib-json.v1')
    cold_equal(fixture, result)


@pytest.mark.parametrize('field', ['account', 'generation', 'identity'])
def test_lookup_is_bound_to_account_generation_and_identity(fixture, field):
    artifact = fixture.pipeline.project_account(ACCOUNT).artifact
    with fixture.stores.database.read() as db:
        packed = selected(db)
        keys = [node.node_id for node in artifact.nodes][:3]
        account, generation = packed.header.account_ref, packed.generation_id
        if field == 'account':
            account = account_ref('unrelated-owner')
        elif field == 'generation':
            generation = 'not-a-generation'
        else:
            keys[0] = 'g1:' + 'f' * 64
        with pytest.raises(ValueError, match='reference_absent'):
            graph_records(db, generation, account, 'node', keys, lambda: None)


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_lookup_preserves_order_and_checks_actual_content_hash(fixture, kind):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        packed = selected(db)
        page = next(page for page in packed.pages if page.kind == kind)
        keys = json.loads(unpack_page(page.data))[:3][::-1]
        values = graph_records(db, packed.generation_id, packed.header.account_ref, kind, keys, lambda: None)
        assert [value[kind + '_id'] for value in values] == keys
        table = 'graph_' + kind + '_content'
        db.execute('DROP TRIGGER ' + table + '_immutable')
        db.execute(f'UPDATE {table} SET occurred_at=? WHERE {kind}_id=?',
                   ('2026-09-01T00:00:00.000000Z', keys[0]))
        with pytest.raises(ValueError, match='graph_content_invalid'):
            graph_records(db, packed.generation_id, packed.header.account_ref, kind, keys, lambda: None)


@pytest.mark.parametrize('kind,keys', [('other', ['x']), ('node', []), ('edge', ['x'] * (PAGE_RECORDS + 1))])
def test_graph_lookup_rejects_unbounded_or_invalid_requests(kind, keys):
    with pytest.raises(ValueError, match='lookup_invalid'):
        graph_records(None, 'unused', account_ref(ACCOUNT), kind, keys, lambda: None)


def test_staging_decodes_each_page_once(fixture, monkeypatch):
    import app.analytics.conversation_pages as pages
    full = fixture.pipeline.project_account(ACCOUNT).artifact
    graph = CompactGraph(full.projection.account_ref)
    graph.add(full.nodes, full.edges, check=lambda: None)
    artifact = CompactArtifact(full.projection, graph)
    original, decoded = pages.unpack_page, []
    def observed(data):
        decoded.append(data)
        return original(data)
    monkeypatch.setattr(pages, 'unpack_page', observed)
    with fixture.stores.database.read() as db:
        packed = selected(db)
        assert list(checked_page_sets(artifact, [packed])) == [packed]
        assert len(decoded) == packed.header.page_count


def test_reference_pages_reduce_bytes_without_raising_the_budget(fixture):
    from app.analytics.conversation_pages import create_pages
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        packed = selected(db)
        findings, metrics, graph, entries = restore_pages(packed, lambda: None)
        raw = create_pages(account=packed.header.account_ref,
            conversation=packed.header.conversation_ref, input_digest=packed.header.input_digest,
            config_digest=packed.header.config_digest, cutoff=packed.header.retention_cutoff,
            findings=findings, metrics=metrics, graph=graph,
            analyzer_entries=(entry.model_dump_json().encode() for entry in entries),
            max_bytes=64 * 1024 * 1024, check=lambda: None)
        assert packed.retained_bytes < raw.retained_bytes * 0.75


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_changed_graph_between_reuse_and_staging_cannot_publish(fixture, monkeypatch, kind):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'new', NOW, 1)
        advance(db)
    stage = fixture.stores.projections.stage_built_artifact
    def changed(artifact, **options):
        with fixture.stores.database.transaction() as db:
            table = 'graph_' + kind + '_content'
            packed = selected(db)
            page = next(page for page in packed.pages if page.kind == kind)
            key = json.loads(unpack_page(page.data))[0]
            db.execute('DROP TRIGGER ' + table + '_immutable')
            db.execute(f'UPDATE {table} SET occurred_at=? WHERE {kind}_id=?',
                       ('2026-09-01T00:00:00.000000Z', key))
        return stage(artifact, **options)
    monkeypatch.setattr(fixture.stores.projections, 'stage_built_artifact', changed)
    with pytest.raises(ValueError, match='graph_content_invalid'):
        fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM projection_generations').fetchone()[0] == 1


def test_cancellation_precedes_reference_queries():
    from app.analytics.errors import ProjectionBuildCancelled
    def cancelled():
        raise ProjectionBuildCancelled()
    with pytest.raises(ProjectionBuildCancelled):
        graph_records(None, 'unused', account_ref(ACCOUNT), 'node', ['g1:' + 'f' * 64], cancelled)


def test_disabling_shared_graph_writes_converts_pages_to_self_contained_records(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    fixture.stores.projections.reuse_graph_content = False
    fixture.source.loaded.clear()
    fixture.pipeline.rebuild_account(ACCOUNT)
    assert fixture.source.loaded == []
    with fixture.stores.database.read() as db:
        assert selected(db).header.encoding == 'zlib-json.v1'
    fixture.source.loaded.clear()
    result = fixture.pipeline.rebuild_account(ACCOUNT).artifact
    assert fixture.source.loaded == []
    cold_equal(fixture, result)


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_reference_digest_rejects_rehashed_graph_content(fixture, kind):
    """A new row hash cannot silently replace the original conversation graph."""
    import hashlib
    from app.analytics.graph_row_encoding import node_bytes, edge_bytes
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        packed = selected(db)
        page = next(page for page in packed.pages if page.kind == kind)
        key = json.loads(unpack_page(page.data))[0]
        table = 'graph_' + kind + '_content'
        row = dict(db.execute(f'SELECT * FROM {table} WHERE {kind}_id=?', (key,)).fetchone())
        old = row['content_id']
        row['occurred_at'] = '2026-09-01T00:00:00.000000Z'
        encode = node_bytes if kind == 'node' else edge_bytes
        _, data = encode(row, packed.header.account_ref)
        new = hashlib.sha256(data).hexdigest()
        db.execute('PRAGMA defer_foreign_keys=ON')
        db.execute('DROP TRIGGER ' + table + '_immutable')
        db.execute('DROP TRIGGER graph_segment_' + kind + 's_immutable')
        db.execute(f'UPDATE {table} SET occurred_at=?,content_id=? WHERE content_id=?',
                   (row['occurred_at'], new, old))
        db.execute(f'UPDATE graph_segment_{kind}s SET content_id=? WHERE content_id=?', (new, old))
        assert graph_records(db, packed.generation_id, packed.header.account_ref, kind, [key], lambda: None)
        with pytest.raises(ValueError, match='graph_digest_invalid'):
            restore_pages(packed, lambda: None)


@pytest.mark.parametrize('fault', ['duplicate', 'wrong_domain', 'not_string', 'missing', 'digest', 'no_digest'])
def test_reference_page_tampering_fails_before_any_generation_is_committed(fixture, monkeypatch, fault):
    stage = fixture.stores.projections.stage_built_artifact
    def tampered(artifact, **options):
        packed = options['conversation_pages'][0]
        pages = list(packed.pages)
        index = next(index for index, page in enumerate(pages) if page.kind == 'node')
        values = json.loads(unpack_page(pages[index].data))
        replacement = {'duplicate': values[1], 'wrong_domain': 'e1:' + 'f' * 64,
                       'not_string': {}, 'missing': 'g1:' + 'f' * 64}
        if fault in replacement:
            values[0] = replacement[fault]
            pages[index] = ConversationPage('node', zlib.compress(json.dumps(values).encode(), 1))
        changes = {'pages_digest': page_digest(pages), 'byte_count': sum(len(page.data) for page in pages)}
        if fault in ('digest', 'no_digest'):
            changes['graph_digest'] = None if fault == 'no_digest' else 'sha256:' + 'f' * 64
        header = packed.header.model_copy(update=changes)
        options['conversation_pages'] = (replace(packed, header=header, pages=tuple(pages)),)
        return stage(artifact, **options)
    monkeypatch.setattr(fixture.stores.projections, 'stage_built_artifact', tampered)
    with pytest.raises(ValueError):
        fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT COUNT(*) FROM projection_generations').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_graph_lookup_selects_one_generation_before_content(fixture, kind):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        packed = selected(db)
        page = next(page for page in packed.pages if page.kind == kind)
        keys = json.loads(unpack_page(page.data))[:3]
        plans = []
        class ObservedConnection:
            def execute(self, statement, parameters):
                plans.extend(row[3] for row in db.execute(
                    'EXPLAIN QUERY PLAN ' + statement, parameters))
                assert len(parameters) <= PAGE_RECORDS * 2 + 3
                return db.execute(statement, parameters)
        values = graph_records(ObservedConnection(), packed.generation_id,
            packed.header.account_ref, kind, keys, lambda: None)
        assert [value[kind + '_id'] for value in values] == keys
        for alias in ('m', 'r', 'c'):
            assert any('SEARCH ' + alias + ' USING PRIMARY KEY' in plan for plan in plans)
            assert not any('SCAN ' + alias + ' ' in plan for plan in plans)


@pytest.mark.parametrize('cancel', [False, True])
def test_conversation_reader_restores_its_page_cache(fixture, monkeypatch, cancel):
    from contextlib import contextmanager
    from app.analytics import conversation_sql
    from app.analytics.database import GENERATION_VERIFICATION_CACHE_KIB
    from app.analytics.errors import ProjectionBuildCancelled
    original = conversation_sql.generation_verification_cache
    observations = []
    @contextmanager
    def observed(db):
        previous = db.execute('PRAGMA cache_size').fetchone()[0]
        try:
            with original(db):
                assert db.execute('PRAGMA cache_size').fetchone()[0] == -GENERATION_VERIFICATION_CACHE_KIB
                yield
        finally:
            assert db.execute('PRAGMA cache_size').fetchone()[0] == previous
            observations.append(previous)
    monkeypatch.setattr(conversation_sql, 'generation_verification_cache', observed)
    def use_reader():
        with conversation_sql.fragment_reader(fixture.stores.projections, ACCOUNT):
            if cancel:
                raise ProjectionBuildCancelled()
    if cancel:
        with pytest.raises(ProjectionBuildCancelled):
            use_reader()
    else:
        use_reader()
    assert len(observations) == 1
