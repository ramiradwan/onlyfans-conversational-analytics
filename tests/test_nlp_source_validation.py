"""Bounded live source admission and durable partial inventory validation."""

from contextlib import ExitStack, contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from app.persistence.migrations import load_migration_catalog
from app.analytics import nlp_findings_store, nlp_processing
from app.analytics.errors import CanonicalRevisionChanged, ProjectionUnavailable
from app.analytics.evidence_contracts import MAX_EVIDENCE_TEXT_CHARS
from app.analytics.identity import source_identity
from app.analytics.nlp_findings_store import FindingsStore, checked_snapshot
from app.analytics.query_execution import (
    QuestionBudget, QuestionCancelled, QuestionLimitExceeded, QuestionLimits,
)
from tests.continuous_analytics_fixture import ACCOUNT, advance, cleanup, make_fixture
from tests.test_nlp_processing_publication import runner

CURRENT_SCHEMA_VERSION = load_migration_catalog(
    Path(__file__).resolve().parents[1] / 'app/analytics/sql'
)[-1].version

pytestmark = pytest.mark.ci_tier('integration')


@pytest.fixture
def fixture(tmp_path):
    value = make_fixture(tmp_path, conversations=1, messages=130)
    yield value
    cleanup(value)


def admitted(fixture):
    value, _, _ = runner(fixture)
    assert value.reconcile(ACCOUNT)
    return value


def test_background_counter_accepts_exact_source_capacity_without_changing_questions():
    limits = nlp_processing._PublicationLimits()
    assert limits.max_records == 200_004
    assert limits.wall_clock_ms == 30_000
    budget = QuestionBudget(limits)
    budget.consume(2)
    budget.consume(2 * 100_000)
    budget.consume(2)
    with pytest.raises(QuestionLimitExceeded):
        budget.consume()
    with pytest.raises(ValueError, match='question_record_limit_invalid'):
        QuestionLimits(max_records=100_001)
    with pytest.raises(ValueError, match='question_time_limit_invalid'):
        QuestionLimits(wall_clock_ms=30_001)


def test_inventory_admission_reuses_one_canonical_connection_and_bounded_pages(fixture, monkeypatch):
    value = admitted(fixture)
    original = fixture.repositories.database.read
    connections = []
    @contextmanager
    def read():
        with original() as connection:
            connections.append(connection)
            yield connection
    monkeypatch.setattr(fixture.repositories.database, 'read', read)
    derived_read = fixture.stores.database.read
    pages = []
    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def fetchmany(self, size):
            assert 0 < size <= 64
            result = self.cursor.fetchmany(size)
            pages.append(len(result))
            return result
    class Connection:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, sql, *args):
            cursor = self.connection.execute(sql, *args)
            return Cursor(cursor) if sql.startswith('SELECT * FROM nlp_sources ') else cursor
    @contextmanager
    def derived():
        with derived_read() as connection:
            yield Connection(connection)
    monkeypatch.setattr(fixture.stores.database, 'read', derived)
    original_read = value._read
    calls = []
    def evidence(account, location, budget):
        calls.append((budget, fixture.source._question_scope_local.connection))
        return original_read(account, location, budget)
    monkeypatch.setattr(value, '_read', evidence)
    canonical, due = value._admit_inventory(ACCOUNT)
    assert len(connections) == 1
    assert len(calls) == 130
    assert pages == [64, 64, 2, 0]
    assert all(connection is connections[0] for _, connection in calls)
    assert len({id(budget) for budget, _ in calls}) == 1
    assert calls[0][0].records_examined == 2 * 130 + 4
    assert canonical.revision == 1 and due is not None
    assert not hasattr(fixture.source._question_scope_local, 'connection')


@pytest.mark.parametrize('failure', ['deadline', 'cancel'])
def test_inventory_pages_share_deadline_and_cancellation(fixture, monkeypatch, failure):
    value = admitted(fixture)
    now, cancelled = [0.0], [False]
    budget = QuestionBudget(nlp_processing._PublicationLimits(), monotonic=lambda: now[0],
                            cancellation_check=lambda: cancelled[0])
    monkeypatch.setattr(nlp_processing, 'QuestionBudget', lambda *args, **kwargs: budget)
    original = value._read
    count = [0]
    def read(*args):
        message = original(*args)
        count[0] += 1
        if count[0] % 64 == 0:
            if failure == 'deadline':
                now[0] += 16
            else:
                cancelled[0] = True
        return message
    monkeypatch.setattr(value, '_read', read)
    with pytest.raises(QuestionLimitExceeded if failure == 'deadline' else QuestionCancelled):
        value._admit_inventory(ACCOUNT)
    assert count[0] == (128 if failure == 'deadline' else 64)
    assert not hasattr(fixture.source._question_scope_local, 'connection')


@pytest.mark.parametrize('change', ['text', 'delete', 'speaker'])
def test_source_changed_after_its_read_cannot_finish_admission(fixture, monkeypatch, change):
    value = admitted(fixture)
    original = value._read
    changed = [False]
    def read(account, location, budget):
        message = original(account, location, budget)
        if not changed[0]:
            changed[0] = True
            with fixture.repositories.database.transaction() as db:
                field = {'text': "text='Changed'", 'delete': 'is_deleted=1',
                         'speaker': "sender_platform_user_id='other'"}[change]
                db.execute('UPDATE account_messages SET '+field+' WHERE creator_account_id=? AND message_id=?',
                           (ACCOUNT, location.message_id))
        return message
    monkeypatch.setattr(value, '_read', read)
    with pytest.raises(ProjectionUnavailable):
        value._admit_inventory(ACCOUNT)


def test_final_account_token_check_is_fresh(fixture, monkeypatch):
    value = admitted(fixture)
    token = fixture.source._identity_cache.token
    calls = [0]
    def current(connection, account):
        calls[0] += 1
        # Preparation, scope entry, one per evidence read, then final recheck.
        if calls[0] == 133:
            with fixture.repositories.database.transaction() as db:
                db.execute("UPDATE account_messages SET text='Changed' WHERE creator_account_id=? AND message_id='m-0-0'", (ACCOUNT,))
        return token(connection, account)
    monkeypatch.setattr(fixture.source._identity_cache, 'token', current)
    with pytest.raises(ProjectionUnavailable):
        value._admit_inventory(ACCOUNT)
    assert calls[0] == 133


@pytest.mark.parametrize('cache_state', ['clear', 'expired', 'evicted'])
def test_publication_prepares_current_identity_after_cache_loss(tmp_path, monkeypatch, cache_state):
    from app.analytics import source_snapshot
    from app.analytics.source_tokens import MAX_IDENTITIES
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        now = [0.0]
        fixture.source._identity_cache._clock = lambda: now[0]
        value = admitted(fixture)
        if cache_state == 'clear':
            fixture.source._identity_cache.clear()
        elif cache_state == 'expired':
            now[0] = 61
        else:
            for number in range(MAX_IDENTITIES):
                other = f'other-{number}'
                with fixture.repositories.database.transaction() as db:
                    db.execute('INSERT INTO account_heads(creator_account_id,canonical_revision,updated_at) VALUES (?,1,?)',
                               (other, fixture.clock.now.isoformat()))
                fixture.source.prepare_question_identity(other)
        original = source_snapshot.scan_identity
        scans = []
        def scan(*args, **kwargs):
            scans.append(args[1])
            return original(*args, **kwargs)
        monkeypatch.setattr(source_snapshot, 'scan_identity', scan)
        value._publication_admission(ACCOUNT)
        assert scans == [ACCOUNT]
        assert not hasattr(fixture.source._question_scope_local, 'connection')
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('change', ['clear', 'mutate'])
def test_scope_uses_exact_prepared_proof_independently_of_cache(tmp_path, monkeypatch, change):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        value = admitted(fixture)
        original = fixture.source.prepare_question_identity
        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            if change == 'clear':
                fixture.source._identity_cache.clear()
            else:
                with fixture.repositories.database.transaction() as db:
                    db.execute("UPDATE account_messages SET text='Changed' WHERE creator_account_id=?", (ACCOUNT,))
            return result
        monkeypatch.setattr(fixture.source, 'prepare_question_identity', prepare)
        if change == 'clear':
            value._publication_admission(ACCOUNT)
        else:
            with pytest.raises(ProjectionUnavailable):
                value._publication_admission(ACCOUNT)
        assert not hasattr(fixture.source._question_scope_local, 'connection')
    finally:
        cleanup(fixture)


def test_changed_token_cannot_replay_old_preparation_proof(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        value = admitted(fixture)
        proof = fixture.source.prepare_question_identity(ACCOUNT, return_proof=True)
        with fixture.repositories.database.transaction() as db:
            db.execute("UPDATE account_messages SET text='Changed' WHERE creator_account_id=?", (ACCOUNT,))
        monkeypatch.setattr(fixture.source, 'prepare_question_identity', lambda *args, **kwargs: proof)
        with pytest.raises(ProjectionUnavailable):
            value._publication_admission(ACCOUNT)
        assert not hasattr(fixture.source._question_scope_local, 'connection')
    finally:
        cleanup(fixture)


def test_mutation_during_preparation_cannot_issue_a_proof(tmp_path, monkeypatch):
    from app.analytics import source_snapshot
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        value = admitted(fixture)
        fixture.source._identity_cache.clear()
        original = source_snapshot.scan_identity
        def scan(*args, **kwargs):
            result = original(*args, **kwargs)
            with fixture.repositories.database.transaction() as db:
                db.execute("UPDATE account_messages SET text='Changed' WHERE creator_account_id=?", (ACCOUNT,))
            return result
        monkeypatch.setattr(source_snapshot, 'scan_identity', scan)
        with pytest.raises(CanonicalRevisionChanged):
            value._publication_admission(ACCOUNT)
        assert not hasattr(fixture.source._question_scope_local, 'connection')
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('failure', ['deadline', 'cancel'])
def test_preparation_uses_the_publication_deadline_and_cancellation(tmp_path, monkeypatch, failure):
    from app.analytics import source_snapshot
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        value = admitted(fixture)
        fixture.source._identity_cache.clear()
        now, cancelled = [0.0], [False]
        budget = QuestionBudget(nlp_processing._PublicationLimits(), monotonic=lambda: now[0],
                                cancellation_check=lambda: cancelled[0])
        monkeypatch.setattr(nlp_processing, 'QuestionBudget', lambda *args, **kwargs: budget)
        original = source_snapshot.scan_identity
        def scan(*args, **kwargs):
            if failure == 'deadline':
                now[0] = 31
            else:
                cancelled[0] = True
            return original(*args, **kwargs)
        monkeypatch.setattr(source_snapshot, 'scan_identity', scan)
        with pytest.raises(QuestionLimitExceeded if failure == 'deadline' else QuestionCancelled):
            value._publication_admission(ACCOUNT)
        assert not hasattr(fixture.source._question_scope_local, 'connection')
        assert not fixture.source._identity_cache._entries
    finally:
        cleanup(fixture)


def test_preparation_does_not_reset_deadline_before_source_validation(fixture, monkeypatch):
    from app.analytics import source_snapshot
    value = admitted(fixture)
    fixture.source._identity_cache.clear()
    now = [0.0]
    budget = QuestionBudget(nlp_processing._PublicationLimits(), monotonic=lambda: now[0])
    created = []
    def new_budget(*args, **kwargs):
        created.append(True)
        return budget
    monkeypatch.setattr(nlp_processing, 'QuestionBudget', new_budget)
    original_scan = source_snapshot.scan_identity
    def scan(*args, **kwargs):
        result = original_scan(*args, **kwargs)
        now[0] = 20
        return result
    monkeypatch.setattr(source_snapshot, 'scan_identity', scan)
    original_read = value._read
    reads = [0]
    def read(*args):
        result = original_read(*args)
        reads[0] += 1
        if reads[0] == 64:
            now[0] += 16
        return result
    monkeypatch.setattr(value, '_read', read)
    with pytest.raises(QuestionLimitExceeded):
        value._publication_admission(ACCOUNT)
    assert reads[0] == 64 and len(created) == 1


@pytest.mark.parametrize('borrowed', [False, True])
def test_prepared_scope_keeps_outer_sql_interruption_and_closes_handles(tmp_path, borrowed):
    from app.analytics.question_connections import QuestionConnectionPool
    from app.persistence import sqlite_api
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    pool = QuestionConnectionPool()
    try:
        admitted(fixture)
        fixture.source._identity_cache.clear()
        cancelled = [False]
        budget = QuestionBudget(nlp_processing._PublicationLimits(), cancellation_check=lambda: cancelled[0])
        interrupted = []
        with pytest.raises(QuestionCancelled):
            with ExitStack() as owned:
                if borrowed:
                    canonical, _ = owned.enter_context(pool.borrow(
                        fixture.source, fixture.stores.projections, ACCOUNT, budget))
                    owned.enter_context(fixture.source.use_question_connection(canonical))
                scope = owned.enter_context(fixture.source.open_question_scope(
                    ACCOUNT, budget, prepare_identity=True, verify_source_token=True))
                def trace(sql):
                    if sql.startswith('WITH RECURSIVE'):
                        cancelled[0] = True
                scope.connection.set_trace_callback(trace)
                try:
                    scope.connection.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n').fetchone()
                except sqlite_api.OperationalError as error:
                    interrupted.append(str(error))
                    raise
        assert interrupted  # The native SQL progress handler survived preparation.
        assert not hasattr(fixture.source._question_scope_local, 'connection')
        with pytest.raises(sqlite_api.ProgrammingError):
            scope.connection.execute('SELECT 1')
    finally:
        pool.close()
        assert pool.wait_closed(1)
        cleanup(fixture)


@pytest.mark.parametrize('guard', [
    'message_delete', 'chat_delete', 'tombstone', 'participant_scope',
    'barrier_account', 'barrier_conversation', 'barrier_message', 'barrier_participant',
    'text_limit', 'source_version', 'speaker',
])
def test_bulk_admission_preserves_live_source_predicates(tmp_path, guard):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        value = admitted(fixture)
        with fixture.repositories.database.transaction() as db:
            if guard == 'message_delete':
                db.execute('UPDATE account_messages SET is_deleted=1 WHERE creator_account_id=?', (ACCOUNT,))
            elif guard == 'chat_delete':
                db.execute('UPDATE account_chats SET is_deleted=1 WHERE creator_account_id=?', (ACCOUNT,))
            elif guard == 'tombstone':
                db.execute("INSERT INTO entity_tombstones(creator_account_id,entity_kind,entity_id,chat_id,stream_epoch,source_seq,deleted_at) VALUES (?,'message','m-0-0','chat-0',1,2,?)",
                           (ACCOUNT, fixture.clock.now.isoformat()))
            elif guard == 'participant_scope':
                db.execute("INSERT INTO participant_deletion_chat_scopes VALUES (?,'synthetic-fan','chat-0',1,?)",
                           (ACCOUNT, fixture.clock.now.isoformat()))
            elif guard.startswith('barrier_'):
                scope = guard.removeprefix('barrier_')
                key = {'account': '*', 'conversation': 'chat-0', 'message': 'm-0-0', 'participant': 'synthetic-fan'}[scope]
                db.execute("INSERT INTO deletion_barriers(creator_account_id,scope_kind,scope_key,deletion_revision,deleted_at,provenance) VALUES (?,?,?,1,?,'example')",
                           (ACCOUNT, scope, key, fixture.clock.now.isoformat()))
            elif guard == 'text_limit':
                db.execute('UPDATE account_messages SET text=? WHERE creator_account_id=?',
                           ('x' * (MAX_EVIDENCE_TEXT_CHARS + 1), ACCOUNT))
            elif guard == 'source_version':
                db.execute("UPDATE account_messages SET text='Changed' WHERE creator_account_id=?", (ACCOUNT,))
        if guard == 'speaker':
            with fixture.stores.database.transaction() as db:
                db.execute("UPDATE nlp_sources SET speaker_ref='other'")
        source_identity(fixture.source, ACCOUNT)
        with pytest.raises(ValueError, match='nlp_source_invalid'):
            value._admit_inventory(ACCOUNT)
    finally:
        cleanup(fixture)


def test_expiry_after_last_read_is_rechecked(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        value = admitted(fixture)
        original = value._read
        def read(*args):
            message = original(*args)
            fixture.clock.now += timedelta(days=100)
            return message
        monkeypatch.setattr(value, '_read', read)
        with pytest.raises(ValueError, match='nlp_source_expired'):
            value._admit_inventory(ACCOUNT)
    finally:
        cleanup(fixture)


def test_reconciliation_reuses_connection_for_locators_and_evidence(fixture, monkeypatch):
    value, _, _ = runner(fixture)
    source_identity(fixture.source, ACCOUNT)
    original = fixture.repositories.database.read
    connections = []
    @contextmanager
    def read():
        with original() as connection:
            connections.append(connection)
            yield connection
    monkeypatch.setattr(fixture.repositories.database, 'read', read)
    assert not value.reconcile(ACCOUNT, maximum=128)
    assert len(connections) == 1


@pytest.mark.parametrize('failure', ['deadline', 'cancel'])
def test_cold_reconciliation_preparation_is_bounded(tmp_path, monkeypatch, failure):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        from app.analytics import source_snapshot
        value, _, _ = runner(fixture)
        fixture.source._identity_cache.clear()
        now, cancelled = [0.0], [False]
        budget = QuestionBudget(QuestionLimits(max_records=100_000, wall_clock_ms=30_000),
                                monotonic=lambda: now[0], cancellation_check=lambda: cancelled[0])
        monkeypatch.setattr(value, '_budget', lambda: budget)
        def unbounded_read(*args, **kwargs):
            pytest.fail('reconciliation must prepare identity in its bounded scope')
        monkeypatch.setattr(fixture.source, 'read_identity', unbounded_read)
        original = source_snapshot.scan_identity
        def scan(*args, **kwargs):
            if failure == 'deadline':
                now[0] = 31
            else:
                cancelled[0] = True
            kwargs['check']()
            return original(*args, **kwargs)
        monkeypatch.setattr(source_snapshot, 'scan_identity', scan)
        with pytest.raises(QuestionLimitExceeded if failure == 'deadline' else QuestionCancelled):
            value.reconcile(ACCOUNT)
        assert fixture.source.connection is None
        assert getattr(fixture.source._question_scope_local, 'connection', None) is None
        assert not value.findings.state(ACCOUNT)['scan_complete']
    finally:
        cleanup(fixture)


def test_completed_reconciliation_prepares_cold_identity_under_same_budget(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        from app.analytics import source_snapshot
        value, _, _ = runner(fixture)
        assert value.reconcile(ACCOUNT)
        fixture.source._identity_cache.clear()
        calls = []
        original = source_snapshot.scan_identity
        def scan(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)
        monkeypatch.setattr(source_snapshot, 'scan_identity', scan)
        monkeypatch.setattr(fixture.source, 'read_identity', lambda *args, **kwargs: pytest.fail('unbounded identity read'))
        assert value.reconcile(ACCOUNT)
        assert calls == [True]
    finally:
        cleanup(fixture)


def test_reconciliation_charges_authorization_to_same_deadline(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        value, _, _ = runner(fixture)
        now = [0.0]
        budgets = []
        def make_budget():
            budget = QuestionBudget(QuestionLimits(max_records=100_000, wall_clock_ms=30_000),
                                    monotonic=lambda: now[0])
            budgets.append(budget)
            return budget
        monkeypatch.setattr(value, '_budget', make_budget)
        def authorize(account_id):
            assert len(budgets) == 1
            now[0] = 31
        monkeypatch.setattr(value, 'authorize', authorize)
        with pytest.raises(QuestionLimitExceeded):
            value.reconcile(ACCOUNT)
    finally:
        cleanup(fixture)


@pytest.mark.parametrize('already_complete', [False, True])
def test_reconciliation_includes_scope_cleanup_in_deadline(tmp_path, monkeypatch, already_complete):
    fixture = make_fixture(tmp_path, conversations=1, messages=1)
    try:
        value, _, _ = runner(fixture)
        if already_complete:
            assert value.reconcile(ACCOUNT)
        now = [0.0]
        budget = QuestionBudget(QuestionLimits(max_records=100_000, wall_clock_ms=30_000),
                                monotonic=lambda: now[0])
        monkeypatch.setattr(value, '_budget', lambda: budget)
        original = fixture.source.open_question_scope
        @contextmanager
        def scope(*args, **kwargs):
            with original(*args, **kwargs) as current:
                yield current
            now[0] = 31
        monkeypatch.setattr(fixture.source, 'open_question_scope', scope)
        with pytest.raises(QuestionLimitExceeded):
            value.reconcile(ACCOUNT)
    finally:
        cleanup(fixture)


def test_partial_pages_hash_only_on_freeze_or_completion_and_resume(fixture, monkeypatch):
    value, _, _ = runner(fixture)
    original = nlp_findings_store.inventory_digest
    calls = []
    def digest(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(nlp_findings_store, 'inventory_digest', digest)
    assert not value.reconcile(ACCOUNT, maximum=64)
    assert not calls
    partial = value.findings.freeze(ACCOUNT, source_identity(fixture.source, ACCOUNT))
    with fixture.stores.database.read() as db:
        frozen = checked_snapshot(db, partial)
    assert not any(row['scan_complete'] for row in frozen['coverage'].values())
    assert all(row['eligible'] == 64 for row in frozen['coverage'].values())
    # One writer-locked partial seal and the existing independent freeze check.
    assert len(calls) == 2
    calls.clear()
    restarted = FindingsStore(fixture.stores.database)
    value.findings = restarted
    assert not value.reconcile(ACCOUNT, maximum=64)
    assert not calls
    assert value.reconcile(ACCOUNT, maximum=64)
    assert len(calls) == 1
    assert restarted.state(ACCOUNT)['scan_complete']
    with fixture.stores.database.read() as db:
        assert checked_snapshot(db, partial) == frozen


def test_interrupted_page_resumes_without_claiming_complete_inventory(fixture, monkeypatch):
    cancelled = [False]
    value, _, _ = runner(fixture, cancellation_check=lambda: cancelled[0])
    original = value.findings.reconcile_page
    def page(*args, **kwargs):
        result = original(*args, **kwargs)
        cancelled[0] = True
        return result
    with monkeypatch.context() as patch:
        patch.setattr(value.findings, 'reconcile_page', page)
        with pytest.raises(QuestionCancelled):
            value.reconcile(ACCOUNT, maximum=64)
    interrupted = value.findings.state(ACCOUNT)
    assert interrupted['scan_cursor'] == '[]'
    assert not interrupted['scan_complete'] and not interrupted['inventory_digest']
    cancelled[0] = False
    value.findings = FindingsStore(fixture.stores.database)
    while not value.reconcile(ACCOUNT, maximum=64):
        pass
    assert value.findings.state(ACCOUNT)['scan_complete']
    frozen = value.findings.freeze(ACCOUNT, source_identity(fixture.source, ACCOUNT))
    with fixture.stores.database.read() as db:
        assert all(row['eligible'] == 130 for row in checked_snapshot(db, frozen)['coverage'].values())


def test_global_locator_limit_still_rolls_back_an_overfull_page(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, conversations=1, messages=3)
    try:
        value, _, _ = runner(fixture)
        monkeypatch.setattr(nlp_findings_store, 'MAX_LOCATORS', 2)
        with pytest.raises(ValueError, match='nlp_locator_storage_limit'):
            value.reconcile(ACCOUNT)
        with fixture.stores.database.read() as db:
            assert db.execute('SELECT count(*) FROM nlp_sources').fetchone()[0] == 0
        assert not value.findings.state(ACCOUNT)['scan_complete']
    finally:
        cleanup(fixture)


def test_cursor_only_progress_preserves_inventory_visibility(fixture):
    value = admitted(fixture)
    # A revision change restarts the scan, but unchanged source bindings must
    # not advance availability again merely because another cursor is saved.
    with fixture.repositories.database.transaction() as db:
        advance(db)
    assert not value.reconcile(ACCOUNT, maximum=64)
    before = value.findings.state(ACCOUNT)
    assert not value.reconcile(ACCOUNT, maximum=64)
    after = value.findings.state(ACCOUNT)
    assert after['scan_cursor'] != before['scan_cursor']
    for field in ('inventory_digest', 'availability_revision', 'notification_revision'):
        assert after[field] == before[field]


def test_partial_freeze_rejects_source_change_before_snapshot_commit(fixture, monkeypatch):
    value, _, _ = runner(fixture)
    assert not value.reconcile(ACCOUNT, maximum=64)
    canonical = source_identity(fixture.source, ACCOUNT)
    original = nlp_findings_store.inventory_digest
    calls = [0]
    def digest(*args, **kwargs):
        result = original(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 2:
            with fixture.repositories.database.transaction() as db:
                db.execute("UPDATE account_messages SET text='Changed' WHERE creator_account_id=?", (ACCOUNT,))
                advance(db)
            assert not value.reconcile(ACCOUNT, maximum=64)
        return result
    monkeypatch.setattr(nlp_findings_store, 'inventory_digest', digest)
    with pytest.raises(ValueError, match='nlp_snapshot_stale'):
        value.findings.freeze(ACCOUNT, canonical)


def test_small_inventory_uses_actual_publication_path(tmp_path, monkeypatch):
    fixture = make_fixture(tmp_path, conversations=1, messages=3)
    try:
        value = admitted(fixture)
        assert value.process_one(ACCOUNT)
        original = value._admit_inventory
        calls = []
        def admit(account):
            result = original(account)
            calls.append(result)
            return result
        monkeypatch.setattr(value, '_admit_inventory', admit)
        published = value.publish_pending(ACCOUNT)
        assert published.changed
        assert published.artifact.projection.projection_generation == 1
        assert len(calls) == 3  # Freeze, stage and activation each admit live sources.
        assert value.publish_pending(ACCOUNT) is None
    finally:
        cleanup(fixture)


def _append_queue_sources(database, state, start, stop, lane):
    with database.transaction() as db:
        db.executemany(
            'INSERT INTO nlp_sources(account_ref,source_ref,source_version,speaker_ref,'
            'conversation_id,message_id,seen_epoch,lane,read_metadata) VALUES(?,?,?,?,?,?,?,?,?)',
            [(state['account_ref'], f'{index:064x}', 'version', 'speaker', 'chat',
              f'message-{index}', state['scan_epoch'], lane, '{}')
             for index in range(start, stop)],
        )


@pytest.mark.parametrize('lane', ['live', 'backfill'])
@pytest.mark.parametrize('vacancy', [False, True])
def test_queue_claim_and_refill_work_stays_bounded_as_one_lane_inventory_grows(fixture, monkeypatch, lane, vacancy):
    value, _, _ = runner(fixture)
    database = fixture.stores.database
    state = value.findings.state(ACCOUNT)
    _append_queue_sources(database, state, 0, 1024, lane)
    first = value.findings.claim(ACCOUNT)
    assert first is not None
    value.findings.release(first)
    transaction = database.transaction
    work = []
    for ordinal, (start, stop) in enumerate(((1024, 1024), (1024, 8192))):
        _append_queue_sources(database, state, start, stop, lane)
        if vacancy:
            abandoned = value.findings.claim(ACCOUNT)
            value.findings.abandon(abandoned, 'unsupported_input')
        steps = [0]
        def progress():
            steps[0] += 100
            return int(steps[0] > 100_000)
        @contextmanager
        def measured_transaction():
            with transaction() as db:
                db.set_progress_handler(progress, 100)
                try:
                    yield db
                finally:
                    db.set_progress_handler(None, 0)
        with monkeypatch.context() as patch:
            patch.setattr(database, 'transaction', measured_transaction)
            claim = value.findings.claim(ACCOUNT)
        assert claim is not None and claim.source_ref == f'{ordinal + 1 if vacancy else 0:064x}'
        assert value.findings.valid_claim(claim)
        work.append(steps[0])
        value.findings.release(claim)
        with database.read() as db:
            assert db.execute("SELECT count(*) FROM nlp_jobs WHERE state IN ('pending','running')").fetchone()[0] == 256
    assert work[1] <= work[0] + 5_000, work


@pytest.mark.parametrize('lane', ['live', 'backfill'])
def test_late_opposite_lane_enters_full_queue_and_preserves_claim_fairness(fixture, lane):
    value, _, _ = runner(fixture)
    database = fixture.stores.database
    state = value.findings.state(ACCOUNT)
    _append_queue_sources(database, state, 0, 1024, lane)
    claim = value.findings.claim(ACCOUNT)
    assert claim is not None
    value.findings.release(claim)
    opposite = 'backfill' if lane == 'live' else 'live'
    _append_queue_sources(database, state, 1024, 1026, opposite)
    with database.transaction() as db:
        db.execute('UPDATE nlp_accounts SET live_claims=0 WHERE account_ref=?', (state['account_ref'],))
    lanes = []
    for _ in range(4):
        claimed = value.findings.claim(ACCOUNT)
        assert claimed is not None
        with database.read() as db:
            row = db.execute('SELECT lane,source_version,selection_epoch,admission_epoch FROM nlp_jobs WHERE account_ref=? AND source_ref=?',
                             (state['account_ref'], claimed.source_ref)).fetchone()
        assert row['source_version'] == 'version'
        assert row['selection_epoch'] == state['selection_epoch']
        assert row['admission_epoch'] == state['admission_epoch']
        lanes.append(row['lane'])
        value.findings.release(claimed)
    assert lanes == ['live', 'live', 'live', 'backfill']
    with database.read() as db:
        assert db.execute("SELECT count(*) FROM nlp_jobs WHERE state IN ('pending','running')").fetchone()[0] == 256
        assert db.execute('SELECT lane FROM nlp_jobs WHERE account_ref=? AND source_ref=?',
                          (state['account_ref'], f'{1024:064x}')).fetchone()[0] == opposite


def test_backfill_refills_after_terminal_work_with_a_sustained_live_backlog(fixture):
    value, _, _ = runner(fixture)
    database = fixture.stores.database
    state = value.findings.state(ACCOUNT)
    _append_queue_sources(database, state, 0, 1024, 'live')
    initial = value.findings.claim(ACCOUNT)
    value.findings.release(initial)
    _append_queue_sources(database, state, 1024, 1026, 'backfill')
    with database.transaction() as db:
        db.execute('UPDATE nlp_accounts SET live_claims=3 WHERE account_ref=?', (state['account_ref'],))
    first_backfill = value.findings.claim(ACCOUNT)
    assert first_backfill.source_ref == f'{1024:064x}'
    value.findings.abandon(first_backfill, 'unsupported_input')
    lanes = []
    for _ in range(4):
        claim = value.findings.claim(ACCOUNT)
        assert claim is not None
        with database.read() as db:
            lane = db.execute('SELECT lane FROM nlp_jobs WHERE account_ref=? AND source_ref=?',
                              (state['account_ref'], claim.source_ref)).fetchone()[0]
            assert db.execute("SELECT count(*) FROM nlp_jobs WHERE state IN ('pending','running')").fetchone()[0] == 256
        lanes.append(lane)
        value.findings.abandon(claim, 'unsupported_input')
    assert lanes == ['live', 'live', 'live', 'backfill']
    assert claim.source_ref == f'{1025:064x}'
    after = value.findings.state(ACCOUNT)
    for field in ('selection_epoch', 'admission_epoch', 'scan_epoch', 'scan_cursor', 'inventory_digest'):
        assert after[field] == state[field]


def test_source_lane_migration_preserves_populated_inventory_and_claims(fixture, tmp_path):
    import hashlib
    import json
    import shutil
    from pathlib import Path
    from app.analytics.database import ProjectionsDatabase
    from app.analytics.enrichment_proof_transition import GUARD_DIGESTS
    from app.analytics.validation_receipt import TRIGGER_DIGESTS, content_stamp

    _, profile, _ = runner(fixture)
    catalog = Path(__file__).parents[1] / 'app/analytics/sql'
    previous = tmp_path / 'previous-catalog'
    previous.mkdir()
    for source in catalog.glob('*.sql'):
        if int(source.name[:4]) <= 30:
            shutil.copy2(source, previous / source.name)
    path = tmp_path / 'upgrade.sqlite3'
    database = ProjectionsDatabase(path, migrations_dir=previous)
    store = FindingsStore(database)
    store.select(ACCOUNT, profile)
    state = store.state(ACCOUNT)
    _append_queue_sources(database, state, 0, 512, 'backfill')
    claim = store.claim(ACCOUNT)
    assert claim is not None
    def content(database):
        with database.read() as db:
            return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1,2')]
                    for table in ('nlp_accounts', 'nlp_sources', 'nlp_jobs')}
    before = content(database)
    with database.read() as db:
        before_stamp = content_stamp(db)
        before_signatures = {row[0]: hashlib.sha256(row[1].encode()).hexdigest()
                             for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
    assert before_stamp is not None
    upgraded = ProjectionsDatabase(path)
    assert content(upgraded) == before
    with upgraded.read() as db:
        after_stamp = content_stamp(db)
        signatures = {row[0]: hashlib.sha256(row[1].encode()).hexdigest()
                      for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
    assert after_stamp is not None and after_stamp[:2] == before_stamp[:2]
    assert after_stamp[2] != before_stamp[2] and after_stamp[3] == before_stamp[3]
    def signature_digest(values):
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    literal_tables = ('nlp_literal_accounts', 'nlp_literal_derivations', 'nlp_literal_derivation_batches',
                      'nlp_literal_publications', 'nlp_literal_publication_batches')
    literal_tracking = {f'generation_content_{table}_{event}'
                        for table in literal_tables for event in ('insert', 'update', 'delete')}
    literal_guards = {f'{table}_{guard}' for table in literal_tables
                      for guard in ('immutable', 'replace_blocked')}
    literal_guards.add('nlp_literal_publication_batches_building')
    origin_tracking = {f'generation_content_nlp_origins_{event}' for event in ('insert', 'delete')}
    origin_tracking |= {f'generation_content_nlp_origin_epoch_{event}' for event in ('insert', 'update', 'delete')}
    origin_guards = {'nlp_record_origins_delete', 'nlp_record_origins_immutable'}
    assert signatures.keys() - before_signatures.keys() == literal_tracking | literal_guards | origin_tracking | origin_guards
    assert {name: signatures[name] for name in before_signatures} == before_signatures
    assert signature_digest(before_signatures) == GUARD_DIGESTS[30]
    assert signature_digest(signatures) == GUARD_DIGESTS[CURRENT_SCHEMA_VERSION]
    assert signature_digest({name: value for name, value in before_signatures.items()
                             if name.startswith('generation_content_')}) == TRIGGER_DIGESTS[30]
    assert signature_digest({name: value for name, value in signatures.items()
                             if name.startswith('generation_content_')}) == TRIGGER_DIGESTS[CURRENT_SCHEMA_VERSION]
    restarted = FindingsStore(upgraded)
    assert restarted.valid_claim(claim)
    restarted.release(claim)
    assert restarted.claim(ACCOUNT).source_ref == claim.source_ref
    assert upgraded.migration_runner.run() == []
    with upgraded.read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == CURRENT_SCHEMA_VERSION
