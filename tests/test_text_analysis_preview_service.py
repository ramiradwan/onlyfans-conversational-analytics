"""Immutable preview scope, attempt fencing and noncreating outcome recovery."""
from dataclasses import replace
from datetime import timedelta, timezone
from threading import Event, Thread
from contextlib import contextmanager

import pytest

from app.analytics.nlp_preview_scope import freeze_preview_scope
from app.analytics.query_execution import QuestionBudget, QuestionLimits, QuestionLimitExceeded
from app.models.text_analysis_preview_execution import PreviewPrepareRequest
from app.persistence.auth import RevocationKey, RevocationScopeType
from app.persistence.retention import CreatorVaultRetention
from app.security.runtime_policy import RuntimeAuthorizationDenied
from tests.continuous_analytics_fixture import ACCOUNT, advance, insert_message
from tests.text_analysis_preview_fixture import (
    make_preview_fixture, close_preview_fixture, request, prepare_preview, accumulated_output,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.fixture
def preview(tmp_path):
    x = make_preview_fixture(tmp_path)
    try:
        yield x
    finally:
        close_preview_fixture(x)


def test_original_receipt_precedes_readiness_and_deleted_scope_is_not_recreated(preview, monkeypatch):
    x = preview
    body, prepared = prepare_preview(x, confirm=False)
    def unavailable(*args, **kwargs):
        pytest.fail('A retry consulted current profile readiness')
    monkeypatch.setattr(type(x.selection), 'choose', unavailable)
    assert x.service.prepare(x.http_policy(), body) == prepared
    with pytest.raises(ValueError, match='request_conflict'):
        x.service.prepare(x.http_policy(), body.model_copy(update={'expected_profile_sha256': '0'*64}))
    CreatorVaultRetention(x.repository.database, clock=lambda: x.f.clock.now).delete_message(ACCOUNT, 'm-0-0')
    deleted = x.service.prepare(x.http_policy(), body)
    assert deleted.receipt.state == 'result_deleted' and deleted.receipt.job_id == prepared.receipt.job_id
    assert deleted.scope is None and deleted.disclosure is None and deleted.confirmation is None
    with x.repository.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_preview_requests').fetchone()[0] == 1
        assert not db.execute('SELECT 1 FROM text_analysis_preview_capsules').fetchone()
        assert not db.execute('SELECT 1 FROM knowledge_jobs').fetchone()


def test_frozen_scope_uses_latest_eligible_utc_messages_and_budget_exhaustion_refuses(preview):
    x = preview
    with x.repository.database.transaction() as db:
        for i in range(40):
            moment = x.f.clock.now - timedelta(minutes=40-i)
            if i % 2:
                moment = moment.astimezone(timezone(timedelta(hours=3)))
            insert_message(db, 'chat-0', f'recent-{i:02}', moment, i)
        insert_message(db, 'chat-0', 'future', x.f.clock.now+timedelta(seconds=1), 41)
        insert_message(db, 'chat-0', 'expired', x.f.clock.now-timedelta(days=90), 42)
        insert_message(db, 'chat-0', 'too-large', x.f.clock.now, 43, text='x'*65537)
        advance(db)
    scope, identity, members = freeze_preview_scope(x.repository, x.source, ACCOUNT, x.f.clock.now,
        QuestionBudget(QuestionLimits(max_records=100000, wall_clock_ms=30000)))
    assert scope.source_count == identity.source_count == 32
    assert [m.location.message_id for m in members] == [f'recent-{i:02}' for i in range(8, 40)]
    assert scope.earliest == x.f.clock.now-timedelta(minutes=32)
    assert scope.latest == x.f.clock.now-timedelta(minutes=1)
    with pytest.raises(QuestionLimitExceeded):
        freeze_preview_scope(x.repository, x.source, ACCOUNT, x.f.clock.now,
            QuestionBudget(QuestionLimits(max_records=1, wall_clock_ms=30000)))


def test_prepared_cannot_claim_and_confirmation_does_not_reselect_sources(preview):
    x = preview
    body, prepared = prepare_preview(x, confirm=False)
    assert not x.service.pending_locators()
    with x.repository.database.transaction() as db:
        insert_message(db, 'chat-0', 'new-after-prepare', x.f.clock.now, 5)
        advance(db)
    with pytest.raises(ValueError, match='confirmation_mismatch'):
        x.service.confirm(x.http_policy(), body.external_key,
            prepared.confirmation.model_copy(update={'input_inventory_sha256': '0'*64}))
    confirmed = x.service.confirm(x.http_policy(), body.external_key, prepared.confirmation)
    assert confirmed.scope == prepared.scope and confirmed.receipt.job_id == prepared.receipt.job_id
    binding = x.service.bind_pending(x.service.pending_locators()[0])
    assert len(binding.sources) == 2
    assert all(m.location.message_id != 'new-after-prepare' for m in binding.sources)
    token = x.service.claim(binding)
    x.service.check(binding, token)
    with pytest.raises(ValueError, match='binding_invalid'):
        x.service.check(replace(binding), token)
    with pytest.raises(ValueError, match='attempt_conflict'):
        x.service.claim(binding)


def test_no_output_is_terminal_nonspend_and_original_key_never_restarts(preview):
    x = preview
    body, confirmed = prepare_preview(x)
    binding = x.service.bind_pending(x.service.pending_locators()[0])
    token = x.service.claim(binding)
    outcome = accumulated_output(x, binding, findings=False)
    receipt = x.service.commit(binding, token, outcome, validation_wall_clock_ms=30000)
    assert receipt.state == 'failed' and receipt.terminal_reason == 'empty'
    assert x.service.prepare(x.http_policy(), body).receipt.state == 'failed'
    assert x.service.confirm(x.http_policy(), body.external_key, confirmed.confirmation).receipt.state == 'failed'
    assert not x.service.pending_locators()
    with x.repository.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'available'
        assert not db.execute('SELECT 1 FROM text_analysis_preview_outputs').fetchone()
        assert not db.execute('SELECT 1 FROM knowledge_members').fetchone()


def test_stale_attempt_cannot_commit_after_lease_reclamation(preview):
    x = preview
    prepare_preview(x)
    first = x.service.bind_pending(x.service.pending_locators()[0])
    old = x.service.claim(first, lease_seconds=1)
    output = accumulated_output(x, first)
    x.f.clock.now += timedelta(seconds=2)
    second = x.service.bind_pending(x.service.pending_locators()[0])
    current = x.service.claim(second)
    assert current.job_id == old.job_id and current.attempt_fence == old.attempt_fence+1
    with pytest.raises(ValueError, match='attempt_stale'):
        x.service.commit(first, old, output, validation_wall_clock_ms=30000)
    with x.repository.database.read() as db:
        assert not db.execute('SELECT 1 FROM text_analysis_preview_outputs').fetchone()
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'reserved'


def test_exact_capsule_capacity_boundary_rolls_back_job_and_lifetime_receipt(preview):
    x = preview
    with x.repository.database.read() as db:
        initial = x.repository._storage(db)
    x.service.prepare(x.http_policy(), request(x))
    from app.persistence.contributions import storage_high_water
    with x.repository.database.read() as db:
        used = x.repository._storage(db)
        before = storage_high_water(db)
    body = request(x)
    x.repository.max_storage_bytes = used + (used-initial) - 1
    with pytest.raises(ValueError, match='storage_full'):
        x.service.prepare(x.http_policy(), body)
    with x.repository.database.read() as db:
        assert storage_high_water(db) == before
        assert x.repository._storage(db) == used
    assert x.service.read_outcome(x.http_policy(), body.external_key) is None
    x.repository.max_storage_bytes += 1
    assert x.service.prepare(x.http_policy(), body).receipt.state == 'prepared'
    with x.repository.database.read() as db:
        assert x.repository._storage(db) == x.repository.max_storage_bytes


def test_session_revoked_during_real_writer_wait_refuses_before_receipt(preview, monkeypatch):
    x = preview
    body = request(x)
    entered, attempted, acquired, done = Event(), Event(), Event(), Event()
    prechecked, proceed = Event(), Event()
    errors = []
    original = x.auth.database.transaction
    worker_id = []
    @contextmanager
    def observed(*args, **kwargs):
        from threading import get_ident
        reservation = worker_id and get_ident() == worker_id[0] and prechecked.is_set()
        if reservation:
            attempted.set()
        with original(*args, **kwargs) as db:
            if reservation:
                acquired.set()
            yield db
    monkeypatch.setattr(x.auth.database, 'transaction', observed)
    policy = x.http_policy()
    def work():
        from threading import get_ident
        worker_id.append(get_ident())
        entered.set()
        try:
            x.service.read_outcome(policy, body.external_key)
        except Exception as error:
            errors.append(error)
        finally:
            done.set()
    # Stop at the actual reservation after a genuine initial session check.
    capture = x.service._http
    def captured(*args):
        result = capture(*args)
        prechecked.set()
        if not proceed.wait(5):
            raise TimeoutError('Writer reservation was not released')
        return result
    monkeypatch.setattr(x.service, '_http', captured)
    thread = Thread(target=work)
    try:
        thread.start()
        assert entered.wait(5) and prechecked.wait(5)
        with original() as db:
            proceed.set()
            assert attempted.wait(5)
            assert not acquired.is_set() and not done.is_set()
            x.auth._revoke_in_transaction(db, RevocationKey(RevocationScopeType.PRINCIPAL, policy.identity.principal_id), reason='revoked')
            x.auth._increment_authorization_epoch(db)
    finally:
        proceed.set()
        if thread.ident is not None:
            thread.join(10)
    assert acquired.is_set() and done.is_set() and not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeAuthorizationDenied)
    with x.repository.database.read() as db:
        assert not db.execute('SELECT 1 FROM text_analysis_preview_requests').fetchone()
