"""Check content-key locality without changing graph records or publication guards."""

from contextlib import contextmanager
from dataclasses import replace

import pytest

from app.analytics.compact_graph import CompactGraph
from app.analytics.database import content_write_cache, content_write_cache_target
from app.analytics.errors import ProjectionBuildCancelled
from app.analytics.opaque_refs import account_ref, opaque_ref
from app.analytics.shared_graph import _plans, _records, _content_order, _existing_ids
from app.models.analytics import GraphNode, GraphEdge


def graph():
    account = account_ref('synthetic-content-order')
    nodes = [GraphNode(node_id=opaque_ref('graph_node', str(i)), account_ref=account,
                      kind='message', properties={'character_count': i}) for i in range(300)]
    edges = [GraphEdge(edge_id=opaque_ref('graph_edge', str(i)), account_ref=account,
                      source_id=nodes[i].node_id, target_id=nodes[i+1].node_id,
                      relation='precedes', properties={'scope': 'message'}) for i in range(299)]
    result = CompactGraph(account)
    result.add(nodes, edges, check=lambda: None)
    return result


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_content_order_changes_only_the_physical_write_sequence(kind):
    value = graph()
    plans = list(_plans(value, {}, lambda: None))
    originals = [tuple(p.keys) for p in plans]
    expected = [item for p in plans if p.kind == kind for item in _records(value, p, lambda: None)]
    actual = list(_content_order(value, plans, kind, lambda: None))
    assert sorted(actual) == sorted(expected)
    assert [item[0][1] for item in actual] == sorted(item[0][1] for item in expected)
    assert [tuple(p.keys) for p in plans] == originals
    assert any(a != b for a, b in zip(actual, expected, strict=True))


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_reused_segments_do_not_prepare_any_content(kind):
    value = graph()
    plans = [replace(p, reused=True) for p in _plans(value, {}, lambda: None)]
    assert list(_content_order(value, plans, kind, lambda: None)) == []


def test_cancellation_interrupts_sort_key_preparation():
    value = graph()
    plans = list(_plans(value, {}, lambda: None))
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 20:
            raise ProjectionBuildCancelled()
    with pytest.raises(ProjectionBuildCancelled):
        list(_content_order(value, plans, 'node', check))
    assert calls == 20


@pytest.mark.parametrize('count,expected', [(0,16384), (1,16384), (32768,16384),
    (32769,16385), (100000,50000), (1000000,131072)])
def test_write_cache_target_is_bounded(count, expected):
    assert content_write_cache_target(count) == expected


@pytest.mark.parametrize('value', [-1, True, 3.5, '100'])
def test_invalid_record_counts_are_rejected(value):
    with pytest.raises(ValueError):
        content_write_cache_target(value)


class CacheConnection:
    def __init__(self):
        self.cache_size = -8000
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        if statement == 'PRAGMA cache_size':
            return self
        assert statement.startswith('PRAGMA cache_size=')
        self.cache_size = int(statement.split('=')[1])
        return self

    def fetchone(self):
        return (self.cache_size,)


@pytest.mark.parametrize('fail', [False, True])
def test_cache_target_is_restored_after_success_or_cancellation(fail):
    connection = CacheConnection()
    try:
        with content_write_cache(connection, 1000000):
            assert connection.cache_size == -131072
            if fail:
                raise ProjectionBuildCancelled()
    except ProjectionBuildCancelled:
        assert fail
    assert connection.cache_size == -8000
    assert all(sql.startswith('PRAGMA cache_size') for sql in connection.statements)


def test_existing_key_lookups_are_account_scoped_and_bounded():
    calls = []
    class Lookup:
        def execute(self, statement, parameters):
            calls.append((statement, parameters))
            return [(key,) for key in parameters[1:] if key.endswith('0')]
    keys = [str(index) for index in range(600)]
    found = _existing_ids(Lookup(), 'graph_node_content', 'content_id', 'synthetic-account',
                          keys, lambda: None)
    assert found == {key for key in keys if key.endswith('0')}
    assert [len(parameters) for _, parameters in calls] == [257, 257, 89]
    assert all(parameters[0] == 'synthetic-account' for _, parameters in calls)
    assert all('creator_account_id=?' in sql for sql, _ in calls)


def test_existing_empty_key_set_does_not_query():
    class Forbidden:
        def execute(self, *args):
            raise AssertionError('empty lookup executed SQL')
    assert _existing_ids(Forbidden(), 'graph_node_content', 'content_id', 'account', [], lambda: None) == set()


def test_real_writes_are_ordered_and_skip_existing_payloads(tmp_path, monkeypatch):
    import app.analytics.shared_graph as storage
    import app.analytics.incremental_graph as incremental
    from app.analytics.sqlite_graph_store import SQLiteGraphGenerationWriter
    from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup, insert_message, advance, cold_equal
    fixture = make_fixture(tmp_path, conversations=3, messages=30)
    original_transaction = SQLiteGraphGenerationWriter._owned_transaction
    original_write = storage.write_shared_graph
    original_incremental_write = incremental.write_incremental_graph
    captured = {'node': [], 'edge': []}
    statistics = []
    class RecordingConnection:
        def __init__(self, connection):
            self.connection = connection
        def __getattr__(self, name):
            return getattr(self.connection, name)
        def executemany(self, statement, parameters):
            parameters = list(parameters)
            for kind in captured:
                if statement.startswith('INSERT INTO graph_' + kind + '_content('):
                    captured[kind].extend(row[1] for row in parameters)
            return self.connection.executemany(statement, parameters)
    @contextmanager
    def transaction(writer):
        with original_transaction(writer) as connection:
            yield RecordingConnection(connection)
    def write(*args, **kwargs):
        result = original_write(*args, **kwargs)
        statistics.append(result)
        return result
    def incremental_write(*args, **kwargs):
        result = original_incremental_write(*args, **kwargs)
        statistics.append(result)
        return result
    monkeypatch.setattr(SQLiteGraphGenerationWriter, '_owned_transaction', transaction)
    monkeypatch.setattr(storage, 'write_shared_graph', write)
    monkeypatch.setattr(incremental, 'write_incremental_graph', incremental_write)
    try:
        first = fixture.pipeline.project_account(ACCOUNT).artifact
        assert captured['node'] == sorted(captured['node'])
        assert captured['edge'] == sorted(captured['edge'])
        assert statistics[-1]['node_identities_written'] == len(first.nodes)
        previous = {kind: set(values) for kind, values in captured.items()}
        captured['node'].clear(); captured['edge'].clear()
        with fixture.repositories.database.transaction() as connection:
            insert_message(connection, 'chat-1', 'ordered-write-addition', NOW)
            advance(connection)
        second = fixture.pipeline.project_account(ACCOUNT).artifact
        cold_equal(fixture, second)
        for kind in captured:
            assert captured[kind] == sorted(captured[kind])
            assert not previous[kind].intersection(captured[kind])
            assert statistics[-1][kind + '_content_written'] == len(captured[kind])
        new_ids = {node.node_id for node in second.nodes} - {node.node_id for node in first.nodes}
        assert statistics[-1]['node_identities_written'] == len(new_ids)
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('skip', [False, True])
@pytest.mark.parametrize('mode', ['full', 'digests'])
def test_workload_report_records_the_optional_unchanged_diagnostic(tmp_path, monkeypatch, skip, mode):
    import json
    import sys
    from tools.qualify_continuous_analytics import main

    output = tmp_path / 'workload'
    arguments = ['qualify_continuous_analytics', '--messages', '100',
                 '--query-samples', '2', '--output', str(output), '--verification-mode', mode]
    if skip:
        arguments.append('--skip-unchanged-rebuild')
    monkeypatch.setattr(sys, 'argv', arguments)
    assert main() == 0
    report = json.loads((output / 'report.json').read_text())
    assert report['complete'] and report['clean_rebuild_equal']
    assert report['forced_unchanged_rebuild'] is not skip
    assert report['verification_mode'] == mode
    assert report['reference_representation'].startswith('compact_' if mode == 'digests' else 'full_')
    assert [phase['phase'] for phase in report['phases']] == (
        ['cold', 'one_new_message'] if skip else ['cold', 'unchanged_rebuild', 'one_new_message'])
    assert report['query']['samples'] == 2 and report['query']['failures'] == {}
    for phase in report['phases']:
        counters = phase['process_io_delta']
        if counters is not None:
            assert set(counters) == {'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                                     'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount'}
            assert all(value >= 0 for value in counters.values())
