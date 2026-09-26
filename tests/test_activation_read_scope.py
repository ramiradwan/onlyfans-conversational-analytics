"""Scoped activation reads reuse connections without retaining old witnesses."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest
from sqlcipher3 import dbapi2 as sqlite3

from tests.test_projection_activation import activation_case, reservation_fields


def reserve(case):
    return case.ledger.reserve(
        creator_account_id='account-a', generation_id='generation-a',
        canonical_identity=case.identity, **reservation_fields())


def capture_connections(monkeypatch, ledger):
    connections = []
    original = ledger.database.connect
    def connect():
        value = original()
        connections.append(value)
        return value
    monkeypatch.setattr(ledger.database, 'connect', connect)
    return connections


@pytest.mark.parametrize('activation_case', ['sqlite'], indirect=True)
def test_nested_reads_share_one_autocommit_connection(activation_case, monkeypatch):
    case = activation_case
    expected = reserve(case)
    connections = capture_connections(monkeypatch, case.ledger)
    with case.ledger.read_scope():
        assert case.ledger.get('generation-a') == expected
        with case.ledger.read_scope():
            assert case.ledger.get('generation-a') == expected
            assert case.ledger.get('missing') is None
        assert len(connections) == 1
        assert not connections[0].in_transaction
        assert connections[0].isolation_level is None
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute('SELECT 1')
    assert case.ledger.get('generation-a') == expected
    assert len(connections) == 2


@pytest.mark.parametrize('activation_case', ['sqlite'], indirect=True)
def test_each_read_observes_newly_committed_cancellation(activation_case):
    expected = reserve(activation_case)
    other = activation_case.reopen()
    with activation_case.ledger.read_scope():
        assert activation_case.ledger.get('generation-a') == expected
        cancelled = other.cancel(expected.intent_id)
        assert activation_case.ledger.get('generation-a') == cancelled


@pytest.mark.parametrize('activation_case', ['sqlite'], indirect=True)
def test_context_cannot_reuse_a_closed_connection(activation_case, monkeypatch):
    expected = reserve(activation_case)
    connections = capture_connections(monkeypatch, activation_case.ledger)
    with pytest.raises(RuntimeError, match='interrupted'):
        with activation_case.ledger.read_scope():
            context = copy_context()
            assert activation_case.ledger.get('generation-a') == expected
            raise RuntimeError('interrupted')
    assert context.run(activation_case.ledger.get, 'generation-a') == expected
    assert len(connections) == 2
    with pytest.raises(sqlite3.ProgrammingError):
        connections[0].execute('SELECT 1')


@pytest.mark.parametrize('activation_case', ['sqlite'], indirect=True)
def test_copied_context_does_not_share_between_threads(activation_case, monkeypatch):
    expected = reserve(activation_case)
    connections = capture_connections(monkeypatch, activation_case.ledger)
    with activation_case.ledger.read_scope():
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(context.run, activation_case.ledger.get, 'generation-a').result() == expected
        assert len(connections) == 2
        assert activation_case.ledger.get('generation-a') == expected
        assert len(connections) == 2


@pytest.mark.parametrize('activation_case', ['sqlite'], indirect=True)
def test_reads_reject_a_transaction_started_in_the_scope(activation_case, monkeypatch):
    reserve(activation_case)
    connections = capture_connections(monkeypatch, activation_case.ledger)
    with activation_case.ledger.read_scope():
        connections[0].execute('BEGIN')
        try:
            with pytest.raises(ValueError, match='activation_read_scope_requires_autocommit'):
                activation_case.ledger.get('generation-a')
        finally:
            connections[0].rollback()
        assert activation_case.ledger.get('generation-a') is not None


@pytest.mark.parametrize('activation_case', ['sqlite'], indirect=True)
def test_get_override_is_not_bypassed(activation_case, monkeypatch):
    reserve(activation_case)
    calls = []
    original = activation_case.ledger.get
    def get(generation):
        calls.append(generation)
        return original(generation)
    monkeypatch.setattr(activation_case.ledger, 'get', get)
    with activation_case.ledger.read_scope():
        for _ in range(5):
            activation_case.ledger.get('generation-a')
    assert calls == ['generation-a'] * 5


@pytest.mark.parametrize('revoke', [False, True])
def test_page_staging_keeps_each_witness_check(tmp_path, monkeypatch, revoke):
    from dataclasses import replace
    from app.persistence.projection_activation import _ACTIVATION_READ_SCOPE
    from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cleanup, insert_message, make_fixture

    fixture = make_fixture(tmp_path, conversations=5, messages=5)
    calls = []
    try:
        fixture.pipeline.project_account(ACCOUNT)
        ledger = fixture.stores.projections.activation
        original = ledger.get
        def get(generation):
            result = original(generation)
            scope = _ACTIVATION_READ_SCOPE.get()
            if scope is not None and scope.active:
                calls.append((generation, id(scope.connection)))
                if revoke and len(calls) == 2:
                    return replace(result, state='cancelled')
            return result
        monkeypatch.setattr(ledger, 'get', get)
        with fixture.repositories.database.transaction() as db:
            insert_message(db, 'chat-1', 'scoped-page-update', NOW, 2)
            advance(db)
        if revoke:
            with pytest.raises(ValueError, match='conversation_page_reference_unavailable'):
                fixture.pipeline.build_candidate(ACCOUNT)
            assert len(calls) == 2
        else:
            fixture.pipeline.project_account(ACCOUNT)
            assert len(calls) == 4
        assert len({connection for _, connection in calls}) == 1
        assert len({generation for generation, _ in calls}) == 1
        assert _ACTIVATION_READ_SCOPE.get() is None
    finally:
        cleanup(fixture)
