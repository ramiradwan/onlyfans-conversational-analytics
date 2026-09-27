"""Verified append graph work streams a prefix and stages only new payloads."""
import inspect
from unittest.mock import Mock

import pytest

from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message
from tests.test_dominant_append_reuse import dominant_fixture


@pytest.mark.parametrize('state', ['ordinary', 'rebuilt'])
def test_append_does_not_materialize_the_complete_predecessor_graph(tmp_path, monkeypatch, state):
    from app.analytics import conversation_append
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        if state == 'rebuilt':
            f.pipeline.publish_candidate(f.pipeline.build_candidate(ACCOUNT, force=True))
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'streamed-tail', NOW, 2)
            advance(db)
        materialize = Mock(side_effect=AssertionError('complete predecessor graph reconstructed'))
        monkeypatch.setattr(conversation_append, '_previous_graph', materialize)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        materialize.assert_not_called()
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)


def test_staging_does_not_decode_already_stored_payloads(tmp_path, monkeypatch):
    from app.analytics import incremental_graph
    import hashlib
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.stores.database.read() as db:
            known = {row[0] for kind in ('node', 'edge') for row in db.execute(
                f'SELECT content_id FROM graph_{kind}_content')}
        loads, repeated = incremental_graph.json.loads, []
        def observe(data, *args, **kwargs):
            frame = inspect.currentframe().f_back
            if (frame.f_globals.get('__name__') == incremental_graph.__name__
                    and frame.f_code.co_name in ('write_incremental_graph', '_content_parameters')
                    and isinstance(data, str) and hashlib.sha256(data.encode()).hexdigest() in known):
                repeated.append(len(data))
            return loads(data, *args, **kwargs)
        monkeypatch.setattr(incremental_graph.json, 'loads', observe)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'staging-tail', NOW, 2)
            advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        assert not repeated, {'repeated_records': len(repeated), 'repeated_bytes': sum(repeated)}
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)


def test_changed_buckets_reuse_verified_canonical_bytes(tmp_path, monkeypatch):
    from app.analytics import incremental_graph
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        reparse = Mock(wraps=incremental_graph._records_from_chunk)
        monkeypatch.setattr(incremental_graph, '_records_from_chunk', reparse)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'verified-bucket-tail', NOW, 2)
            advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        assert reparse.call_count == 0, reparse.call_count
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)


@pytest.mark.parametrize('fault', ['digest', 'count', 'category', 'bucket', 'account', 'order', 'duplicate'])
def test_verified_bucket_handoff_rejects_inconsistent_bytes_and_membership(fault):
    from types import SimpleNamespace
    import hashlib, json
    from app.analytics.incremental_graph import _records_from_verified_chunk
    account = 'a1:' + 'a' * 64
    rows = [dict(account_ref=account, kind='message', node_id='g1:10' + digit * 62,
                 occurred_at=None, properties={}) for digit in ('0', '1')]
    if fault == 'order':
        rows.reverse()
    elif fault == 'duplicate':
        rows[1] = rows[0]
    chunk = ','.join(json.dumps(row, sort_keys=True, separators=(',', ':')) for row in rows).encode()
    segment = SimpleNamespace(kind='node', bucket='10', count=2,
        categories=(('message', 2),), chunk_digest=hashlib.sha256(chunk).hexdigest())
    if fault == 'digest':
        segment.chunk_digest = '0' * 64
    elif fault == 'count':
        segment.count = 3
    elif fault == 'category':
        segment.categories = (('participant', 2),)
    elif fault == 'bucket':
        segment.bucket = 'ff'
    elif fault == 'account':
        account = 'a1:' + 'b' * 64
    with pytest.raises(ValueError):
        _records_from_verified_chunk(segment, chunk, account, lambda: None)


def test_streamed_unit_digest_equals_complete_selected_graph_after_successive_appends(tmp_path):
    from datetime import timedelta
    from app.analytics.compact_graph import CompactGraph
    from app.analytics.conversation_graph_units import graph_unit_ids
    from app.analytics.opaque_refs import account_ref, conversation_ref
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        for index in range(3):
            with f.repositories.database.transaction() as db:
                insert_message(db, 'chat-0', f'streamed-{index}', NOW - timedelta(seconds=3-index), index)
                advance(db)
            result = f.pipeline.project_account(ACCOUNT)
            with f.stores.projections.open_conversation_fragments(ACCOUNT) as loader:
                unit = loader.previous_graph_unit(conversation_ref(ACCOUNT, 'chat-0'))
            nodes, edges = map(set, graph_unit_ids(unit))
            graph = CompactGraph(account_ref(ACCOUNT))
            graph.add((n for n in result.artifact.nodes if n.node_id in nodes),
                      (e for e in result.artifact.edges if e.edge_id in edges), check=lambda: None)
            assert len(graph.nodes) == len(nodes) and len(graph.edges) == len(edges)
            assert graph.digest(check=lambda: None) == unit.header.graph_digest
            cold_equal(f, result.artifact)
    finally:
        cleanup(f)
