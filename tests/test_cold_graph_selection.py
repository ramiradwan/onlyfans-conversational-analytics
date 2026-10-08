"""Cold selected-content reuse keeps the complete SQL validation contract."""

import asyncio
from itertools import islice

import pytest

from app.analytics import cold_graph_selection as selection
from app.analytics import conversation_integrity_store as integrity
from app.analytics import graph_endpoint_validation as endpoints, shared_graph
from app.analytics.conversation_graph_units import ConversationGraphProof
from app.analytics.conversation_id_frames import EncodedIds
from app.analytics.graph_store import GraphReferentialIntegrityError
from app.analytics.opaque_refs import account_ref
from app.analytics.sqlite_projection_store import recompute_generation
from app.analytics.validation_receipt import content_stamp, generation_binding
from tests.continuous_analytics_fixture import ACCOUNT, cleanup, make_fixture
from tests.test_cold_graph_endpoint_validation import damage, stable_values

pytestmark = [pytest.mark.ci_tier('integration')]


@pytest.fixture
def candidate(tmp_path):
    fixture = make_fixture(tmp_path)
    try:
        built = fixture.pipeline.build_candidate(ACCOUNT)
        yield fixture, built.staged_generation_id
    finally:
        cleanup(fixture)


def generation_row(db, generation):
    return db.execute('SELECT * FROM projection_generations WHERE generation_id=?',
                      (generation,)).fetchone()


def first_node(db, generation):
    rows = shared_graph.ordered_rows(db, generation, account_ref(ACCOUNT), 'node')
    try:
        return dict(next(rows))
    finally:
        rows.close()


def capture_collectors(monkeypatch):
    captured = []
    original = selection.ColdGraphSelection.__init__

    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(selection.ColdGraphSelection, '__init__', initialize)
    return captured


def count_sql(monkeypatch):
    calls = []
    original = shared_graph.selected_content_ids

    def selected(*args, **kwargs):
        calls.append((args[3], kwargs.get('page_layout')))
        return original(*args, **kwargs)

    monkeypatch.setattr(shared_graph, 'selected_content_ids', selected)
    return calls


def ready_selection(db, generation):
    row = generation_row(db, generation)
    collected = selection.ColdGraphSelection(db, row, account_ref(ACCOUNT), lambda: None)
    verified = endpoints.verify_cold_graph_rows(
        db, generation, account_ref(ACCOUNT), lambda: None, _selection=collected)
    assert verified is not None and verified.segments
    assert collected.seal()
    return collected, row


@pytest.mark.parametrize('large_groups', [False, True])
@pytest.mark.parametrize('endpoint_sql', [False, True])
def test_cold_output_matches_forced_sql_in_both_group_paths(
        candidate, monkeypatch, large_groups, endpoint_sql):
    fixture, generation = candidate
    if large_groups:
        monkeypatch.setattr(integrity, 'MAX_FULL_SELECTION_IDS', 0)
    if endpoint_sql:
        monkeypatch.setattr(endpoints, 'MAX_COLD_ENDPOINT_ROWS', 0)
    captured = capture_collectors(monkeypatch)
    sql = count_sql(monkeypatch)
    hits = []
    original = selection.ColdGraphSelection.selected

    def selected(self, kind, bucket, identities):
        result = original(self, kind, bucket, identities)
        if result:
            hits.append((bucket, type(identities), self.ready))
        return result

    monkeypatch.setattr(selection.ColdGraphSelection, 'selected', selected)
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        actual = recompute_generation(db, generation, materialize_projection=False)
        assert sql == []
        assert hits and all(ready for _, _, ready in hits)
        if large_groups:
            assert all(bucket is not None and kind is EncodedIds
                       for bucket, kind, _ in hits)
        else:
            assert any(bucket is None for bucket, _, _ in hits)
        monkeypatch.setattr(selection, 'MAX_COLD_SELECTION_ROWS', 0)
        expected = recompute_generation(db, generation, materialize_projection=False)
        assert sql and all(page_layout for _, page_layout in sql)
        assert stable_values(actual) == stable_values(expected)
        assert set(actual) == set(expected)
        assert len(captured) == 2
        assert captured[0].peak_bytes > 0
        assert all(not item.ready and item.rows == item.retained_bytes == 0
                   for item in captured)
        assert db.in_transaction
        db.rollback()


@pytest.mark.parametrize('bound', ['MAX_COLD_SELECTION_ROWS', 'MAX_COLD_SELECTION_BYTES'])
def test_capacity_refusal_releases_data_and_runs_complete_sql(candidate, monkeypatch, bound):
    fixture, generation = candidate
    monkeypatch.setattr(selection, bound, 1)
    captured = capture_collectors(monkeypatch)
    sql = count_sql(monkeypatch)
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        assert recompute_generation(db, generation, materialize_projection=False)['node_count'] > 1
        assert sql
        assert len(captured) == 1
        assert captured[0].discard_reason == 'capacity'
        assert captured[0].rows == captured[0].retained_bytes == 0
        assert not captured[0].ready
        db.rollback()


def test_dictionary_resize_is_charged_before_any_lookup(candidate, monkeypatch):
    fixture, generation = candidate
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        cursor = shared_graph.ordered_rows(db, generation, account_ref(ACCOUNT), 'node')
        try:
            rows = [dict(row) for row in islice(cursor, 6)]
        finally:
            cursor.close()
        assert len(rows) == 6
        record = generation_row(db, generation)
        probe = selection.ColdGraphSelection(db, record, account_ref(ACCOUNT), lambda: None)
        probe._begin_graph(db, generation, account_ref(ACCOUNT))
        for row in rows[:5]:
            probe.collect('node', row, row['node_id'], row['content_id'])
        before_resize = probe.retained_bytes
        row = rows[5]
        probe.collect('node', row, row['node_id'], row['content_id'])
        after_resize = probe.retained_bytes
        assert after_resize > before_resize
        probe.finish()
        monkeypatch.setattr(selection, 'MAX_COLD_SELECTION_BYTES', after_resize - 1)
        bounded = selection.ColdGraphSelection(db, record, account_ref(ACCOUNT), lambda: None)
        bounded._begin_graph(db, generation, account_ref(ACCOUNT))
        for row in rows[:5]:
            bounded.collect('node', row, row['node_id'], row['content_id'])
        assert bounded.rows == 5 and bounded.retained_bytes == before_resize
        row = rows[5]
        bounded.collect('node', row, row['node_id'], row['content_id'])
        assert bounded.discard_reason == 'capacity'
        assert bounded.peak_bytes == after_resize
        assert bounded.rows == bounded.retained_bytes == 0
        assert bounded.selected('node', None, [row['node_id']]) is None
        bounded.finish()
        db.rollback()


@pytest.mark.parametrize('scope', ['materialized', 'graph_proof', 'conversation_proof'])
def test_ineligible_recomputation_does_not_construct_collector(candidate, monkeypatch, scope):
    fixture, generation = candidate

    def forbidden(*args, **kwargs):
        raise AssertionError('cold collector entered an ineligible scope')

    monkeypatch.setattr(selection.ColdGraphSelection, '__init__', forbidden)
    kwargs = {'materialize_projection': scope == 'materialized'}
    if scope == 'graph_proof':
        kwargs['graph_validation'] = shared_graph.SharedGraphValidation('', (), None)
    elif scope == 'conversation_proof':
        kwargs['conversation_validation'] = ConversationGraphProof('unused', '', (), ())
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        assert recompute_generation(db, generation, **kwargs)['node_count'] > 0
        db.rollback()


def test_autocommit_collector_cannot_start_a_validation_scope(candidate):
    fixture, generation = candidate
    with fixture.stores.database.read() as db:
        assert not db.in_transaction
        collected = selection.ColdGraphSelection(
            db, generation_row(db, generation), account_ref(ACCOUNT), lambda: None)
        assert collected.discard_reason == 'scope_unavailable'
        assert not collected.seal()
        collected.close()
        assert not db.in_transaction


@pytest.mark.parametrize('field', [
    'generation_id', 'creator_account_id', 'segment_bucket', 'page_bucket',
    'content_id', 'node_id',
])
def test_ineligible_observed_row_discards_all_collected_content(candidate, field):
    fixture, generation = candidate
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        row = first_node(db, generation)
        collected = selection.ColdGraphSelection(
            db, generation_row(db, generation), account_ref(ACCOUNT), lambda: None)
        collected._begin_graph(db, generation, account_ref(ACCOUNT))
        collected.collect('node', row, row['node_id'], row['content_id'])
        assert collected.rows == 1 and collected.retained_bytes > 0
        bad = dict(row, **{field: 'wrong'})
        collected.collect('node', bad, row['node_id'], row['content_id'])
        assert collected.discard_reason == 'row_ineligible'
        assert collected.rows == collected.retained_bytes == 0
        assert not collected.seal()
        collected.finish()
        assert db.in_transaction
        db.rollback()


def test_duplicate_observation_and_incomplete_walk_cannot_be_sealed(candidate):
    fixture, generation = candidate
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        row = first_node(db, generation)
        for duplicate in (False, True):
            collected = selection.ColdGraphSelection(
                db, generation_row(db, generation), account_ref(ACCOUNT), lambda: None)
            collected._begin_graph(db, generation, account_ref(ACCOUNT))
            collected.collect('node', row, row['node_id'], row['content_id'])
            if duplicate:
                collected.collect('node', row, row['node_id'], row['content_id'])
            assert not collected.seal()
            assert collected.discard_reason == ('duplicate_identity' if duplicate else 'graph_incomplete')
            assert collected.rows == collected.retained_bytes == 0
            collected.finish()
        db.rollback()


def test_bound_selection_refuses_wrong_scope_and_never_returns_partial_content(candidate):
    fixture, generation = candidate
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        row = first_node(db, generation)
        collected, generation_record = ready_selection(db, generation)
        assert collected.matches(db, generation_record, account_ref(ACCOUNT))
        assert not collected.matches(object(), generation_record, account_ref(ACCOUNT))
        assert not collected.matches(db, generation_record, account_ref('other-account'))
        wrong_generation = dict(generation_record, canonical_revision=-1)
        assert not collected.matches(db, wrong_generation, account_ref(ACCOUNT))
        assert collected.selected('node', row['node_id'][3:5], [row['node_id']]) == {
            row['node_id']: row['content_id']}
        missing = 'g1:' + '0' * 64
        assert shared_graph.selected_content_ids(db, generation, account_ref(ACCOUNT),
                                                'node', [missing], page_layout=True) == {}
        assert collected.selected('node', None, [row['node_id'], missing]) is None
        assert collected.discard_reason == 'coverage_missing'
        assert collected.rows == collected.retained_bytes == 0
        collected.finish()
        db.rollback()


def test_one_shot_request_is_refused_without_consuming_fallback_input(candidate):
    fixture, generation = candidate
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        row = first_node(db, generation)
        collected, _ = ready_selection(db, generation)
        identities = (identity for identity in [row['node_id']])
        assert collected.selected('node', None, identities) is None
        assert list(identities) == [row['node_id']]
        assert collected.rows == collected.retained_bytes == 0
        collected.finish()
        db.rollback()


@pytest.mark.parametrize('replacement', ['rollback', 'commit'])
@pytest.mark.parametrize('fallback', [False, True])
def test_replaced_transaction_rejects_even_when_all_snapshot_counters_match(
        candidate, monkeypatch, replacement, fallback):
    fixture, generation = candidate
    if fallback:
        monkeypatch.setattr(selection, 'MAX_COLD_SELECTION_ROWS', 0)
    captured = capture_collectors(monkeypatch)
    original = integrity.verify_generation_integrity

    def replace_transaction(db, record, *args, **kwargs):
        result = original(db, record, *args, **kwargs)
        before = (endpoints._snapshot(db), generation_binding(generation_row(db, generation)))
        getattr(db, replacement)()
        db.execute('BEGIN')
        assert (endpoints._snapshot(db), generation_binding(generation_row(db, generation))) == before
        return result

    monkeypatch.setattr(integrity, 'verify_generation_integrity', replace_transaction)
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        try:
            with pytest.raises(GraphReferentialIntegrityError, match='cold_selection_snapshot_changed'):
                recompute_generation(db, generation, materialize_projection=False)
            assert captured and all(item.rows == item.retained_bytes == 0 for item in captured)
        finally:
            db.rollback()


def test_snapshot_write_invalidates_admitted_selection(candidate):
    fixture, generation = candidate
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        collected, row = ready_selection(db, generation)
        db.execute('UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1')
        try:
            with pytest.raises(GraphReferentialIntegrityError, match='cold_selection_snapshot_changed'):
                try:
                    collected.matches(db, row, account_ref(ACCOUNT))
                finally:
                    collected.close()
            assert collected.rows == collected.retained_bytes == 0
        finally:
            db.rollback()


def test_primary_cancellation_survives_missing_sentinel_cleanup(candidate, monkeypatch):
    fixture, generation = candidate
    cancelled = asyncio.CancelledError('selected-content consumer cancelled')
    captured = capture_collectors(monkeypatch)

    def cancel(db, *args, **kwargs):
        db.rollback()
        db.execute('BEGIN')
        raise cancelled

    monkeypatch.setattr(integrity, 'verify_generation_integrity', cancel)
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        try:
            with pytest.raises(asyncio.CancelledError) as caught:
                recompute_generation(db, generation, materialize_projection=False)
            assert caught.value is cancelled
            assert captured and all(not item.ready and item.rows == item.retained_bytes == 0
                                    for item in captured)
        finally:
            db.rollback()


@pytest.mark.parametrize('phase', ['collection', 'lookup'])
def test_cancellation_inside_walk_or_lookup_releases_mapping(candidate, monkeypatch, phase):
    fixture, generation = candidate
    cancelled = asyncio.CancelledError('bounded selected-content cancellation')
    captured = capture_collectors(monkeypatch)
    lookup = {'active': False, 'checks': 0}
    original = selection.ColdGraphSelection.selected

    def selected(self, *args):
        lookup['active'] = True
        try:
            return original(self, *args)
        finally:
            lookup['active'] = False

    monkeypatch.setattr(selection.ColdGraphSelection, 'selected', selected)

    def check():
        if phase == 'collection' and captured and captured[0].rows >= 2:
            assert not captured[0].ready
            raise cancelled
        if phase == 'lookup' and lookup['active']:
            lookup['checks'] += 1
            if lookup['checks'] == 2:
                assert captured[0].ready and captured[0].rows > 0
                raise cancelled

    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        try:
            with pytest.raises(asyncio.CancelledError) as caught:
                recompute_generation(db, generation, materialize_projection=False, check=check)
            assert caught.value is cancelled
            assert len(captured) == 1
            assert captured[0].peak_rows >= 2
            assert not captured[0].ready
            assert captured[0].rows == captured[0].retained_bytes == 0
            assert db.in_transaction
        finally:
            db.rollback()


@pytest.mark.parametrize('subclass', [False, True])
def test_consumer_refuses_fabricated_selection_capability(candidate, monkeypatch, subclass):
    fixture, generation = candidate
    base = selection.ColdGraphSelection if subclass else object

    class FakeSelection(base):
        def matches(self, *args):
            raise AssertionError('untrusted capability was consulted')

        def selected(self, *args):
            raise AssertionError('untrusted mapping was consulted')

    fake = object.__new__(FakeSelection)
    sql = count_sql(monkeypatch)
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        verified = integrity.verify_generation_integrity(
            db, generation_row(db, generation), account_ref(ACCOUNT), _selection=fake)
        assert verified.headers and sql
        db.rollback()


@pytest.mark.parametrize('fault', ['missing_node', 'node_version', 'account', 'generation',
                                  'missing_edge', 'edge_version'])
def test_corrupt_exact_content_rejects_with_reuse_and_forced_sql(candidate, monkeypatch, fault):
    fixture, generation = candidate
    with fixture.stores.database.read() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        try:
            damage(db, generation, fault, 'source_id')
            db.commit()
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN')
            assert content_stamp(db) is not None
            for fallback in (False, True):
                if fallback:
                    monkeypatch.setattr(selection, 'MAX_COLD_SELECTION_ROWS', 0)
                with pytest.raises(GraphReferentialIntegrityError, match='graph_endpoint_absent'):
                    recompute_generation(db, generation, materialize_projection=False)
        finally:
            db.rollback()
    assert fixture.repositories.projection_activation.get(generation) is None


def test_page_bucket_mismatch_retains_original_sql_rejection(candidate, monkeypatch):
    """A real FK-consistent page relocation cannot certify excluded membership."""
    fixture, generation = candidate
    captured = capture_collectors(monkeypatch)
    sql = count_sql(monkeypatch)
    with fixture.stores.database.read() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        try:
            page = db.execute("""SELECT p.* FROM generation_graph_segments m
                JOIN graph_segment_membership_pages p USING(creator_account_id,segment_id)
                JOIN graph_membership_nodes r USING(creator_account_id,page_id)
                JOIN graph_node_content c USING(creator_account_id,content_id,node_id)
                WHERE m.generation_id=? AND m.creator_account_id=? AND m.kind='node'
                    AND c.kind='message' AND (
                        SELECT count(*) FROM graph_segment_membership_pages other
                        WHERE other.creator_account_id=p.creator_account_id
                            AND other.segment_id=p.segment_id)=1 LIMIT 1""",
                (generation, account_ref(ACCOUNT))).fetchone()
            assert page is not None
            bucket = page['bucket'][:2] + ('1' if page['bucket'][-1] == '0' else '0')
            guards = []
            for name in ('graph_membership_pages_immutable', 'graph_membership_selection_immutable'):
                ddl = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                                 (name,)).fetchone()[0]
                guards.append((name, ddl))
                db.execute('DROP TRIGGER ' + name)
            db.execute('UPDATE graph_membership_pages SET bucket=? '
                       'WHERE creator_account_id=? AND page_id=?',
                       (bucket, page['creator_account_id'], page['page_id']))
            db.execute('UPDATE graph_segment_membership_pages SET bucket=? '
                       'WHERE creator_account_id=? AND page_id=?',
                       (bucket, page['creator_account_id'], page['page_id']))
            for name, ddl in guards:
                db.execute(ddl)
                assert db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                                  (name,)).fetchone()[0] == ddl
            db.commit()
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN')
            assert db.execute('PRAGMA foreign_key_check').fetchall() == []
            assert content_stamp(db) is not None
            for fallback in (False, True):
                if fallback:
                    monkeypatch.setattr(selection, 'MAX_COLD_SELECTION_ROWS', 0)
                with pytest.raises(ValueError, match='conversation_integrity_selected_content_changed'):
                    recompute_generation(db, generation, materialize_projection=False)
            assert sql
            assert len(captured) == 2
            assert captured[0].discard_reason == 'row_ineligible'
            assert captured[1].discard_reason == 'capacity'
            assert all(item.rows == item.retained_bytes == 0 for item in captured)
        finally:
            db.rollback()
    assert fixture.repositories.projection_activation.get(generation) is None


def test_caller_savepoint_and_outer_transaction_survive_success(candidate):
    fixture, generation = candidate
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        db.execute('SAVEPOINT selected_content_caller')
        changes = db.total_changes
        try:
            result = recompute_generation(db, generation, materialize_projection=False)
            assert result['node_count'] > 0 and result['edge_count'] > 0
            assert db.in_transaction and db.total_changes == changes
            # Both operations require the caller's original savepoint to remain.
            db.execute('ROLLBACK TO SAVEPOINT selected_content_caller')
            db.execute('RELEASE SAVEPOINT selected_content_caller')
            # Releasing the nested caller scope must leave its original BEGIN open.
            assert db.in_transaction and db.total_changes == changes
        finally:
            db.rollback()


def test_savepoint_authorizer_refusal_keeps_full_sql_and_original_policy(candidate, monkeypatch):
    from app.persistence import sqlite_api
    from sqlcipher3.dbapi2 import SQLITE_SAVEPOINT

    fixture, generation = candidate
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        try:
            with monkeypatch.context() as forced_sql:
                forced_sql.setattr(selection, 'MAX_COLD_SELECTION_ROWS', 0)
                expected = recompute_generation(db, generation, materialize_projection=False)
            captured = capture_collectors(monkeypatch)
            sql = count_sql(monkeypatch)
            savepoints = []

            def authorize(action, operation, name, *_unused):
                if action == SQLITE_SAVEPOINT:
                    savepoints.append((operation, name))
                    if operation == 'BEGIN':
                        return sqlite_api.SQLITE_DENY
                return sqlite_api.SQLITE_OK

            db.set_authorizer(authorize)
            try:
                actual = recompute_generation(db, generation, materialize_projection=False)
                assert stable_values(actual) == stable_values(expected)
                assert sql and all(page_layout for _, page_layout in sql)
                assert len(captured) == 1
                assert captured[0].discard_reason == 'savepoint_unavailable'
                assert not captured[0].ready
                assert captured[0].rows == captured[0].retained_bytes == 0
                assert len(savepoints) == 1 and savepoints[0][0] == 'BEGIN'
                assert savepoints[0][1].startswith('cold_selection_')
                assert db.in_transaction
                # The original callback still enforces its policy after fallback.
                with pytest.raises(sqlite_api.DatabaseError, match='not authorized'):
                    db.execute('SAVEPOINT selected_content_policy_probe')
                assert savepoints[-1] == ('BEGIN', 'selected_content_policy_probe')
                assert len(savepoints) == 2 and db.in_transaction
            finally:
                db.set_authorizer(None)
        finally:
            db.rollback()
