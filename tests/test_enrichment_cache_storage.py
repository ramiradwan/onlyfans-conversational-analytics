"""Qualify stored reuse lifecycle without making the cache authoritative."""

import json
import subprocess
import sys
from datetime import timedelta

import pytest

from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.enrichment import EnrichmentStage
from app.analytics.enrichment_cache import CachedEnrichment, MAX_ENTRY_BYTES, validate_entries
from app.analytics.factory import create_analytics_stores
from app.analytics.identity import canonical_identity
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.opaque_refs import account_ref
from tests.test_enrichment_reuse import setup, counts, NOW, ACCOUNT, counters
from tests.test_analytics_evidence import stored, ACCOUNT as STORED_ACCOUNT


def test_retired_generations_do_not_retain_reuse_records(setup):
    source, pipeline, _, stores = setup
    pipeline.project_account(ACCOUNT)
    source.conversations["chat"]["messages"].pop()
    source.revision += 1
    pipeline.project_account(ACCOUNT)
    if stores.database is None:
        assert all(not g.enrichment_entries for g in stores.projections._generations.values() if g.status == "retired")
    else:
        with stores.database.read() as db:
            assert db.execute("SELECT count(*) FROM enrichment_reuse").fetchone()[0] == 6
            assert db.execute("SELECT count(*) FROM enrichment_reuse c JOIN projection_generations g USING(generation_id) WHERE g.status='retired'").fetchone()[0] == 0


def test_clear_removes_cached_values(setup):
    _, pipeline, analyzers, stores = setup
    pipeline.project_account(ACCOUNT)
    stores.projections.clear(ACCOUNT)
    pipeline.project_account(ACCOUNT)
    assert counts(analyzers) == [6, 6, 6]


def test_cache_eviction_preserves_the_clean_rebuild_result(setup):
    source, pipeline, analyzers, stores = setup
    first = pipeline.project_account(ACCOUNT)
    if stores.database is None:
        for generation in stores.projections._generations.values():
            generation.enrichment_entries.clear()
    else:
        with stores.database.transaction() as db:
            db.execute("DELETE FROM enrichment_reuse")
    second = pipeline.rebuild_account(ACCOUNT)
    assert first.artifact == second.artifact
    assert counts(analyzers) == [6, 6, 6]


def test_cache_generation_rows_are_immutable(setup):
    _, pipeline, _, stores = setup
    if stores.database is None:
        pytest.skip("SQL trigger contract")
    pipeline.project_account(ACCOUNT)
    from app.persistence import sqlite_api
    with pytest.raises(sqlite_api.IntegrityError, match="enrichment_reuse_immutable"):
        with stores.database.transaction() as db:
            db.execute("UPDATE enrichment_reuse SET document_digest=?", ("0" * 64,))


def test_corrupted_serialization_is_a_miss_not_a_result(setup):
    _, pipeline, analyzers, stores = setup
    first = pipeline.project_account(ACCOUNT)
    if stores.database is None:
        generation = stores.projections._active_generation_locked(ACCOUNT)
        generation.enrichment_entries = {key: b'{}' for key in generation.enrichment_entries}
    else:
        with stores.database.transaction() as db:
            db.execute("DROP TRIGGER enrichment_reuse_update_blocked")
            db.execute("UPDATE enrichment_reuse SET document_digest=?", ("0" * 64,))
    assert pipeline.rebuild_account(ACCOUNT).artifact == first.artifact
    assert counts(analyzers) == [6, 6, 6]


def test_idle_expiry_clears_persistent_cache(setup):
    _, pipeline, _, stores = setup
    if stores.database is None:
        pytest.skip("persistent source-time timer contract")
    pipeline.project_account(ACCOUNT)
    stores.projections._retention_clock = lambda: NOW + timedelta(days=90)
    assert stores.projections.enforce_retention(ACCOUNT)
    with stores.database.read() as db:
        assert db.execute("SELECT count(*) FROM enrichment_reuse").fetchone()[0] == 0


def test_enrichment_reuse_survives_a_new_process(stored, tmp_path):
    source = HistoryAnalyticsSource(stored.history)
    path = tmp_path/'analytics.sqlite3'
    stores = create_analytics_stores("sqlite", projections_path=path,
        activation=stored.projection_activation,
        canonical_identity_reader=lambda account: canonical_identity(source.account_read_model(account)),
        retention_clock=lambda: NOW)
    analyzers = counters()
    pipeline = AnalyticsPipeline(source, projections=stores.projections, clock=lambda: NOW,
        enrichment=EnrichmentStage(sentiment=analyzers[0], topics_entities=analyzers[1], engagement=analyzers[2]))
    first = pipeline.project_account(STORED_ACCOUNT)
    stores.projections.close_retention_scheduler()
    program = '''
import json,sys
from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.enrichment import EnrichmentStage
from app.analytics.factory import create_analytics_stores
from app.analytics.identity import canonical_identity
from app.analytics.pipeline import AnalyticsPipeline
from app.persistence.database import CanonicalSQLite
from app.persistence.history import HistoryRepository
from app.persistence.projection_activation import SQLiteProjectionActivationRepository
from tests.test_enrichment_reuse import NOW, counters, counts
canonical = CanonicalSQLite(sys.argv[1])
source = HistoryAnalyticsSource(HistoryRepository(canonical))
stores = create_analytics_stores("sqlite", projections_path=sys.argv[2],
    activation=SQLiteProjectionActivationRepository(canonical),
    canonical_identity_reader=lambda account: canonical_identity(source.account_read_model(account)),
    retention_clock=lambda: NOW)
analyzers = counters()
pipeline = AnalyticsPipeline(source, projections=stores.projections, clock=lambda: NOW,
    enrichment=EnrichmentStage(sentiment=analyzers[0], topics_entities=analyzers[1], engagement=analyzers[2]))
result = pipeline.rebuild_account(sys.argv[3])
stores.projections.close_retention_scheduler()
print(json.dumps({"calls": counts(analyzers), "digest": result.artifact.projection.projection_digest}))
'''
    run = subprocess.run([sys.executable, "-c", program, str(stored.database.path), str(path), STORED_ACCOUNT],
                         capture_output=True, text=True, check=True, timeout=40)
    output = json.loads(run.stdout)
    assert output["calls"] == [0, 0, 0]
    assert output["digest"] == first.artifact.projection.projection_digest


def test_cache_payloads_hold_no_input_text(setup):
    source, pipeline, _, stores = setup
    result = pipeline.project_account(ACCOUNT)
    if stores.database is None:
        values = stores.projections._active_generation_locked(ACCOUNT).enrichment_entries.values()
    else:
        with stores.database.read() as db:
            values = [row[0].encode() for row in db.execute("SELECT document_json FROM enrichment_reuse")]
    assert len(values) == 9
    for data in values:
        assert len(data) <= MAX_ENTRY_BYTES
        assert CachedEnrichment.model_validate_json(data).key.account_ref == account_ref(ACCOUNT)
        assert all(message["text"].encode() not in data for message in source.conversations["chat"]["messages"])
    with pytest.raises(ValueError):
        validate_entries(result.artifact, (b'{}',))
