"""Check compact graph construction against stored and full artifact contracts."""

from datetime import timedelta
import json

import pytest

from app.analytics.compact_graph import CompactGraph, CompactArtifact
from app.analytics.graph_privacy import graph_content_digest
from app.analytics.graph_projection import RelationshipGraphProjector
from app.analytics.opaque_refs import account_ref
from tests.continuous_analytics_fixture import ACCOUNT, NOW, make_fixture, cleanup, cold_equal, insert_message, advance


@pytest.mark.parametrize("messages", [1, 127, 128, 129, 257])
def test_compact_publication_matches_full_graph_across_batches(tmp_path, messages):
    f = make_fixture(tmp_path, conversations=2, messages=messages)
    try:
        result = f.pipeline.project_account(ACCOUNT)
        cold_equal(f, result.artifact)
        with f.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'new', NOW-timedelta(seconds=1), 0)
            advance(db)
        cold_equal(f, f.pipeline.project_account(ACCOUNT).artifact)
    finally:
        cleanup(f)

def test_compact_records_retain_strings_and_exact_digest(tmp_path):
    f = make_fixture(tmp_path)
    try:
        artifact = f.pipeline.project_account(ACCOUNT).artifact
        graph = CompactGraph(account_ref(ACCOUNT))
        graph.add(artifact.nodes, artifact.edges, check=lambda: None)
        assert all(isinstance(value, str) for value in (*graph.nodes.values(), *graph.edges.values()))
        assert graph.digest(check=lambda: None) == graph_content_digest(artifact.nodes, artifact.edges)
        graph.add(artifact.nodes, artifact.edges, check=lambda: None)
        assert graph.summary(artifact.projection.source_revision) == artifact.projection.graph
        assert graph.materialize() == (artifact.nodes, artifact.edges)
    finally:
        cleanup(f)


def test_compact_conflict_is_not_silently_ignored(tmp_path):
    f = make_fixture(tmp_path)
    try:
        artifact = f.pipeline.project_account(ACCOUNT).artifact
        graph = CompactGraph(account_ref(ACCOUNT))
        graph.add(artifact.nodes, artifact.edges, check=lambda: None)
        changed = artifact.nodes[0].model_copy(update={'occurred_at': NOW})
        with pytest.raises(ValueError, match='graph_record_identity_collision'):
            graph.add([changed], [], check=lambda: None)
        with pytest.raises(ValueError, match='compact_graph_account_invalid'):
            CompactGraph(account_ref('other')).merge(graph, check=lambda: None)
    finally:
        cleanup(f)
def test_mutated_compact_rows_fail_independent_stored_verification(tmp_path, monkeypatch):
    import app.analytics.sqlite_projection_store as stores
    from app.analytics.sqlite_projection_store import ProjectionValidationError
    f = make_fixture(tmp_path)
    original = stores.write_compact_graph
    def changed(writer, graph, **kwargs):
        key = next(key for key, value in graph.nodes.items() if json.loads(value)['kind'] == 'message')
        record = json.loads(graph.nodes[key]); record['properties']['character_count'] = 999
        graph.nodes[key] = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return original(writer, graph, **kwargs)
    monkeypatch.setattr(stores, 'write_compact_graph', changed)
    try:
        with pytest.raises(ProjectionValidationError):
            f.pipeline.build_candidate(ACCOUNT)
        assert f.stores.projections.get(ACCOUNT) is None
    finally:
        cleanup(f)


def test_candidate_releases_compact_graph(tmp_path, monkeypatch):
    import gc, weakref
    f = make_fixture(tmp_path); observed = []
    stage = f.stores.projections.stage_built_artifact
    def capture(artifact, **kwargs):
        assert isinstance(artifact, CompactArtifact)
        observed.append(weakref.ref(artifact.graph))
        return stage(artifact, **kwargs)
    monkeypatch.setattr(f.stores.projections, 'stage_built_artifact', capture)
    try:
        candidate = f.pipeline.build_candidate(ACCOUNT)
        gc.collect()
        assert candidate.reference is not None and candidate.artifact_json == b''
        assert observed and all(item() is None for item in observed)
        f.pipeline.publish_candidate(candidate)
    finally:
        cleanup(f)


@pytest.mark.parametrize('operation', ['digest', 'add', 'merge'])
def test_compact_processing_obeys_cancellation(tmp_path, operation):
    from app.analytics.errors import ProjectionBuildCancelled
    f = make_fixture(tmp_path)
    try:
        artifact = f.pipeline.project_account(ACCOUNT).artifact
        graph = CompactGraph(account_ref(ACCOUNT)); graph.add(artifact.nodes, artifact.edges, check=lambda: None)
        def stop():
            raise ProjectionBuildCancelled()
        with pytest.raises(ProjectionBuildCancelled):
            if operation == 'digest':
                graph.digest(check=stop)
            elif operation == 'merge':
                CompactGraph(account_ref(ACCOUNT)).merge(graph, check=stop)
            else:
                graph.add(artifact.nodes, artifact.edges, check=stop)
    finally:
        cleanup(f)


def test_public_stage_rejects_private_compact_artifacts(tmp_path):
    from app.analytics.sqlite_projection_store import ProjectionValidationError
    f = make_fixture(tmp_path)
    try:
        artifact = f.pipeline.project_account(ACCOUNT).artifact
        graph = CompactGraph(account_ref(ACCOUNT)); graph.add(artifact.nodes, artifact.edges, check=lambda: None)
        with pytest.raises(ProjectionValidationError, match='compact_graph_requires_owned_build'):
            f.stores.projections.stage_artifact(CompactArtifact(artifact.projection, graph),
                creator_account_id=ACCOUNT, canonical_identity=f.source.read_identity(ACCOUNT))
    finally:
        cleanup(f)


def test_compact_writer_observes_cancellation_before_inserts(tmp_path, monkeypatch):
    from app.analytics.errors import ProjectionBuildCancelled
    import app.analytics.sqlite_projection_store as stores
    f = make_fixture(tmp_path)
    write = stores.write_compact_graph
    def cancelled(writer, graph, **kwargs):
        def stop():
            raise ProjectionBuildCancelled()
        return write(writer, graph, check=stop)
    monkeypatch.setattr(stores, 'write_compact_graph', cancelled)
    try:
        with pytest.raises(ProjectionBuildCancelled):
            f.pipeline.build_candidate(ACCOUNT)
        assert f.stores.projections.get(ACCOUNT) is None
    finally:
        cleanup(f)


def test_message_order_edges_cross_batch_boundaries(tmp_path):
    from app.analytics.opaque_refs import message_ref
    from app.analytics.graph_projection import stable_node_id
    from app.models.analytics import GraphNodeKind
    f = make_fixture(tmp_path, conversations=1, messages=257)
    try:
        artifact = f.pipeline.project_account(ACCOUNT).artifact
        edges = [edge for edge in artifact.edges if edge.relation.value == 'precedes'
                 and edge.properties.get('scope') == 'message']
        assert len(edges) == 256
        by_source = {edge.source_id: edge for edge in edges}
        for index in (126, 127, 128, 254, 255):
            source = stable_node_id(account_ref(ACCOUNT), GraphNodeKind.MESSAGE,
                message_ref(ACCOUNT, 'chat-0', f'm-0-{index}'))
            target = stable_node_id(account_ref(ACCOUNT), GraphNodeKind.MESSAGE,
                message_ref(ACCOUNT, 'chat-0', f'm-0-{index+1}'))
            assert by_source[source].target_id == target
            assert by_source[source].sequence == index
            assert by_source[source].properties['interval_seconds'] == 60.0
    finally:
        cleanup(f)
