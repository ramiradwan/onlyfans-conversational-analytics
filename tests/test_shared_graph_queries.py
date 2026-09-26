"""Keep graph verification scoped to the selected generation's segments."""

import pytest

from app.analytics.shared_graph import ordered_rows, verify_segment_links
from tests.test_shared_graph import fixture
from tests.continuous_analytics_fixture import ACCOUNT


class CapturedQueries:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def execute(self, statement, parameters=()):
        self.calls.append((statement, parameters))
        return self.connection.execute(statement, parameters)


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_ordered_reads_start_from_generation_manifest(fixture, kind):
    artifact = fixture.pipeline.project_account(ACCOUNT).artifact
    with fixture.stores.database.read() as db:
        generation = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
        capture = CapturedQueries(db)
        rows = list(ordered_rows(capture, generation, artifact.projection.account_ref, kind))
        statement, parameters = capture.calls[-1]
        details = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + statement, parameters)]
        expected = artifact.nodes if kind == 'node' else artifact.edges
        key = kind + '_id'
        assert [row[key] for row in rows] == [getattr(row, key) for row in expected]
        assert all(row['generation_id'] == generation for row in rows)
        assert details[0].startswith('SEARCH m ')
        assert 'generation_id=?' in details[0]
        assert any('SEARCH p ' in detail and 'segment_id=?' in detail for detail in details)
        assert any('SEARCH r ' in detail and 'page_id=?' in detail for detail in details)
        assert not any('TEMP B-TREE' in detail for detail in details)


def test_endpoint_checks_select_generation_before_edge_content(fixture):
    artifact = fixture.pipeline.project_account(ACCOUNT).artifact
    with fixture.stores.database.read() as db:
        generation = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
        capture = CapturedQueries(db)
        verify_segment_links(capture, generation, artifact.projection.account_ref)
        statement, parameters = next((sql, params) for sql, params in capture.calls
            if 'graph_segment_edges r' in sql)
        details = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + statement, parameters)]
        assert any('SEARCH m ' in detail and 'generation_id=?' in detail for detail in details)
        assert any('SEARCH m ' in detail and 'segment_id=?' in detail for detail in details)
        assert any('SEARCH r ' in detail and 'page_id=?' in detail for detail in details)
        assert any('SEARCH nm ' in detail and 'generation_id=?' in detail for detail in details)
        assert not any('CORRELATED' in detail for detail in details)


@pytest.mark.parametrize('endpoint', ['source_id', 'target_id'])
def test_endpoint_must_belong_to_selected_generation(fixture, monkeypatch, endpoint):
    import app.analytics.shared_graph as storage
    from app.analytics.graph_store import GraphReferentialIntegrityError
    from tests.continuous_analytics_fixture import advance

    artifact = fixture.pipeline.project_account(ACCOUNT).artifact
    fixture.stores.projections._conversation_graph_proofs.clear()
    absent = getattr(artifact.edges[0], endpoint)
    records = storage._records
    def omit_selected_node(graph, plan, check):
        for value, membership in records(graph, plan, check):
            if plan.kind != 'node' or membership[2] != absent:
                yield value, membership
    monkeypatch.setattr(storage, '_predecessor_segments', lambda *args: {})
    monkeypatch.setattr(storage, '_records', omit_selected_node)
    with fixture.repositories.database.transaction() as db:
        advance(db)
    with pytest.raises(GraphReferentialIntegrityError, match='graph_endpoint_absent'):
        fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.read() as db:
        assert db.execute('SELECT 1 FROM graph_node_identities WHERE node_id=?', (absent,)).fetchone()


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_content_insert_guard_uses_bucket_lookup(fixture, kind):
    from app.analytics.opaque_refs import account_ref

    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (f'graph_{kind}_content_building',)).fetchone()[0]
        statement = trigger.split('WHEN NOT EXISTS(', 1)[1].split(')\nBEGIN', 1)[0]
        statement = statement.replace('NEW.creator_account_id', '?')
        statement = statement.replace(f'substr(NEW.{kind}_id,4,2)', '?')
        details = [row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + statement,
            (account_ref(ACCOUNT), 'ab'))]
        assert details[0].startswith('SEARCH s ')
        assert 'graph_segments_building_lookup' in details[0]
        assert all(part in details[0] for part in ('creator_account_id=?', 'kind=?', 'bucket=?', 'sealed=?'))
