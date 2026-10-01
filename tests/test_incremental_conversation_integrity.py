"""A verified append must not reopen unchanged account payload buckets."""
from unittest.mock import Mock

import pytest

from tests.continuous_analytics_fixture import (
    ACCOUNT, NOW, advance, cleanup, cold_equal, insert_message,
)
from tests.test_dominant_append_reuse import dominant_fixture

pytestmark = [pytest.mark.ci_tier("integration")]


def test_append_reads_only_changed_payload_buckets(tmp_path, monkeypatch):
    from app.analytics import incremental_graph, shared_graph
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        opened, affected = [], set()
        read = shared_graph.verified_segment_chunk
        assemble = incremental_graph.build_incremental_graph
        def observe_read(db, account, proof, kind, bucket):
            opened.append((kind, bucket))
            return read(db, account, proof, kind, bucket)
        def observe_build(**kwargs):
            changed = kwargs['changed_graph']
            for kind, records in [('node', changed.nodes), ('edge', changed.edges)]:
                affected.update((kind, key[3:5]) for key in records)
            for kind in ('node', 'edge'):
                affected.update((kind, key[3:5]) for key in kwargs['timeline_' + kind + 's'])
            return assemble(**kwargs)
        monkeypatch.setattr(shared_graph, 'verified_segment_chunk', observe_read)
        monkeypatch.setattr(incremental_graph, 'build_incremental_graph', observe_build)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'integrity-tail', NOW, 2)
            advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        extra = set(opened) - affected
        assert not extra, {'payload_calls': len(opened), 'unchanged_buckets': len(extra)}
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)


@pytest.mark.parametrize('state', ['rebuilt', 'restarted'])
def test_append_group_reuse_survives_runtime_preparation(tmp_path, monkeypatch, state):
    from app.analytics import shared_graph
    from app.analytics.opaque_refs import conversation_ref
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        if state == 'rebuilt':
            f.pipeline.publish_candidate(f.pipeline.build_candidate(ACCOUNT, force=True))
        else:
            from app.analytics.canonical_source import HistoryAnalyticsSource
            from app.analytics.factory import create_analytics_stores
            from app.analytics.pipeline import AnalyticsPipeline
            f.stores.projections.close_retention_scheduler()
            source = HistoryAnalyticsSource(f.repositories.history)
            stores = create_analytics_stores('sqlite', projections_path=f.stores.database.path,
                activation=f.repositories.projection_activation, canonical_identity_reader=source.read_identity,
                retention_clock=lambda: NOW)
            f.source, f.stores = source, stores
            f.pipeline = AnalyticsPipeline(source, projections=stores.projections,
                graph=stores.graph, enrichment=f.pipeline.enrichment, clock=lambda: NOW)
            assert f.pipeline.prepare_questions(ACCOUNT, 1)
        with f.stores.projections.open_conversation_fragments(ACCOUNT) as load:
            assert load.previous_graph_unit(conversation_ref(ACCOUNT, 'chat-0')).header.checksum_version == 2
        read = Mock(wraps=shared_graph.verified_segment_chunk)
        monkeypatch.setattr(shared_graph, 'verified_segment_chunk', read)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'prepared-integrity-tail', NOW, 2)
            advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        assert read.call_count < 32, read.call_count
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)




def test_append_content_versions_come_from_verified_chunks(tmp_path, monkeypatch):
    """Construction must not reopen graph content through selected-content SQL."""
    from app.analytics import shared_graph
    from app.analytics.conversation_graph_units import graph_unit_ids
    from app.analytics.opaque_refs import conversation_ref
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        ref = conversation_ref(ACCOUNT, 'chat-0')
        with f.stores.projections.open_conversation_fragments(ACCOUNT) as load:
            unit = load.previous_graph_unit(ref)
            nodes, _edges = graph_unit_ids(unit)
            selected = nodes[:32]
            expected = load.graph_content_ids('node', selected)
        active = {'append': False}
        from app.analytics import conversation_append
        original_append = conversation_append.try_append
        original_selected = shared_graph.selected_content_ids
        def observed_append(*args, **kwargs):
            active['append'] = True
            try:
                return original_append(*args, **kwargs)
            finally:
                active['append'] = False
        def observed_selected(*args, **kwargs):
            if active['append']:
                raise AssertionError('selected_content_ids SQL path reopened during append')
            return original_selected(*args, **kwargs)
        monkeypatch.setattr(conversation_append, 'try_append', observed_append)
        monkeypatch.setattr(shared_graph, 'selected_content_ids', observed_selected)
        with f.stores.projections.open_conversation_fragments(ACCOUNT) as load:
            actual = load.append_graph_content_ids('node', selected)
        assert actual == expected
        assert len(actual) == len(selected)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'chunk-derived-tail', NOW, 2)
            advance(db)
        candidate = f.pipeline.build_candidate(ACCOUNT)
        f.pipeline.publish_candidate(candidate)
        cold_equal(f, candidate.artifact())
    finally:
        cleanup(f)


def test_append_chunk_reuse_rechecks_canonical_chunk_digest(tmp_path, monkeypatch):
    """A proof does not authorize construction from changed chunk bytes."""
    from app.analytics import shared_graph
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        before = f.stores.database.active_generation(ACCOUNT).generation_id
        original = shared_graph.verified_segment_chunk
        def corrupted(*args, **kwargs):
            value = original(*args, **kwargs)
            if value is None:
                return None
            segment, encoded = value
            return segment, encoded + b'changed'
        monkeypatch.setattr(shared_graph, 'verified_segment_chunk', corrupted)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'corrupt-proof-tail', NOW, 2)
            advance(db)
        with pytest.raises(ValueError, match='conversation_append_chunk_invalid'):
            f.pipeline.build_candidate(ACCOUNT)
        assert f.stores.database.active_generation(ACCOUNT).generation_id == before
    finally:
        cleanup(f)

def test_group_root_binds_scope_membership_and_content():
    from app.analytics.conversation_integrity import summarize_group, encode_manifest
    from app.analytics.opaque_refs import account_ref, conversation_ref
    account, chat = account_ref(ACCOUNT), conversation_ref(ACCOUNT, 'chat-0')
    key = 'g1:10' + 'a' * 62
    first = summarize_group(account, chat, 'node', '10', {key: 'b' * 64})
    changed = summarize_group(account, chat, 'node', '10', {key: 'c' * 64})
    assert first[3] == changed[3] and first[4] != changed[4]
    root, metadata = encode_manifest(account, chat, [first])
    assert encode_manifest(account, chat, [changed])[0] != root
    assert encode_manifest(account_ref('other'), chat, [first])[0] != root
    assert encode_manifest(account, conversation_ref(ACCOUNT, 'other'), [first])[0] != root
    with pytest.raises(ValueError):
        encode_manifest(account, chat, [first, first])
    assert len(metadata) < 1024


@pytest.mark.parametrize('fault', ['version', 'scope', 'count', 'order', 'digest', 'missing'])
def test_manifest_damage_is_rejected(tmp_path, fault):
    from copy import deepcopy
    from dataclasses import replace
    import json
    from app.analytics.conversation_integrity import decode_manifest
    from app.analytics.opaque_refs import conversation_ref
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        with f.stores.projections.open_conversation_fragments(ACCOUNT) as load:
            unit = load.previous_graph_unit(conversation_ref(ACCOUNT, 'chat-0'))
        value = deepcopy(json.loads(unit.integrity_metadata))
        if fault == 'version': value['version'] = 1
        elif fault == 'scope': value['conversation'] = conversation_ref(ACCOUNT, 'other')
        elif fault == 'count': value['groups'][0][2] += 1
        elif fault == 'order': value['groups'].reverse()
        elif fault == 'digest': value['groups'][0][4] = '0' * 64
        else: value['groups'].pop()
        bad = replace(unit, integrity_metadata=json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
        with pytest.raises(ValueError): decode_manifest(bad)
    finally:
        cleanup(f)


def test_self_consistent_false_summary_cannot_publish(tmp_path, monkeypatch):
    from dataclasses import replace
    import json
    from app.analytics import conversation_integrity as integrity
    from app.analytics.conversation_graph_units import graph_unit_ids, unit_id
    original = integrity.append_unit
    f = dominant_fixture(tmp_path)
    try:
        f.pipeline.project_account(ACCOUNT)
        before = f.stores.database.active_generation(ACCOUNT).generation_id
        def corrupt(*args, **kwargs):
            unit = original(*args, **kwargs)
            nodes, edges = graph_unit_ids(unit)
            value = json.loads(unit.integrity_metadata)
            value['groups'][0][4] = '0' * 64
            digest, metadata = integrity.encode_manifest(unit.header.account_ref,
                unit.header.conversation_ref, value['groups'])
            header = replace(unit.header, graph_digest=digest,
                unit_id=unit_id(digest, nodes, edges, checksum_version=2))
            return replace(unit, header=header, integrity_metadata=metadata)
        monkeypatch.setattr(integrity, 'append_unit', corrupt)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-0', 'invalid-summary-tail', NOW, 2)
            advance(db)
        with pytest.raises(ValueError, match='conversation_integrity_selected_content_changed'):
            f.pipeline.build_candidate(ACCOUNT)
        assert f.stores.database.active_generation(ACCOUNT).generation_id == before
    finally:
        cleanup(f)
