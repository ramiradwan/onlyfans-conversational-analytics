"""Verify compact publication handles and independently streamed graph checks."""

from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import weakref

import pytest

from app.analytics.errors import CanonicalRevisionChanged, ProjectionBuildCancelled
from app.analytics.graph_verification import verify_graph_rows
from app.analytics.projection_encoding import (
    ENRICHMENT_UNIT_PIPELINE_REVISION, projection_document, projection_digest,
)
from app.analytics.sqlite_projection_store import recompute_generation, ProjectionValidationError
from app.models.analytics import RebuildArtifact
from tests.continuous_analytics_fixture import ACCOUNT, cleanup, make_fixture, cold_equal


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path)
    yield value
    cleanup(value)


def test_sqlite_publication_does_not_serialize_or_decode_an_artifact(fixture, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("full artifact transfer")
    monkeypatch.setattr(RebuildArtifact, "model_dump_json", forbidden)
    monkeypatch.setattr(RebuildArtifact, "model_validate_json", forbidden)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    assert candidate.reference is not None and candidate.artifact_json == b""
    assert len(repr(candidate)) < 4096
    run = fixture.pipeline.publish_candidate(candidate)
    assert run.changed and run._artifact is None
    cold_equal(fixture, run.artifact)


@pytest.mark.parametrize("field,value", [
    ("generation_id", "missing"), ("source_revision", 99), ("projection_generation", 99),
    ("account_ref", "a1:" + "0" * 64), ("canonical_content_digest", "sha256:" + "0" * 64),
    ("pipeline_revision", "changed"), ("pipeline_config_digest", "sha256:" + "0" * 64),
    ("pipeline_identity_digest", "sha256:" + "0" * 64),
    ("projection_digest", "sha256:" + "0" * 64), ("graph_digest", "sha256:" + "0" * 64),
    ("publication_epoch", "missing"), ("retention_due_at", None),
])
def test_forged_generation_reference_cannot_publish(fixture, field, value):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    forged = replace(candidate, reference=replace(candidate.reference, **{field: value}))
    with pytest.raises(CanonicalRevisionChanged):
        fixture.pipeline.publish_candidate(forged)
    assert fixture.stores.projections.get(ACCOUNT) is None


@pytest.mark.parametrize("field,value", [
    ("creator_account_id", "another-owner"), ("source_revision", 99),
    ("canonical_content_digest", "sha256:" + "0" * 64),
    ("staged_generation_id", "missing"), ("publication_epoch", "missing"),
])
def test_outer_candidate_cannot_disagree_with_its_reference(fixture, field, value):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    with pytest.raises(CanonicalRevisionChanged):
        fixture.pipeline.publish_candidate(replace(candidate, **{field: value}))
    assert fixture.stores.projections.get(ACCOUNT) is None


def test_reference_cannot_resolve_changed_or_expired_sources(fixture):
    run = fixture.pipeline.publish_candidate(fixture.pipeline.build_candidate(ACCOUNT))
    assert run.artifact.projection.source_revision == 1
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='Changed source' WHERE message_id='m-1-1'")
    with pytest.raises(CanonicalRevisionChanged):
        _ = run.artifact


def test_reference_expires_without_retaining_message_objects(fixture):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    run = fixture.pipeline.publish_candidate(candidate)
    fixture.clock.now = candidate.reference.retention_due_at
    with pytest.raises(CanonicalRevisionChanged):
        _ = run.artifact
    assert run._artifact is None


def test_discarded_reference_cannot_publish_or_load(fixture):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    fixture.pipeline.discard_candidate(candidate)
    with pytest.raises(CanonicalRevisionChanged):
        fixture.pipeline.publish_candidate(candidate)
    with pytest.raises(CanonicalRevisionChanged):
        candidate.artifact()

def test_streamed_graph_checks_hold_at_most_two_decoded_rows(fixture, monkeypatch):
    from app.analytics import sqlite_graph_store as graph
    run = fixture.pipeline.project_account(ACCOUNT)
    expected = run.artifact
    live, peak = [], [0]
    for name in ("_node", "_edge"):
        original = getattr(graph, name)
        def tracked(row, original=original):
            item = original(row)
            live.append(weakref.ref(item))
            peak[0] = max(peak[0], sum(ref() is not None for ref in live))
            return item
        monkeypatch.setattr(graph, name, tracked)
    generation = fixture.stores.database.active_generation(ACCOUNT).generation_id
    with fixture.stores.database.read() as db:
        verified = verify_graph_rows(db, generation, expected.projection.account_ref)
    assert verified.digest == expected.projection.graph_digest
    assert verified.nodes == verified.edges == []
    assert sum(verified.node_counts.values()) == len(expected.nodes)
    assert sum(verified.edge_counts.values()) == len(expected.edges)
    assert peak[0] <= 2 and not any(ref() is not None for ref in live)


def test_stored_verification_materializes_graph_only_when_requested(fixture):
    expected = fixture.pipeline.project_account(ACCOUNT).artifact
    generation = fixture.stores.database.active_generation(ACCOUNT).generation_id
    with fixture.stores.database.read() as db:
        values = recompute_generation(db, generation)
        full = recompute_generation(db, generation, materialize_graph=True)
    assert values["nodes"] == values["edges"] == []
    assert full["nodes"] == expected.nodes and full["edges"] == expected.edges
    assert values["graph_digest"] == full["graph_digest"] == expected.projection.graph_digest


def test_matching_stored_digest_does_not_hide_corrupted_graph_rows(fixture):
    fixture.pipeline.compact_graph = False
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    with fixture.stores.database.transaction() as db:
        db.execute("DROP TRIGGER graph_node_building_update")
        db.execute("""UPDATE graph_nodes SET properties_json=json_set(properties_json,'$.character_count',999)
            WHERE generation_id=? AND kind='message'""", (candidate.staged_generation_id,))
    with pytest.raises(ProjectionValidationError):
        fixture.pipeline.publish_candidate(candidate)
    assert fixture.stores.projections.get(ACCOUNT) is None


def test_streamed_graph_checks_observe_cancellation(fixture):
    expected = fixture.pipeline.project_account(ACCOUNT).artifact
    generation = fixture.stores.database.active_generation(ACCOUNT).generation_id
    calls = [0]
    def stop():
        calls[0] += 1
        if calls[0] == 7:
            raise ProjectionBuildCancelled()
    with fixture.stores.database.read() as db, pytest.raises(ProjectionBuildCancelled):
        verify_graph_rows(db, generation, expected.projection.account_ref, check=stop)
    assert calls[0] == 7


@pytest.mark.parametrize("empty", [False, True])
def test_streamed_projection_encoding_is_byte_identical(fixture, empty):
    from app.analytics.projection_store import empty_projection
    projection = fixture.pipeline.project_account(ACCOUNT).artifact.projection
    if empty:
        projection = empty_projection(projection)
    expected = json.dumps(projection.model_dump(mode="json"), ensure_ascii=False,
                          separators=(",", ":"), sort_keys=True)
    if ENRICHMENT_UNIT_PIPELINE_REVISION in projection.pipeline_revision:
        from tests.test_projection_verification import independent_projection_digest
        expected_digest = independent_projection_digest(
            projection.model_dump(mode="json")
        )
    else:
        expected_digest = "sha256:" + hashlib.sha256(json.dumps(
            projection.model_dump(mode="json", exclude={"projection_digest"}),
            ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    assert projection_document(projection) == expected
    assert projection_digest(projection) == expected_digest


def test_projection_digest_never_dumps_the_complete_arrays(fixture, monkeypatch):
    from app.models.analytics import AnalyticsProjection
    projection = fixture.pipeline.project_account(ACCOUNT).artifact.projection
    original = AnalyticsProjection.model_dump
    def bounded_dump(self, *args, **kwargs):
        assert {"message_enrichments", "conversation_metrics"} <= kwargs["exclude"]
        return original(self, *args, **kwargs)
    monkeypatch.setattr(AnalyticsProjection, "model_dump", bounded_dump)
    assert projection_digest(projection) == projection.projection_digest


def test_direct_projection_call_still_returns_an_eager_snapshot(fixture):
    result = fixture.pipeline.project_account(ACCOUNT)
    assert result._artifact is not None
    snapshot = result.artifact
    with fixture.repositories.database.transaction() as db:
        db.execute("UPDATE account_messages SET text='New source'")
    assert result.artifact is snapshot

@pytest.mark.parametrize("receipts", [False, True])
def test_activation_requires_full_verification_or_an_unchanged_receipt(fixture, monkeypatch, receipts):
    fixture.stores.projections.reuse_validation_receipts = receipts
    from unittest.mock import Mock
    from app.analytics import sqlite_projection_store as storage
    verify = Mock(wraps=storage.recompute_generation)
    monkeypatch.setattr(storage, "recompute_generation", verify)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    assert verify.call_count == 1
    fixture.pipeline.publish_candidate(candidate)
    assert verify.call_count == (1 if receipts else 2)


def test_corruption_after_witness_completion_cannot_become_visible(fixture):
    fixture.pipeline.compact_graph = False
    def corrupt(stage, generation):
        if stage == "canonical_completed":
            with fixture.stores.database.transaction() as db:
                db.execute("DROP TRIGGER graph_node_building_update")
                db.execute("""UPDATE graph_nodes SET properties_json=json_set(properties_json,'$.character_count',999)
                    WHERE generation_id=? AND kind='message'""", (generation,))
    fixture.stores.projections.crash_hook = corrupt
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    with pytest.raises(ProjectionValidationError):
        fixture.pipeline.publish_candidate(candidate)
    witness = fixture.repositories.projection_activation.get(candidate.staged_generation_id)
    assert witness.state == "cancelled"
    assert fixture.stores.projections.get(ACCOUNT) is None
    assert fixture.stores.database.generation(candidate.staged_generation_id).status == "retired"

def test_stored_candidate_releases_the_original_graph(fixture, monkeypatch):
    import gc
    fixture.pipeline.compact_graph = False
    stage = fixture.stores.projections.stage_artifact
    references = []
    def observe(artifact, **kwargs):
        references.extend(weakref.ref(node) for node in artifact.nodes)
        references.extend(weakref.ref(edge) for edge in artifact.edges)
        return stage(artifact, **kwargs)
    monkeypatch.setattr(fixture.stores.projections, "stage_artifact", observe)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    gc.collect()
    assert candidate.reference is not None and references
    assert not any(reference() is not None for reference in references)


def test_invalid_fragment_rolls_back_the_entire_stage(fixture):
    raw = fixture.source.account_read_model(ACCOUNT)
    artifact = fixture.pipeline._build(ACCOUNT, raw, projection_generation=1)
    with pytest.raises(ValueError):
        fixture.stores.projections.stage_artifact(artifact, creator_account_id=ACCOUNT,
            canonical_identity=fixture.source.read_identity(ACCOUNT), conversation_fragments=(b"{}",))
    assert fixture.stores.database.generations(ACCOUNT) == []
    with fixture.stores.database.read() as db:
        for table in ("analytics_projections", "projection_query_metadata", "graph_nodes", "graph_edges"):
            assert db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0

def test_capacity_comparison_uses_a_stored_reference(tmp_path, monkeypatch):
    """Run the diagnostic against its isolated synthetic stores."""
    from app.analytics.sqlite_projection_store import SQLiteAnalyticsProjectionStore
    from tools.qualify_continuous_analytics import main
    def unexpected_read(*args, **kwargs):
        raise AssertionError("stored artifact was materialized during digest comparison")
    monkeypatch.setattr(SQLiteAnalyticsProjectionStore, "read_generation_artifact", unexpected_read)
    output = tmp_path / "measurement"
    monkeypatch.setattr("sys.argv", ["qualify_continuous_analytics", "--messages", "100",
        "--query-samples", "1", "--verification-mode", "digests", "--output", str(output)])
    assert main() == 0
    report = json.loads((output / "report.json").read_text())
    assert report["verification_mode"] == "digests"
    assert report["complete"] and report["clean_rebuild_equal"]
    assert report["artifact_materialization_seconds"] is None
    assert report["publication_peak_bytes"] > 0

def test_deferred_result_requires_its_completed_witness(fixture):
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    result = fixture.pipeline.publish_candidate(candidate)
    witness = fixture.repositories.projection_activation.get(candidate.staged_generation_id)
    fixture.repositories.projection_activation.reconcile_completed(witness)
    with pytest.raises(CanonicalRevisionChanged):
        _ = result.artifact


def test_direct_snapshot_survives_a_source_change_during_optional_refresh(fixture, monkeypatch):
    refresh = fixture.source.refresh_identity_cache
    before = fixture.source.read_identity(ACCOUNT)
    def change(account):
        with fixture.repositories.database.transaction() as db:
            db.execute("UPDATE account_messages SET text='Changed after activation'")
        refresh(account)
    monkeypatch.setattr(fixture.source, "refresh_identity_cache", change)
    result = fixture.pipeline.project_account(ACCOUNT)
    assert result.artifact.projection.canonical_content_digest == before.content_digest
    assert fixture.stores.projections.get(ACCOUNT) is None

def test_private_build_staging_validates_without_a_full_graph_clone(fixture, monkeypatch):
    from app.analytics import sqlite_projection_store as storage
    def no_clone(*args, **kwargs):
        raise AssertionError("complete graph clone requested")
    monkeypatch.setattr(storage, "safe_graph_records", no_clone)
    result = fixture.pipeline.publish_candidate(fixture.pipeline.build_candidate(ACCOUNT))
    assert result.changed
    cold_equal(fixture, result.artifact)


def test_private_build_staging_still_rejects_unchecked_properties(fixture):
    raw = fixture.source.account_read_model(ACCOUNT)
    artifact = fixture.pipeline._build(ACCOUNT, raw, projection_generation=1)
    node = artifact.nodes[0].model_copy(update={"properties": {"text": "Synthetic forbidden property"}})
    bad = artifact.model_copy(update={"nodes": [node, *artifact.nodes[1:]]})
    with pytest.raises(ValueError):
        fixture.stores.projections.stage_built_artifact(bad, creator_account_id=ACCOUNT,
            canonical_identity=fixture.source.read_identity(ACCOUNT))
    assert fixture.stores.database.generations(ACCOUNT) == []

def test_mutating_private_graph_after_input_validation_fails_stored_verification(fixture, monkeypatch):
    fixture.pipeline.reuse_conversations = False
    fixture.pipeline.compact_graph = False
    validate = fixture.stores.projections._validate_artifact_shape
    def mutate(artifact, **kwargs):
        validate(artifact, **kwargs)
        node = next(node for node in artifact.nodes if node.kind.value == "message")
        node.properties["character_count"] = 999
    monkeypatch.setattr(fixture.stores.projections, "_validate_artifact_shape", mutate)
    with pytest.raises(ProjectionValidationError):
        fixture.pipeline.build_candidate(ACCOUNT)
    assert fixture.stores.projections.get(ACCOUNT) is None
    assert all(generation.status != "active" for generation in fixture.stores.database.generations(ACCOUNT))
