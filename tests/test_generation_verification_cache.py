"""Keep verification cache settings local without changing storage guarantees."""

import asyncio
from contextlib import contextmanager, nullcontext

import pytest

from app.analytics.database import (
    GENERATION_RETIREMENT_CACHE_KIB, GENERATION_VERIFICATION_CACHE_KIB,
    STARTUP_VALIDATION_CACHE_KIB, MAX_GENERATION_VERIFICATION_CACHE_KIB,
    PERSISTED_GENERATION_READ_CACHE_KIB,
    generation_retirement_cache, generation_verification_cache, startup_validation_cache,
)
from app.analytics.errors import ProjectionBuildCancelled
from app.persistence import sqlite_api as sqlite3

pytestmark = [pytest.mark.ci_tier('integration')]


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


@pytest.mark.parametrize('error', [None, ValueError, ProjectionBuildCancelled])
def test_retirement_cache_is_bounded_and_restored(connection, error):
    connection.execute('PRAGMA cache_size=-4096')
    with pytest.raises(error) if error else nullcontext():
        with generation_retirement_cache(connection):
            assert cache_size(connection) == -GENERATION_RETIREMENT_CACHE_KIB
            assert connection.in_transaction
            if error:
                raise error()
    assert cache_size(connection) == -4096
    assert connection.in_transaction


def test_retirement_cache_restores_outer_verification_cache(connection):
    connection.execute('PRAGMA cache_size=-2048')
    with generation_verification_cache(connection):
        assert cache_size(connection) == -GENERATION_VERIFICATION_CACHE_KIB
        with generation_retirement_cache(connection):
            assert cache_size(connection) == -GENERATION_RETIREMENT_CACHE_KIB
        assert cache_size(connection) == -GENERATION_VERIFICATION_CACHE_KIB
    assert cache_size(connection) == -2048


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


@pytest.mark.parametrize('initial', [-8000, 800])
@pytest.mark.parametrize('failure', [None, ValueError('synthetic validation'), asyncio.CancelledError()])
def test_startup_validation_cache_restores_exact_caller_state(connection, initial, failure):
    connection.execute(f'PRAGMA cache_size={initial}')
    connection.execute('PRAGMA query_only=ON')
    before = tuple(connection.execute('PRAGMA ' + name).fetchone()[0]
                   for name in ('synchronous', 'foreign_keys', 'query_only'))
    other = sqlite3.connect(':memory:')
    try:
        other_cache = cache_size(other)
        with pytest.raises(type(failure)) if failure is not None else nullcontext():
            with startup_validation_cache(connection):
                assert cache_size(connection) == -STARTUP_VALIDATION_CACHE_KIB
                assert connection.in_transaction
                assert cache_size(other) == other_cache
                if failure is not None:
                    raise failure
        assert cache_size(connection) == initial
        assert cache_size(other) == other_cache
        assert connection.in_transaction
        assert tuple(connection.execute('PRAGMA ' + name).fetchone()[0]
                     for name in ('synchronous', 'foreign_keys', 'query_only')) == before
        assert connection.execute('SELECT COUNT(*) FROM pending').fetchone()[0] == 1
    finally:
        other.close()


def test_matching_startup_cache_does_not_reconfigure(connection):
    connection.execute(f'PRAGMA cache_size={-STARTUP_VALIDATION_CACHE_KIB}')
    statements = []
    connection.set_trace_callback(statements.append)
    with startup_validation_cache(connection):
        pass
    assert not any('cache_size=' in sql for sql in statements)


@pytest.mark.parametrize('failure', [None, asyncio.CancelledError])
def test_matching_startup_cache_restores_a_body_change(connection, failure):
    initial = -STARTUP_VALIDATION_CACHE_KIB
    connection.execute(f'PRAGMA cache_size={initial}')
    error = failure() if failure else None
    with pytest.raises(failure) if failure else nullcontext() as raised:
        with startup_validation_cache(connection):
            connection.execute('PRAGMA cache_size=800')
            if error is not None:
                raise error
    if error is not None:
        assert raised.value is error
    assert cache_size(connection) == initial and connection.in_transaction


def test_shared_static_migration_validator_keeps_its_cache(connection):
    from app.persistence.migrations import MigrationRunner

    connection.execute('PRAGMA cache_size=800')
    MigrationRunner._validate_database(connection)
    assert cache_size(connection) == 800
    assert connection.in_transaction



@pytest.mark.parametrize('target', [128 * 1024, 512 * 1024])
@pytest.mark.parametrize('initial', [-8000, 800])
@pytest.mark.parametrize('failure', [None, ProjectionBuildCancelled, asyncio.CancelledError])
def test_optional_verification_target_restores_exact_state(connection, target, initial, failure):
    connection.execute(f'PRAGMA cache_size={initial}')
    profile = tuple(connection.execute('PRAGMA ' + name).fetchone()[0]
                    for name in ('synchronous', 'foreign_keys', 'query_only'))
    error = None if failure is None else failure()
    with pytest.raises(failure) if failure else nullcontext() as raised:
        with generation_verification_cache(connection, cache_kib=target):
            assert cache_size(connection) == -target and connection.in_transaction
            if error is not None:
                raise error
    if error is not None:
        assert raised.value is error
    assert cache_size(connection) == initial and connection.in_transaction
    assert tuple(connection.execute('PRAGMA ' + name).fetchone()[0]
                 for name in ('synchronous', 'foreign_keys', 'query_only')) == profile
    assert connection.execute('SELECT COUNT(*) FROM pending').fetchone()[0] == 1


@pytest.mark.parametrize('target', [0, -1, 512 * 1024 + 1, True, 1.5, '32768'])
def test_optional_verification_target_is_bounded_before_sql(connection, target):
    from unittest.mock import Mock

    observed = Mock(wraps=connection)
    with pytest.raises(ValueError, match='analytics_verification_cache_target_invalid'):
        with generation_verification_cache(observed, cache_kib=target):
            pytest.fail('invalid target entered the verification scope')
    observed.execute.assert_not_called()


def test_optional_target_matching_and_body_cache_change_restore_exactly(connection):
    connection.execute('PRAGMA cache_size=-131072')
    with generation_verification_cache(connection, cache_kib=128 * 1024):
        connection.execute('PRAGMA cache_size=800')
    assert cache_size(connection) == -131072
    assert connection.in_transaction


@pytest.mark.parametrize('failure', [None, ProjectionBuildCancelled, asyncio.CancelledError])
def test_verification_restore_failure_preserves_primary_base_exception(connection, failure):
    connection.execute('PRAGMA cache_size=800')
    restore_error = RuntimeError('synthetic verification restoration failed')
    primary = None if failure is None else failure()
    class RestoreFailure:
        def execute(self, sql):
            if sql == 'PRAGMA cache_size=800':
                raise restore_error
            return connection.execute(sql)
    expected = restore_error if primary is None else primary
    with pytest.raises(type(expected)) as raised:
        with generation_verification_cache(RestoreFailure(), cache_kib=512 * 1024):
            assert cache_size(connection) == -512 * 1024
            if primary is not None:
                raise primary
    assert raised.value is expected
    assert connection.in_transaction


def _observe_persisted_read_cache(monkeypatch, fixture, *, restore_failure=False):
    from app.analytics.database import ProjectionsDatabase
    from app.persistence.database import _TrackedConnection

    path = fixture.stores.database.path
    read = ProjectionsDatabase.read
    execute = _TrackedConnection.execute
    observed = {'connections': [], 'restored': [], 'targets': set()}
    restore_error = RuntimeError('synthetic full-read cache restoration failed')
    @contextmanager
    def owned_read(database):
        with read(database) as db:
            if database.path != path:
                yield db
                return
            execute(db, 'PRAGMA cache_size=800')
            observed['connections'].append(db)
            try:
                yield db
            finally:
                observed['restored'].append(execute(db, 'PRAGMA cache_size').fetchone()[0])
    def checked_execute(db, sql, *args, **kwargs):
        if db._tracked_path == path:
            if sql == f'PRAGMA cache_size={-PERSISTED_GENERATION_READ_CACHE_KIB}':
                observed['targets'].add(id(db))
            if restore_failure and id(db) in observed['targets'] and sql == 'PRAGMA cache_size=800':
                raise restore_error
        return execute(db, sql, *args, **kwargs)
    monkeypatch.setattr(ProjectionsDatabase, 'read', owned_read)
    monkeypatch.setattr(_TrackedConnection, 'execute', checked_execute)
    return observed, restore_error


@pytest.mark.parametrize('scope', ['full_read', 'projection_read', 'artifact_read', 'writer'])
def test_only_owned_nonmaterializing_full_read_uses_large_target(tmp_path, monkeypatch, scope):
    from app.analytics import sqlite_projection_store as module
    from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup

    fixture = make_fixture(tmp_path)
    try:
        artifact = fixture.pipeline.project_account(ACCOUNT).artifact
        generation = fixture.stores.database.active_generation(ACCOUNT).generation_id
        fixture.stores.projections.close_retention_scheduler()
        baseline = fixture.stores.database.open_connection_count(fixture.stores.database.path)
        seen = []
        recompute = module._recompute_generation
        def checked(db, *args, **kwargs):
            seen.append(cache_size(db))
            assert db.in_transaction
            return recompute(db, *args, **kwargs)
        monkeypatch.setattr(module, '_recompute_generation', checked)
        observed, _ = _observe_persisted_read_cache(monkeypatch, fixture)
        if scope == 'writer':
            with fixture.stores.database.transaction() as db:
                db.execute('PRAGMA cache_size=800')
                # Even a full nonmaterializing writer retains the default target.
                result = module.recompute_generation(db, generation, materialize_projection=False)
                assert cache_size(db) == 800 and db.in_transaction
        else:
            result = fixture.stores.projections._validate_persisted_generation(generation,
                materialize_projection=scope != 'full_read', materialize_graph=scope == 'artifact_read')
            assert observed['restored'] == [800]
            for db in observed['connections']:
                with pytest.raises(sqlite3.ProgrammingError):
                    db.execute('SELECT 1')
        target = PERSISTED_GENERATION_READ_CACHE_KIB if scope == 'full_read' else GENERATION_VERIFICATION_CACHE_KIB
        assert seen == [-target]
        assert result['projection_digest'] == artifact.projection.projection_digest
        assert result['graph_digest'] == artifact.projection.graph_digest
        assert fixture.stores.database.open_connection_count(fixture.stores.database.path) == baseline
    finally:
        cleanup(fixture)


def _valid_startup_capture(fixture, generation):
    store = fixture.stores.projections
    with fixture.stores.database.read() as db:
        row = dict(db.execute(
            'SELECT * FROM projection_generations WHERE generation_id=?',
            (generation,),
        ).fetchone())
    witness = fixture.repositories.projection_activation.get(generation)
    assert row['status'] == 'active'
    assert witness is not None and witness.state == 'completed'
    assert store._intent_matches(row, witness, require_completed=True)
    # Cold startup has no pre-existing process-local warm envelope. Retaining
    # the real verification result must now be possible in the success control.
    with store._verification_envelope_lock:
        store._startup_verifications.clear()
        store._verification_envelopes.clear()
    return row, witness


@pytest.mark.parametrize('failure', [ProjectionBuildCancelled, asyncio.CancelledError])
@pytest.mark.parametrize('restore_failure', [False, True])
def test_full_read_cancellation_closes_and_never_retains_startup_handoff(
    tmp_path, monkeypatch, failure, restore_failure,
):
    from app.analytics import sqlite_projection_store as module
    from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup

    fixture = make_fixture(tmp_path)
    try:
        fixture.pipeline.project_account(ACCOUNT)
        generation = fixture.stores.database.active_generation(ACCOUNT).generation_id
        fixture.stores.projections.close_retention_scheduler()
        baseline = fixture.stores.database.open_connection_count(fixture.stores.database.path)
        capture = _valid_startup_capture(fixture, generation)
        primary = failure()
        observed, _ = _observe_persisted_read_cache(monkeypatch, fixture, restore_failure=restore_failure)
        def cancelled(db, *args, **kwargs):
            assert cache_size(db) == -PERSISTED_GENERATION_READ_CACHE_KIB
            raise primary
        monkeypatch.setattr(module, '_recompute_generation', cancelled)
        monkeypatch.setattr(fixture.stores.projections, '_retain_startup_verification',
            lambda *args: pytest.fail('cancelled full read retained a startup handoff'))
        with pytest.raises(failure) as raised:
            fixture.stores.projections._validate_persisted_generation(generation,
                materialize_projection=False, capture_startup=capture)
        assert raised.value is primary
        assert not fixture.stores.projections._startup_verifications
        assert observed['restored'] == [-PERSISTED_GENERATION_READ_CACHE_KIB if restore_failure else 800]
        assert fixture.stores.database.open_connection_count(fixture.stores.database.path) == baseline
        for db in observed['connections']:
            with pytest.raises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('restore_failure', [False, True])
def test_successful_full_read_handoff_requires_cache_restoration(tmp_path, monkeypatch, restore_failure):
    from app.analytics import sqlite_projection_store as module
    from tests.continuous_analytics_fixture import ACCOUNT, make_fixture, cleanup

    fixture = make_fixture(tmp_path)
    try:
        artifact = fixture.pipeline.project_account(ACCOUNT).artifact
        generation = fixture.stores.database.active_generation(ACCOUNT).generation_id
        fixture.stores.projections.close_retention_scheduler()
        baseline = fixture.stores.database.open_connection_count(fixture.stores.database.path)
        capture = _valid_startup_capture(fixture, generation)
        observed, restore_error = _observe_persisted_read_cache(
            monkeypatch, fixture, restore_failure=restore_failure)
        completed, retained = [], []
        recompute = module._recompute_generation
        retain = fixture.stores.projections._retain_startup_verification
        def checked(*args, **kwargs):
            value = recompute(*args, **kwargs)
            completed.append(True)
            return value
        def checked_retain(*args):
            assert cache_size(observed['connections'][-1]) == 800
            retained.append(True)
            return retain(*args)
        monkeypatch.setattr(module, '_recompute_generation', checked)
        monkeypatch.setattr(fixture.stores.projections, '_retain_startup_verification', checked_retain)
        if restore_failure:
            with pytest.raises(RuntimeError) as raised:
                fixture.stores.projections._validate_persisted_generation(generation,
                    materialize_projection=False, capture_startup=capture)
            assert raised.value is restore_error
            assert retained == [] and not fixture.stores.projections._startup_verifications
        else:
            values = fixture.stores.projections._validate_persisted_generation(generation,
                materialize_projection=False, capture_startup=capture)
            assert values['projection_digest'] == artifact.projection.projection_digest
            assert values['graph_digest'] == artifact.projection.graph_digest
            assert retained == [True]
            saved = fixture.stores.projections._startup_verifications[generation]
            assert saved.row == tuple(sorted(capture[0].items())) and saved.witness == capture[1]
            assert saved.receipt.generation_id == saved.envelope.generation_id == generation
        assert completed == [True]
        assert observed['restored'] == [-PERSISTED_GENERATION_READ_CACHE_KIB if restore_failure else 800]
        assert fixture.stores.database.open_connection_count(fixture.stores.database.path) == baseline
        for db in observed['connections']:
            with pytest.raises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')
    finally:
        cleanup(fixture)
