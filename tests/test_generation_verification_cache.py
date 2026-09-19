"""Keep verification cache settings local without changing storage guarantees."""

from contextlib import nullcontext

import pytest

from app.analytics.database import (
    GENERATION_VERIFICATION_CACHE_KIB, generation_verification_cache,
)
from app.analytics.errors import ProjectionBuildCancelled
from app.persistence import sqlite_api as sqlite3


@pytest.fixture
def connection():
    value = sqlite3.connect(':memory:', isolation_level=None)
    value.execute('PRAGMA foreign_keys=ON')
    value.execute('PRAGMA synchronous=FULL')
    value.execute('CREATE TABLE pending(value TEXT)')
    value.execute('BEGIN')
    value.execute("INSERT INTO pending VALUES ('synthetic')")
    try:
        yield value
    finally:
        value.close()


def cache_size(connection):
    return connection.execute('PRAGMA cache_size').fetchone()[0]


@pytest.mark.parametrize('initial', [-2000, -8000, -16384, -32768, 800])
@pytest.mark.parametrize('error', [None, ValueError, ProjectionBuildCancelled])
def test_setting_restored_on_success_failure_and_cancellation(connection, initial, error):
    connection.execute(f'PRAGMA cache_size={initial}')
    before = tuple(connection.execute('PRAGMA ' + name).fetchone()[0]
                   for name in ('synchronous', 'foreign_keys', 'query_only'))
    with pytest.raises(error) if error else nullcontext():
        with generation_verification_cache(connection):
            assert cache_size(connection) == -GENERATION_VERIFICATION_CACHE_KIB
            assert connection.in_transaction
            if error:
                raise error()
    assert cache_size(connection) == initial
    assert connection.in_transaction
    after = tuple(connection.execute('PRAGMA ' + name).fetchone()[0]
                  for name in ('synchronous', 'foreign_keys', 'query_only'))
    assert after == before
    assert connection.execute('SELECT COUNT(*) FROM pending').fetchone()[0] == 1
    connection.rollback()
    assert connection.execute('SELECT COUNT(*) FROM pending').fetchone()[0] == 0


def test_query_only_connection_keeps_its_setting(connection):
    connection.execute('PRAGMA query_only=ON')
    before = cache_size(connection)
    with generation_verification_cache(connection):
        assert connection.execute('PRAGMA query_only').fetchone()[0] == 1
    assert cache_size(connection) == before


def test_other_connections_are_unchanged(connection):
    other = sqlite3.connect(':memory:')
    try:
        before = cache_size(other)
        with generation_verification_cache(connection):
            assert cache_size(other) == before
        assert cache_size(other) == before
    finally:
        other.close()


def test_nested_verification_restores_the_outer_setting(connection):
    connection.execute('PRAGMA cache_size=-2048')
    with generation_verification_cache(connection):
        with generation_verification_cache(connection):
            assert cache_size(connection) == -GENERATION_VERIFICATION_CACHE_KIB
        assert cache_size(connection) == -GENERATION_VERIFICATION_CACHE_KIB
    assert cache_size(connection) == -2048


def test_matching_target_is_not_reconfigured(connection):
    connection.execute(f'PRAGMA cache_size={-GENERATION_VERIFICATION_CACHE_KIB}')
    statements = []
    connection.set_trace_callback(statements.append)
    with generation_verification_cache(connection):
        pass
    assert not any('cache_size=' in sql for sql in statements)


@pytest.mark.parametrize('shared', [False, True])
@pytest.mark.parametrize('materialize', [False, True])
def test_generation_verification_preserves_results_and_caller_state(tmp_path, monkeypatch, shared, materialize):
    from app.analytics import sqlite_projection_store as module
    from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup

    fixture = make_fixture(tmp_path)
    fixture.stores.projections.reuse_graph_content = shared
    try:
        artifact = fixture.pipeline.project_account(ACCOUNT).artifact
        generation = fixture.stores.database.active_generation(ACCOUNT).generation_id
        original = module._recompute_generation
        observed = []
        def verify(db, *args, **kwargs):
            observed.append(cache_size(db))
            return original(db, *args, **kwargs)
        monkeypatch.setattr(module, '_recompute_generation', verify)
        with fixture.stores.database.read() as db:
            db.execute('PRAGMA cache_size=-4096')
            db.execute('BEGIN')
            result = module.recompute_generation(db, generation, materialize_graph=materialize)
            assert cache_size(db) == -4096 and db.in_transaction
            assert result['projection'] == artifact.projection
            assert result['graph_digest'] == artifact.projection.graph_digest
            assert result['node_count'] == len(artifact.nodes)
            assert result['edge_count'] == len(artifact.edges)
            assert result['nodes'] == (artifact.nodes if materialize else [])
            assert result['edges'] == (artifact.edges if materialize else [])
        assert observed == [-GENERATION_VERIFICATION_CACHE_KIB]
    finally:
        cleanup(fixture)


def test_generation_failure_restores_connection_and_remains_a_failure(tmp_path):
    from app.analytics.sqlite_projection_store import recompute_generation, ProjectionValidationError
    from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup

    fixture = make_fixture(tmp_path)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        generation = fixture.stores.database.active_generation(ACCOUNT).generation_id
        with fixture.stores.database.transaction() as db:
            db.execute('DROP TRIGGER graph_node_content_immutable')
            db.execute("UPDATE graph_node_content SET properties_json=json_set(properties_json,'$.character_count',999) WHERE kind='message'")
        with fixture.stores.database.read() as db:
            before = cache_size(db)
            with pytest.raises(ProjectionValidationError):
                recompute_generation(db, generation)
            assert cache_size(db) == before
    finally:
        cleanup(fixture)
