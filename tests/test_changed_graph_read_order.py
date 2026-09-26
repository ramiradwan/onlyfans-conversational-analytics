"""Physical read order preserves complete changed-segment verification."""

from dataclasses import replace

import pytest

from app.analytics import shared_graph
from app.analytics.errors import ProjectionBuildCancelled
from app.analytics.graph_store import GraphReferentialIntegrityError
from app.analytics.opaque_refs import account_ref
from tests.continuous_analytics_fixture import ACCOUNT, NOW, advance, cold_equal, insert_message
from tests.test_shared_graph import fixture


def selected_plans(db):
    row = db.execute("SELECT * FROM projection_generations WHERE status='active'").fetchone()
    return shared_graph.verify_generation_segments(
        db, row['generation_id'], row['creator_account_id'], lambda: None)


class CapturedConnection:
    def __init__(self, connection):
        self.connection = connection
        self.calls = []

    def execute(self, sql, parameters=()):
        self.calls.append((sql, parameters))
        return self.connection.execute(sql, parameters)


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_content_ordered_reads_match_the_streaming_verifier(fixture, kind):
    fixture.pipeline.project_account(ACCOUNT)
    account = account_ref(ACCOUNT)
    with fixture.stores.database.read() as db:
        plans = tuple(p for p in selected_plans(db) if p.kind == kind)
        captured = CapturedConnection(db)
        prepared = shared_graph._read_changed_segment_rows(
            captured, account, plans, lambda: None)
        assert prepared is not None
        calls = [(sql, values) for sql, values in captured.calls if sql.startswith('SELECT *')]
        assert calls
        contents = []
        for sql, values in calls:
            assert f'FROM graph_{kind}_content' in sql
            assert 'ORDER BY content_id' in sql
            assert values[0] == account
            assert 1 < len(values) <= 257
            assert list(values[1:]) == sorted(values[1:])
            contents.extend(values[1:])
        assert contents == sorted(contents)
        for plan in plans:
            rows = prepared[(kind, plan.segment_id)]
            assert shared_graph._verify_segment_rows(
                db, account, plan, lambda: None, prepared=rows
            ) == shared_graph._verify_segment_rows(db, account, plan, lambda: None)


@pytest.mark.parametrize('limit', ['MAX_CHANGED_VERIFICATION_ROWS', 'MAX_CHANGED_VERIFICATION_BYTES'])
def test_bounded_fallback_keeps_the_update_complete(fixture, monkeypatch, limit):
    fixture.pipeline.project_account(ACCOUNT)
    monkeypatch.setattr(shared_graph, limit, 1)
    observed = []
    original = shared_graph._read_changed_segment_rows
    def bounded(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append(result)
        return result
    monkeypatch.setattr(shared_graph, '_read_changed_segment_rows', bounded)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'bounded-read-new-message', NOW, 2)
        advance(db)
    result = fixture.pipeline.project_account(ACCOUNT)
    assert observed and all(value is None for value in observed)
    cold_equal(fixture, result.artifact)


@pytest.mark.parametrize('change', ['missing_generation', 'wrong_account', 'wrong_count'])
def test_membership_mismatches_are_not_hidden(fixture, change):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        plans = selected_plans(db)
        account = account_ref(ACCOUNT)
        if change == 'missing_generation':
            plans = (replace(plans[0], segment_id='absent'),)
        elif change == 'wrong_account':
            account = 'a1:' + '0' * 64
        else:
            plans = (replace(plans[0], count=plans[0].count - 1),)
        with pytest.raises(GraphReferentialIntegrityError, match='graph_segment_digest_invalid'):
            shared_graph._read_changed_segment_rows(db, account, plans, lambda: None)


@pytest.mark.parametrize('kind', ['node', 'edge'])
def test_changed_payload_is_still_independently_verified(fixture, kind):
    fixture.pipeline.project_account(ACCOUNT)
    account = account_ref(ACCOUNT)
    with fixture.stores.database.read() as db:
        plan = next(p for p in selected_plans(db) if p.kind == kind)
        identity = db.execute(
            f'SELECT content_id FROM graph_segment_{kind}s '
            'WHERE creator_account_id=? AND segment_id=? LIMIT 1',
            (account, plan.segment_id)).fetchone()[0]
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute(f'DROP TRIGGER graph_{kind}_content_immutable')
            db.execute(f'UPDATE graph_{kind}_content SET occurred_at=? '
                       'WHERE creator_account_id=? AND content_id=?',
                       ('2026-01-01T00:00:00.000000Z', account, identity))
            prepared = shared_graph._read_changed_segment_rows(db, account, (plan,), lambda: None)
            assert prepared is not None
            with pytest.raises(GraphReferentialIntegrityError, match='graph_segment_content_invalid'):
                shared_graph._verify_segment_rows(db, account, plan, lambda: None,
                    prepared=prepared[(kind, plan.segment_id)])
            with pytest.raises(GraphReferentialIntegrityError, match='graph_segment_content_invalid'):
                shared_graph._verify_segment_rows(db, account, plan, lambda: None)
        finally:
            db.rollback()


def test_unbounded_or_unknown_counts_use_streaming_without_read_ahead(fixture, monkeypatch):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        plan = selected_plans(db)[0]
        captured = CapturedConnection(db)
        for count in (-1, shared_graph.MAX_CHANGED_VERIFICATION_ROWS + 1):
            assert shared_graph._read_changed_segment_rows(
                captured, account_ref(ACCOUNT), (replace(plan, count=count),), lambda: None
            ) is None
        assert captured.calls == []


def test_cancellation_closes_read_ahead_cursors(fixture):
    fixture.pipeline.project_account(ACCOUNT)
    with fixture.stores.database.read() as db:
        plans = selected_plans(db)
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            if calls == 5:
                raise ProjectionBuildCancelled()
        with pytest.raises(ProjectionBuildCancelled):
            shared_graph._read_changed_segment_rows(db, account_ref(ACCOUNT), plans, cancelled)
        assert db.execute('SELECT 1').fetchone()[0] == 1


def test_mutation_after_read_ahead_cannot_publish(fixture, monkeypatch):
    from app.analytics.sqlite_projection_store import ProjectionValidationError

    previous = fixture.pipeline.project_account(ACCOUNT).artifact
    original = shared_graph._read_changed_segment_rows
    mutations = []
    def changed(connection, *args, **kwargs):
        result = original(connection, *args, **kwargs)
        if result is not None:
            cursor = connection.execute(
                "UPDATE projection_generations SET lease_expires_at=lease_expires_at "
                "WHERE status='building'")
            mutations.append(cursor.rowcount)
        return result
    monkeypatch.setattr(shared_graph, '_read_changed_segment_rows', changed)
    with fixture.repositories.database.transaction() as db:
        insert_message(db, 'chat-1', 'mutation-during-verification', NOW, 2)
        advance(db)
    with pytest.raises(ProjectionValidationError, match='stored graph changed during verification'):
        fixture.pipeline.build_candidate(ACCOUNT)
    assert mutations and all(value == 1 for value in mutations)
    with fixture.stores.database.read() as db:
        active = db.execute(
            "SELECT graph_digest FROM projection_generations WHERE status='active'"
        ).fetchone()[0]
        assert active == previous.projection.graph_digest
