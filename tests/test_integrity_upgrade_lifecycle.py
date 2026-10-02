"""Legacy integrity upgrades use the normal owned scheduler publication."""
import pytest
from app.analytics.canonical_source import HistoryAnalyticsSource
from app.analytics.factory import create_analytics_stores
from app.analytics.pipeline import AnalyticsPipeline
from app.analytics.scheduling import InProcessProjectionScheduler
from tests.continuous_analytics_fixture import ACCOUNT, NOW, cleanup
from tests.test_conversation_integrity_migration import legacy_store

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]

@pytest.mark.asyncio
async def test_legacy_startup_upgrades_once_without_changing_public_output(tmp_path):
    f, catalog, database, options, previous = legacy_store(tmp_path)
    expected = previous.project_account(ACCOUNT).artifact
    with database.read() as db:
        old = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
    f.source = HistoryAnalyticsSource(f.repositories.history)
    f.stores = create_analytics_stores('sqlite', projections_path=database.path,
        activation=f.repositories.projection_activation,
        canonical_identity_reader=f.source.read_identity, retention_clock=lambda: NOW, lazy=True)
    f.pipeline = AnalyticsPipeline(f.source, projections=f.stores.projections,
        enrichment=f.pipeline.enrichment, clock=lambda: NOW)
    scheduler = InProcessProjectionScheduler(f.pipeline, reconciliation_interval=30)
    try:
        await scheduler.start(recover=True)
        await scheduler.wait(ACCOUNT)
        store = f.stores.projections._store
        with store.database.read() as db:
            current = db.execute("SELECT generation_id FROM projection_generations WHERE status='active'").fetchone()[0]
            versions = {row[0] for row in db.execute("SELECT u.checksum_version FROM conversation_graph_refs r JOIN conversation_graph_units u USING(creator_account_id,unit_id) WHERE r.generation_id=?", (current,))}
        assert versions == {2}, versions
        assert current != old
        actual = store.get_artifact(ACCOUNT)
        assert actual.nodes == expected.nodes and actual.edges == expected.edges
        assert actual.projection.graph_digest == expected.projection.graph_digest
        assert actual.projection.projection_digest == expected.projection.projection_digest
        assert await scheduler._projection_is_current(ACCOUNT, 1)
        assert not f.pipeline.build_candidate(ACCOUNT).requires_publication
    finally:
        assert await scheduler.close(timeout=10)
        cleanup(f)


@pytest.mark.asyncio
async def test_legacy_upgrade_with_refused_units_does_not_loop(tmp_path, monkeypatch):
    from app.analytics import conversation_integrity
    f, catalog, database, options, previous = legacy_store(tmp_path)
    expected = previous.project_account(ACCOUNT).artifact
    f.source = HistoryAnalyticsSource(f.repositories.history)
    f.stores = create_analytics_stores('sqlite', projections_path=database.path,
        activation=f.repositories.projection_activation,
        canonical_identity_reader=f.source.read_identity, retention_clock=lambda: NOW, lazy=True)
    f.pipeline = AnalyticsPipeline(f.source, projections=f.stores.projections,
        enrichment=f.pipeline.enrichment, clock=lambda: NOW)
    monkeypatch.setattr(conversation_integrity, 'MAX_GROUP_RECORDS', 0)
    scheduler = InProcessProjectionScheduler(f.pipeline)
    try:
        await scheduler.start(recover=True)
        await scheduler.wait(ACCOUNT)
        assert not f.pipeline._requires_integrity_upgrade(ACCOUNT)
        assert not f.pipeline.build_candidate(ACCOUNT).requires_publication
        actual = f.stores.projections.get_artifact(ACCOUNT)
        assert actual.nodes == expected.nodes and actual.edges == expected.edges
        assert actual.projection.graph_digest == expected.projection.graph_digest
    finally:
        assert await scheduler.close(timeout=10)
        cleanup(f)
