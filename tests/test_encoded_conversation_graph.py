"""Reuse checked graph bytes without weakening the public graph contract."""

from dataclasses import FrozenInstanceError, replace
import json

import pytest

from app.analytics.compact_graph import CompactGraph
from app.analytics.conversation_graph_sql import encoded_graph_records, graph_records
from app.analytics.conversation_pages import restore_pages
from app.analytics.errors import ProjectionBuildCancelled
from app.analytics.sqlite_graph_store import _node, _edge
from tests.test_graph_row_encoding import ACCOUNT, TARGET, node_row, edge_row
from tests.test_conversation_graph_references import selected
from tests.test_shared_conversation_pages import fixture
from tests.continuous_analytics_fixture import ACCOUNT as OWNER, cold_equal


def forbidden(*args, **kwargs):
    raise AssertionError('unchanged graph records were reconstructed')


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_encoded_lookup_matches_dictionary_contract_and_is_immutable(fixture, kind):
    fixture.pipeline.project_account(OWNER)
    with fixture.stores.database.read() as db:
        packed = selected(db)
        graph = restore_pages(packed, lambda: None)[2]
        keys = list(graph.nodes if kind == 'node' else graph.edges)[:3][::-1]
        keys.append(keys[0])
        args = (db, packed.generation_id, packed.header.account_ref, kind, keys, lambda: None)
        encoded, dictionaries = encoded_graph_records(*args), graph_records(*args)
        assert [record.key for record in encoded] == keys
        assert [json.loads(record.data) for record in encoded] == dictionaries
        assert all(record.account_ref == packed.header.account_ref and record.kind == kind for record in encoded)
        with pytest.raises(FrozenInstanceError):
            encoded[0].data = '{}'


def test_reference_restore_does_not_rebuild_models_or_encode_graph_records(fixture, monkeypatch):
    from app.analytics import conversation_pages as pages
    expected = fixture.pipeline.project_account(OWNER).artifact
    with fixture.stores.database.read() as db:
        packed = selected(db)
        legacy = restore_pages(replace(packed, graph_encoded_records=None), lambda: None)
        with monkeypatch.context() as patch:
            patch.setattr(pages.GraphNode, 'model_validate', forbidden)
            patch.setattr(pages.GraphEdge, 'model_validate', forbidden)
            patch.setattr(CompactGraph, 'add', forbidden)
            direct = restore_pages(packed, lambda: None)
        assert direct[:2] == legacy[:2]
        assert direct[3] == legacy[3]
        assert direct[2].nodes == legacy[2].nodes
        assert direct[2].edges == legacy[2].edges
        assert direct[2].encoded_bytes == legacy[2].encoded_bytes
        assert direct[2].summary(1) == legacy[2].summary(1)
        assert direct[2].digest(check=lambda: None) == packed.header.graph_digest
    cold_equal(fixture, expected)


@pytest.mark.parametrize('fault', ['account', 'endpoint', 'conversation_edge', 'digest'])
def test_encoded_restore_still_checks_scope_endpoints_and_unit_digest(fixture, fault):
    fixture.pipeline.project_account(OWNER)
    with fixture.stores.database.read() as db:
        packed = selected(db)
        reader = packed.graph_encoded_records
        def changed(kind, keys, check):
            rows = reader(kind, keys, check)
            if kind == 'edge':
                updates = {'account_ref': ACCOUNT} if fault == 'account' else (
                    {'source_id': 'g1:' + 'f' * 64} if fault == 'endpoint' else (
                    {'conversation_edge': True} if fault == 'conversation_edge' else {'data': '{}'}))
                rows[0] = replace(rows[0], **updates)
            return rows
        with pytest.raises(ValueError, match='conversation_page_(graph|endpoint)'):
            restore_pages(replace(packed, graph_encoded_records=changed), lambda: None)


def test_encoded_lookup_obeys_cancellation_before_reading():
    def cancelled():
        raise ProjectionBuildCancelled()
    with pytest.raises(ProjectionBuildCancelled):
        encoded_graph_records(None, 'unused', ACCOUNT, 'node', ['g1:' + 'f' * 64], cancelled)


def graphs():
    source = _node(node_row())
    target = _node(dict(node_row(), node_id=TARGET))
    edge = _edge(edge_row())
    left, right, expected = (CompactGraph(ACCOUNT) for _ in range(3))
    left.add([source], [], check=lambda: None)
    right.add([target], [edge], check=lambda: None)
    expected.add([source, target], [edge], check=lambda: None)
    return left, right, expected


def test_merge_reuses_checked_counts_without_decoding_disjoint_rows(monkeypatch):
    import app.analytics.compact_graph as compact
    left, right, expected = graphs()
    with monkeypatch.context() as patch:
        patch.setattr(compact.json, 'loads', forbidden)
        left.merge(right, check=lambda: None)
    assert left.nodes == expected.nodes and left.edges == expected.edges
    assert left.summary(1) == expected.summary(1)
    assert left.encoded_bytes == expected.encoded_bytes
    assert left.digest(check=lambda: None) == expected.digest(check=lambda: None)


@pytest.mark.parametrize('self_merge', [False, True])
def test_merge_counts_shared_identities_once_and_decodes_only_collisions(monkeypatch, self_merge):
    import app.analytics.compact_graph as compact
    left, right, expected = graphs()
    left.merge(right, check=lambda: None)
    original, decoded = json.loads, []
    def observed(data):
        decoded.append(data)
        return original(data)
    with monkeypatch.context() as patch:
        patch.setattr(compact.json, 'loads', observed)
        left.merge(left if self_merge else right, check=lambda: None)
    assert len(decoded) == (3 if self_merge else 2)
    assert left.summary(1) == expected.summary(1)
    assert left.encoded_bytes == expected.encoded_bytes
    assert left.digest(check=lambda: None) == expected.digest(check=lambda: None)


def test_merge_rejects_changed_bytes_for_a_shared_identity():
    left, right, _ = graphs()
    left.merge(right, check=lambda: None)
    key = next(iter(right.nodes))
    right.nodes[key] = '{}'
    with pytest.raises(ValueError, match='graph_record_identity_collision'):
        left.merge(right, check=lambda: None)


def test_even_an_empty_merge_can_be_cancelled():
    def cancelled():
        raise ProjectionBuildCancelled()
    with pytest.raises(ProjectionBuildCancelled):
        CompactGraph(ACCOUNT).merge(CompactGraph(ACCOUNT), check=cancelled)


def test_merged_counts_cannot_bypass_independent_stored_verification(fixture, monkeypatch):
    from app.analytics.sqlite_projection_store import ProjectionValidationError
    original = CompactGraph.merge
    def wrong_counts(self, other, **options):
        original(self, other, **options)
        if self.node_counts['message']:
            self.node_counts['message'] -= 1
            self.node_counts['topic'] += 1
    monkeypatch.setattr(CompactGraph, 'merge', wrong_counts)
    with pytest.raises(ProjectionValidationError):
        fixture.pipeline.build_candidate(OWNER)
    assert fixture.stores.projections.get(OWNER) is None
