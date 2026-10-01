"""Repeat currentness uses checked immutable contents, not unverified stored digests."""
from unittest.mock import Mock

import pytest

from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup

pytestmark = [pytest.mark.ci_tier('integration')]


@pytest.fixture
def proven(tmp_path):
    f = make_fixture(tmp_path)
    f.pipeline.project_account(ACCOUNT)
    f.now = [0.0]
    f.stores.projections._currentness.clock = lambda: f.now[0]
    yield f
    cleanup(f)


def current(f):
    return f.pipeline.projection_is_current(ACCOUNT, 1)


def test_currentness_expiry_rechecks_existing_verified_content_without_materialization(proven, monkeypatch):
    f = proven
    full = Mock(side_effect=AssertionError('unchanged verified contents need no new full materialization'))
    monkeypatch.setattr(f.stores.projections, 'get', full)
    assert current(f)
    first = next(iter(f.stores.projections._currentness.entries.values()))
    assert first.expires_at == 60
    f.now[0] = 59
    assert current(f)
    assert next(iter(f.stores.projections._currentness.entries.values())) is first
    f.now[0] = 60
    assert current(f)
    assert next(iter(f.stores.projections._currentness.entries.values())).expires_at == 120
    full.assert_not_called()


@pytest.mark.parametrize('cache', ['_graph_segment_proofs', '_conversation_graph_proofs',
                                  '_conversation_enrichment_proofs'])
def test_missing_any_content_proof_requires_full_verification(proven, monkeypatch, cache):
    f = proven
    getattr(f.stores.projections, cache).clear()
    full = Mock(wraps=f.stores.projections.get)
    monkeypatch.setattr(f.stores.projections, 'get', full)
    assert current(f)
    full.assert_called_once()


@pytest.mark.parametrize('fault', ['node', 'document'])
def test_guard_changes_and_actual_corruption_cannot_use_content_proofs(proven, fault):
    from app.analytics.sqlite_projection_store import ProjectionValidationError
    f = proven
    assert current(f)
    f.now[0] = 61
    with f.stores.database.transaction() as db:
        if fault == 'node':
            db.execute('DROP TRIGGER graph_node_content_immutable')
            db.execute("UPDATE graph_node_content SET properties_json=json_set(properties_json,'$.character_count',999) WHERE kind='message'")
        else:
            db.execute('DROP TRIGGER projection_document_update_blocked')
            db.execute("UPDATE analytics_projections SET document_json=json_set(document_json,'$.creator_metrics.message_count',999)")
    with pytest.raises(ProjectionValidationError):
        current(f)


@pytest.mark.parametrize('fault', ['source', 'witness', 'expiry', 'pipeline', 'account'])
def test_verified_content_does_not_replace_live_authority_checks(proven, monkeypatch, fault):
    from datetime import timedelta
    f = proven
    assert current(f)
    f.now[0] = 61
    if fault == 'source':
        with f.repositories.database.transaction() as db:
            db.execute("UPDATE account_messages SET text='A changed canonical message'")
    elif fault == 'witness':
        monkeypatch.setattr(f.stores.projections.activation, 'get', lambda _: None)
    elif fault == 'expiry':
        f.clock.now += timedelta(days=91)
    elif fault == 'pipeline':
        f.pipeline.pipeline_config_digest = 'sha256:' + '0' * 64
    else:
        assert not f.pipeline.projection_is_current('other-account', 1)
        return
    assert not current(f)
