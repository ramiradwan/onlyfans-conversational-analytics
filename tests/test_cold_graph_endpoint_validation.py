"""Fused cold closure uses actual encrypted selected content and complete fallback."""

import asyncio

import pytest

from app.analytics import graph_endpoint_validation as endpoints, shared_graph
from app.analytics.graph_store import GraphReferentialIntegrityError
from app.analytics.opaque_refs import account_ref
from app.analytics.sqlite_projection_store import recompute_generation
from tests.continuous_analytics_fixture import ACCOUNT, advance, cleanup, make_fixture

pytestmark = [pytest.mark.ci_tier('integration')]


def values(db, generation):
    return recompute_generation(db, generation, materialize_projection=False)


def stable_values(value):
    return {name: value[name] for name in (
        'projection', 'projection_digest', 'graph_digest', 'node_count',
        'edge_count', 'graph_segments', 'enrichment_units', 'conversation_integrity',
    )}


@pytest.fixture
def first_candidate(tmp_path):
    fixture = make_fixture(tmp_path)
    candidate = fixture.pipeline.build_candidate(ACCOUNT)
    yield fixture, candidate.staged_generation_id
    cleanup(fixture)


def test_complete_cold_values_match_original_sql(first_candidate, monkeypatch):
    fixture, generation = first_candidate
    original = shared_graph._verify_all_shared_endpoints
    calls = []
    def counted(*args):
        calls.append(args[1:])
        return original(*args)
    monkeypatch.setattr(shared_graph, '_verify_all_shared_endpoints', counted)
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        actual = values(db, generation)
        assert calls == []
        monkeypatch.setattr(endpoints, 'MAX_COLD_ENDPOINT_ROWS', 0)
        expected = values(db, generation)
        assert calls == [(generation, account_ref(ACCOUNT))]
        assert stable_values(actual) == stable_values(expected)
        db.rollback()


@pytest.mark.parametrize('bound', ['MAX_COLD_ENDPOINT_ROWS', 'MAX_COLD_ENDPOINT_BYTES'])
def test_capacity_refusal_uses_complete_original_sql(first_candidate, monkeypatch, bound):
    fixture, generation = first_candidate
    calls = []
    original = shared_graph._verify_all_shared_endpoints
    def counted(*args):
        calls.append(args[1:])
        return original(*args)
    monkeypatch.setattr(shared_graph, '_verify_all_shared_endpoints', counted)
    monkeypatch.setattr(endpoints, bound, 1)
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        assert values(db, generation)['node_count'] > 1
        db.rollback()
    assert calls == [(generation, account_ref(ACCOUNT))]


@pytest.mark.parametrize('mode', ['autocommit', 'materialized'])
def test_ineligible_scope_keeps_original_sql(first_candidate, monkeypatch, mode):
    fixture, generation = first_candidate
    original = shared_graph._verify_all_shared_endpoints
    calls = []
    def counted(*args):
        calls.append(args[1:])
        return original(*args)
    monkeypatch.setattr(shared_graph, '_verify_all_shared_endpoints', counted)
    with fixture.stores.database.read() as db:
        if mode == 'materialized':
            db.execute('BEGIN')
        result = recompute_generation(db, generation,
            materialize_graph=mode == 'materialized', materialize_projection=True)
        assert result['node_count'] > 0
        if mode == 'materialized':
            assert len(result['nodes']) == result['node_count']
            db.rollback()
    assert calls == [(generation, account_ref(ACCOUNT))]


def selected_edge(db, generation):
    return db.execute("""SELECT r.*,c.source_id,c.target_id
        FROM generation_graph_segments m
        CROSS JOIN graph_segment_edges r USING(creator_account_id,segment_id)
        CROSS JOIN graph_edge_content c USING(creator_account_id,content_id,edge_id)
        WHERE m.generation_id=? AND m.creator_account_id=? AND m.kind='edge'
        LIMIT 1""", (generation, account_ref(ACCOUNT))).fetchone()


def _damage(db, generation, fault, endpoint):
    edge = selected_edge(db, generation)
    assert edge is not None
    account = account_ref(ACCOUNT)
    if fault in ('missing_edge', 'edge_version'):
        if fault == 'missing_edge':
            db.execute('DROP TRIGGER graph_edge_content_referenced')
            db.execute('DELETE FROM graph_edge_content WHERE creator_account_id=? AND content_id=?',
                       (account, edge['content_id']))
        else:
            other = db.execute('SELECT content_id FROM graph_edge_content WHERE creator_account_id=? AND edge_id!=? LIMIT 1',
                               (account, edge['edge_id'])).fetchone()[0]
            db.execute('DROP TRIGGER graph_membership_edges_immutable')
            db.execute('UPDATE graph_membership_edges SET content_id=? WHERE creator_account_id=? AND edge_id=?',
                       (other, account, edge['edge_id']))
        return edge['edge_id']
    node = db.execute("""SELECT r.* FROM generation_graph_segments m
        CROSS JOIN graph_segment_nodes r USING(creator_account_id,segment_id)
        WHERE m.generation_id=? AND m.creator_account_id=? AND m.kind='node' AND r.node_id=?""",
        (generation, account, edge[endpoint])).fetchone()
    assert node is not None
    if fault == 'missing_node':
        db.execute('DROP TRIGGER graph_node_content_referenced')
        db.execute('DELETE FROM graph_node_content WHERE creator_account_id=? AND content_id=?',
                   (account, node['content_id']))
    elif fault == 'node_version':
        other = db.execute('SELECT content_id FROM graph_node_content WHERE creator_account_id=? AND node_id!=? LIMIT 1',
                           (account, node['node_id'])).fetchone()[0]
        db.execute('DROP TRIGGER graph_membership_nodes_immutable')
        db.execute('UPDATE graph_membership_nodes SET content_id=? WHERE creator_account_id=? AND node_id=?',
                   (other, account, node['node_id']))
    elif fault == 'account':
        db.execute('DROP TRIGGER graph_node_content_immutable')
        db.execute('UPDATE graph_node_content SET creator_account_id=? WHERE creator_account_id=? AND content_id=?',
                   (account_ref('other-selected-content-account'), account, node['content_id']))
    else:
        assert fault == 'generation'
        db.execute('DROP TRIGGER generation_graph_segments_immutable')
        db.execute("UPDATE generation_graph_segments SET generation_id='other-selected-generation' WHERE generation_id=? AND creator_account_id=? AND kind='node' AND bucket=?",
                   (generation, account, node['node_id'][3:5]))
    return edge['edge_id']


def damage(db, generation, fault, endpoint):
    guard = {
        'missing_edge': 'graph_edge_content_referenced',
        'edge_version': 'graph_membership_edges_immutable',
        'missing_node': 'graph_node_content_referenced',
        'node_version': 'graph_membership_nodes_immutable',
        'account': 'graph_node_content_immutable',
        'generation': 'generation_graph_segments_immutable',
    }[fault]
    original = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (guard,)).fetchone()
    assert original is not None and original[0]
    result = _damage(db, generation, fault, endpoint)
    assert db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (guard,)).fetchone() is None
    db.execute(original[0])
    assert db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (guard,)).fetchone()[0] == original[0]
    return result


@pytest.mark.parametrize('endpoint', ['source_id', 'target_id'])
@pytest.mark.parametrize('fault', ['missing_node', 'node_version', 'account', 'generation', 'missing_edge', 'edge_version'])
@pytest.mark.parametrize('fallback', [False, True])
def test_exact_content_faults_reject_in_both_paths(first_candidate, monkeypatch, endpoint, fault, fallback):
    fixture, generation = first_candidate
    if fallback:
        monkeypatch.setattr(endpoints, 'MAX_COLD_ENDPOINT_ROWS', 0)
    calls = []
    original = shared_graph._verify_all_shared_endpoints
    def counted(*args):
        calls.append(args[1:])
        return original(*args)
    monkeypatch.setattr(shared_graph, '_verify_all_shared_endpoints', counted)
    with fixture.stores.database.read() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        try:
            damage(db, generation, fault, endpoint)
            # The owned fixture stays damaged, while its exact guard catalog and
            # real FK-enabled validation snapshot make fusion eligible.
            db.commit()
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN')
            from app.analytics.validation_receipt import content_stamp
            assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
            assert content_stamp(db) is not None
            if fault in ('missing_edge', 'edge_version'):
                # Actual missing exact content creates the LEFT-joined NULL
                # endpoint row; the NOT NULL stored-column contract is unchanged.
                cursor = shared_graph._ordered_edges_with_missing_content(db, generation, account_ref(ACCOUNT))
                try:
                    assert any(row['edge_id'] is None and row['source_id'] is None
                        and row['target_id'] is None and row['content_id'] is None for row in cursor)
                finally:
                    cursor.close()
            with pytest.raises(GraphReferentialIntegrityError, match='graph_endpoint_absent'):
                values(db, generation)
        finally:
            db.rollback()
    assert calls == ([(generation, account_ref(ACCOUNT))] if fallback else [])
    assert fixture.repositories.projection_activation.get(generation) is None


@pytest.mark.parametrize('endpoint', ['source_id', 'target_id'])
def test_retained_predecessor_node_cannot_satisfy_new_generation(tmp_path, monkeypatch, endpoint):
    fixture = make_fixture(tmp_path)
    try:
        old = fixture.pipeline.project_account(ACCOUNT)
        before = fixture.stores.database.active_generation(ACCOUNT).generation_id
        absent = getattr(old.artifact.edges[0], endpoint)
        fixture.stores.projections._conversation_graph_proofs.clear()
        original = shared_graph._records
        def omit(graph, plan, check):
            for value, member in original(graph, plan, check):
                if plan.kind != 'node' or member[2] != absent:
                    yield value, member
        monkeypatch.setattr(shared_graph, '_predecessor_segments', lambda *args: {})
        monkeypatch.setattr(shared_graph, '_records', omit)
        with fixture.repositories.database.transaction() as db:
            advance(db)
        with pytest.raises(GraphReferentialIntegrityError, match='graph_endpoint_absent'):
            fixture.pipeline.build_candidate(ACCOUNT)
        assert fixture.stores.database.active_generation(ACCOUNT).generation_id == before
    finally:
        cleanup(fixture)


def test_cancellation_during_set_admission_preserves_primary(first_candidate):
    fixture, generation = first_candidate
    primary = asyncio.CancelledError('synthetic endpoint cancellation')
    checks = 0
    def cancelled():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise primary
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        with pytest.raises(asyncio.CancelledError) as failure:
            endpoints.verify_cold_graph_rows(db, generation, account_ref(ACCOUNT), cancelled)
        assert failure.value is primary
        assert db.in_transaction
        assert values(db, generation)['node_count'] > 0
        db.rollback()


def test_cancellation_during_edge_walk_preserves_primary(first_candidate, monkeypatch):
    from app.analytics import graph_verification
    fixture, generation = first_candidate
    primary = asyncio.CancelledError('synthetic edge-walk cancellation')
    original = graph_verification.edge_bytes
    calls = []
    def cancelled(*args):
        calls.append(True)
        raise primary
    monkeypatch.setattr(graph_verification, 'edge_bytes', cancelled)
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        with pytest.raises(asyncio.CancelledError) as failure:
            values(db, generation)
        assert failure.value is primary and calls
        assert db.in_transaction
        monkeypatch.setattr(graph_verification, 'edge_bytes', original)
        assert values(db, generation)['edge_count'] > 0
        db.rollback()


def test_cancellation_on_sql_fallback_preserves_primary(first_candidate, monkeypatch):
    fixture, generation = first_candidate
    primary = asyncio.CancelledError('synthetic SQL-fallback cancellation')
    calls = []
    monkeypatch.setattr(endpoints, 'MAX_COLD_ENDPOINT_ROWS', 0)
    def cancelled(*args):
        calls.append(True)
        raise primary
    monkeypatch.setattr(shared_graph, '_verify_all_shared_endpoints', cancelled)
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        with pytest.raises(asyncio.CancelledError) as failure:
            values(db, generation)
        assert failure.value is primary and calls == [True]
        db.rollback()


def test_replaced_transaction_with_external_epoch_change_is_rejected(first_candidate):
    fixture, generation = first_candidate
    checks = 0
    changed = False
    with fixture.stores.database.read() as db:
        db.execute('BEGIN')
        from app.analytics.validation_receipt import content_stamp
        before = content_stamp(db)
        assert before is not None
        cursor = db.execute("""SELECT COUNT(*) FROM generation_graph_segments m
            CROSS JOIN graph_segment_nodes r USING(creator_account_id,segment_id)
            CROSS JOIN graph_node_content c USING(creator_account_id,content_id,node_id)
            WHERE m.generation_id=? AND m.creator_account_id=? AND m.kind='node'""",
            (generation, account_ref(ACCOUNT)))
        try:
            selected_count = cursor.fetchone()[0]
        finally:
            cursor.close()
        assert selected_count > 0
        def replace_transaction():
            nonlocal checks, changed
            checks += 1
            # Replace the snapshot after admission closes its cursor. Replacing
            # it while fetching already fails closed in the native driver.
            if checks == selected_count + 2:
                db.rollback()
                with fixture.stores.database.transaction() as writer:
                    writer.execute('UPDATE generation_content_epoch SET value=value+1 WHERE singleton=1')
                db.execute('BEGIN')
                after = content_stamp(db)
                assert after is not None and after[:-1] == before[:-1]
                assert after[-1] == before[-1] + 1 and after != before
                changed = True
        with pytest.raises(GraphReferentialIntegrityError, match='graph_endpoint_snapshot_changed'):
            endpoints.verify_cold_graph_rows(db, generation, account_ref(ACCOUNT), replace_transaction)
        assert changed
        db.rollback()


@pytest.mark.parametrize('fault,reason', [('segment', 'graph_segment_invalid'),
    ('conversation_unit', 'conversation_graph_reference_absent')])
def test_all_original_metadata_checks_remain(first_candidate, monkeypatch, fault, reason):
    fixture, generation = first_candidate
    calls = []
    original_sql = shared_graph._verify_all_shared_endpoints
    def counted(*args):
        calls.append(True)
        return original_sql(*args)
    monkeypatch.setattr(shared_graph, '_verify_all_shared_endpoints', counted)
    with fixture.stores.database.read() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        try:
            guard = ('graph_segments_immutable' if fault == 'segment'
                else 'conversation_graph_units_referenced')
            guard_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (guard,)).fetchone()[0]
            assert guard_sql
            if fault == 'segment':
                db.execute('DROP TRIGGER graph_segments_immutable')
                db.execute("UPDATE graph_segments SET kind=CASE kind WHEN 'node' THEN 'edge' ELSE 'node' END WHERE segment_id=(SELECT segment_id FROM generation_graph_segments WHERE generation_id=? LIMIT 1)", (generation,))
            else:
                unit = db.execute('SELECT unit_id FROM conversation_graph_refs WHERE generation_id=? LIMIT 1', (generation,)).fetchone()
                assert unit is not None
                db.execute('DROP TRIGGER conversation_graph_units_referenced')
                db.execute('DELETE FROM conversation_graph_units WHERE creator_account_id=? AND unit_id=?', (account_ref(ACCOUNT), unit[0]))
            db.execute(guard_sql)
            db.commit()
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN')
            from app.analytics.validation_receipt import content_stamp
            assert content_stamp(db) is not None
            with pytest.raises(GraphReferentialIntegrityError, match=reason):
                values(db, generation)
            assert calls == []
        finally:
            db.rollback()


def test_ordinary_revalidation_and_publication_refuse_missing_exact_edge(tmp_path):
    fixture = make_fixture(tmp_path)
    try:
        candidate = fixture.pipeline.build_candidate(ACCOUNT)
        generation = candidate.staged_generation_id
        with fixture.stores.database.read() as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            damage(db, generation, 'missing_edge', 'source_id')
            db.commit()
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN')
            from app.analytics.validation_receipt import content_stamp
            assert content_stamp(db) is not None
            db.rollback()
        with pytest.raises(GraphReferentialIntegrityError, match='graph_endpoint_absent'):
            fixture.stores.projections._validate_persisted_generation(
                generation, materialize_projection=False)
        with pytest.raises(GraphReferentialIntegrityError, match='graph_endpoint_absent'):
            fixture.pipeline.publish_candidate(candidate)
        cancelled = fixture.repositories.projection_activation.get(generation)
        assert cancelled is not None and cancelled.state == 'cancelled'
        assert fixture.stores.projections._generation(generation)['status'] == 'retired'
        assert fixture.stores.database.active_generation(ACCOUNT) is None
    finally:
        cleanup(fixture)
