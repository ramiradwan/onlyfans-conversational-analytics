"""Root-scoped reads preserve current content, bounds and graph results."""

from datetime import timedelta

import pytest

from app.analytics import sqlite_graph_store as storage
from app.analytics.graph_store import GraphDeadlineExceeded
from app.models.analytics import GraphTraversalBounds
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, insert_message
from tests.test_shared_graph import fixture


def active_reader(fixture):
    with fixture.stores.database.read() as db:
        generation, account = db.execute(
            "SELECT generation_id,creator_account_id FROM projection_generations "
            "WHERE status='active'").fetchone()
    reader = storage.SQLiteGraphReader(fixture.stores.database,
        active_generation_resolver=lambda key, **kw: generation if key == account else None)
    return reader, generation, account


def bounds(account, **updates):
    values = dict(account_ref=account, start_time=NOW-timedelta(days=90), end_time=NOW,
                  max_hops=1, max_results=16, max_visited=32, max_queue=32,
                  max_edges_examined=64, wall_clock_ms=1000, include_timeless=True)
    return GraphTraversalBounds(**dict(values, **updates))


@pytest.mark.parametrize('direction', ['incoming', 'outgoing', 'both'])
@pytest.mark.parametrize('hops', [1, 2])
def test_bounded_results_match_independent_memory_graph(fixture, direction, hops):
    artifact = fixture.pipeline.project_account(ACCOUNT).artifact
    reader, _, account = active_reader(fixture)
    memory = reader._memory_snapshot(account, artifact.nodes, artifact.edges, 0)
    root = next(node.node_id for node in artifact.nodes if node.kind.value == 'message')
    limit = bounds(account, max_hops=hops, max_results=len(artifact.edges)+1,
                   max_edges_examined=len(artifact.edges)+1,
                   max_visited=len(artifact.nodes)+1, max_queue=len(artifact.nodes)+1)
    assert reader.degree(account, root, bounds=limit, direction=direction) == memory.degree(
        account, root, bounds=limit, direction=direction)
    assert reader.neighborhood(account, root, bounds=limit, direction=direction) == memory.neighborhood(
        account, root, bounds=limit, direction=direction)


@pytest.mark.parametrize('direction', ['incoming', 'outgoing', 'both'])
def test_prefix_selects_exact_current_content_without_duplicates(fixture, direction):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'indexed-reader-new-message', NOW, 2)
        advance(db)
    artifact = fixture.pipeline.project_account(ACCOUNT).artifact
    _, generation, account = active_reader(fixture)
    roots = {node.node_id for node in artifact.nodes if node.kind.value == 'message'}
    with fixture.stores.database.read() as db:
        prefix, args = storage._selected_read_prefix(db, generation, account, roots, direction)
        rows = db.execute(prefix + 'SELECT * FROM graph_edges ORDER BY edge_id', args).fetchall()
        expected = [edge for edge in artifact.edges if (
            direction != 'incoming' and edge.source_id in roots
            or direction != 'outgoing' and edge.target_id in roots)]
        assert [storage._edge(row) for row in rows] == expected
        assert len({row['edge_id'] for row in rows}) == len(rows)
        plan = [row[3] for row in db.execute(
            'EXPLAIN QUERY PLAN ' + prefix + 'SELECT * FROM graph_edges', args)]
        assert any('graph_edge_content_by_' in line for line in plan)
        assert not any('SCAN c' in line for line in plan)
        assert any('page_id=?' in line for line in plan)
        node_prefix, node_args = storage._selected_read_prefix(db, generation, account)
        assert [storage._node(row) for row in db.execute(
            node_prefix + 'SELECT * FROM graph_nodes ORDER BY node_id', node_args)] == artifact.nodes
        for wrong_generation, wrong_account in [('absent', account), (generation, 'a1:'+'f'*64)]:
            prefix, args = storage._selected_read_prefix(db, wrong_generation, wrong_account, roots, direction)
            assert not prefix and not args


def test_selected_reads_keep_time_kind_and_result_limits(fixture, monkeypatch):
    artifact = fixture.pipeline.project_account(ACCOUNT).artifact
    reader, _, account = active_reader(fixture)
    memory = reader._memory_snapshot(account, artifact.nodes, artifact.edges, 0)
    root = next(node.node_id for node in artifact.nodes if node.kind.value == 'message')
    for options in ({'max_results': 1}, {'max_edges_examined': 1}, {'include_timeless': False}):
        limit = bounds(account, **options)
        actual_degree = reader.degree(account, root, bounds=limit)
        actual_neighborhood = reader.neighborhood(account, root, bounds=limit)
        with monkeypatch.context() as original:
            original.setattr(storage, '_selected_read_prefix', lambda *a, **kw: ('', ()))
            assert actual_degree == reader.degree(account, root, bounds=limit)
            assert actual_neighborhood == reader.neighborhood(account, root, bounds=limit)
