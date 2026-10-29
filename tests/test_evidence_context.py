"""Bounded exact-source neighborhoods and saved-generation admission."""
from contextlib import contextmanager
from datetime import timedelta
import json

import pytest
from pydantic import ValidationError

from app.analytics.evidence import EvidenceUnavailable
from app.analytics.evidence_contracts import EvidenceContextRequest, ResolvedEvidenceContext
from app.analytics.errors import ProjectionUnavailable
from app.analytics.query_contracts import SourceSpan
from app.analytics.query_execution import QuestionBudget, QuestionCancelled, QuestionLimitExceeded, QuestionLimits
from tests.test_analytics_evidence import (ACCOUNT, LOCATION, NOW, TEXT, policy, prepared, stored)  # noqa: F401
from tests.test_nlp_read_projection import api, ready  # noqa: F401

pytestmark = pytest.mark.ci_tier('integration')


def insert(db, name, at, text='Nearby text', *, epoch=1, sequence=1):
    db.execute("""INSERT INTO account_messages(
        creator_account_id,message_id,chat_id,sender_platform_user_id,text,sent_at,direction,
        content_hash,winning_stream_epoch,winning_source_seq,is_deleted,updated_at)
        VALUES (?,?,?,'synthetic-participant',?,?,'inbound','synthetic-hash',?,?,0,?)""",
        (ACCOUNT, name, LOCATION.conversation_id, text, at, epoch, sequence, NOW.isoformat()))


def context_plan(reference, **changes):
    return EvidenceContextRequest(reference=reference, expected_generation=1,
        expected_projection_digest='sha256:' + 'a' * 64, **changes)


def resolve(stored, resolver, reference, source, **changes):
    source.prepare_question_identity(ACCOUNT)
    budget = QuestionBudget(QuestionLimits(max_records=128, wall_clock_ms=5000))
    with source.open_question_scope(ACCOUNT, budget, verify_source_token=True):
        response, lease = resolver.resolve_context(policy(), context_plan(reference, **changes), budget)
        resolver.check_context(policy(), response, budget, lease)
        return response


def source_plan(ready, **changes):
    result = ready.resources.findings(ready.policy, ready.request)
    reference = next(e.reference for e in result.evidence_page.items
                     if e.reference.span is not None and e.reference.span.start == 7)
    return EvidenceContextRequest(reference=reference,
        expected_generation=ready.request.expected_generation,
        expected_projection_digest=ready.request.expected_projection_digest, **changes)


def test_exact_unicode_anchor_bounded_neighbors_and_unchanged_registration(stored):
    with stored.database.transaction() as db:
        for number in range(-6, 7):
            if number:
                insert(db, f'neighbor-{number:+03}', (NOW - timedelta(hours=1) + timedelta(minutes=number)).isoformat())
    resolver, ref, source = prepared(stored, span=SourceSpan(start=1, end=2))
    entries = dict(resolver._entries)
    response = resolve(stored, resolver, ref, source)
    assert len(response.items) == 11 and response.anchor_index == 5
    assert response.has_earlier_stored_items and response.has_later_stored_items
    anchor = response.items[response.anchor_index]
    assert anchor.reference == ref and anchor.text == TEXT
    assert anchor.browser_span == SourceSpan(start=1, end=3)
    assert all(item.reference.span is None and item.browser_span is None
               for i, item in enumerate(response.items) if i != response.anchor_index)
    assert dict(resolver._entries) == entries
    assert all(resolver._entries[key] is value for key, value in entries.items())
    assert 'location' not in response.model_dump_json() and 'conversation_id' not in response.model_dump_json()
    point = resolve(stored, resolver, ref, source, before=0, after=0)
    assert len(point.items) == 1 and point.anchor_index == 0
    assert point.has_earlier_stored_items and point.has_later_stored_items


def test_offset_precision_and_canonical_ties_have_one_instant_order(stored):
    with stored.database.transaction() as db:
        db.execute('UPDATE account_messages SET sent_at=? WHERE creator_account_id=? AND message_id=?',
            ('2026-09-18T11:00:00.000200+00:00', ACCOUNT, LOCATION.message_id))
        insert(db, 'earlier', '2026-09-18T13:00:00.000100+02:00')
        insert(db, 'same-before', '2026-09-18T11:00:00.000200Z', sequence=0)
        insert(db, 'same-after', '2026-09-18T07:00:00.000200-04:00', epoch=2)
        insert(db, 'later', '2026-09-18T11:00:00.0003000Z')
    resolver, ref, source = prepared(stored)
    response = resolve(stored, resolver, ref, source)
    assert [item.text for item in response.items] == ['Nearby text', 'Nearby text', TEXT, 'Nearby text', 'Nearby text']
    assert response.anchor_index == 2
    assert [item.reference.sent_at for item in response.items] == sorted(item.reference.sent_at for item in response.items)
    assert not response.has_earlier_stored_items and not response.has_later_stored_items


@pytest.mark.parametrize('field,value', [('before', True), ('after', '1'), ('before', -1), ('after', 6),
    ('expected_generation', True), ('expected_generation', 0), ('expected_generation', 2**53),
    ('expected_generation', '1'), ('expected_projection_digest', 'bad'), ('extra', 1)])
def test_context_request_is_closed_and_strict(field, value):
    reference = {'account_ref': 'a1:'+'a'*64, 'conversation_ref': 'c1:'+'b'*64,
        'message_ref': 'm1:'+'c'*64, 'source_revision': 1, 'source_version_digest': 'sha256:'+'d'*64,
        'sent_at': NOW.isoformat(), 'span': None}
    document = context_plan(reference).model_dump(mode='json')
    document[field] = value
    with pytest.raises(ValidationError):
        EvidenceContextRequest.model_validate(document)


@pytest.mark.parametrize('text', ['\0' * 65537, '\U0001f600' * 65536], ids=['message-bound', 'response-bound'])
def test_context_refuses_full_message_and_encoded_response_overflow(stored, text):
    with stored.database.transaction() as db:
        for number in range(1, 7):
            insert(db, f'large-{number}', (NOW - timedelta(hours=1, minutes=number)).isoformat(), text)
    resolver, ref, source = prepared(stored)
    with pytest.raises(EvidenceUnavailable):
        resolve(stored, resolver, ref, source)


def test_dense_time_bucket_refuses_within_record_budget(stored):
    with stored.database.transaction() as db:
        for number in range(100):
            insert(db, f'dense-{number:03}', (NOW - timedelta(hours=1)).isoformat())
    resolver, ref, source = prepared(stored)
    with pytest.raises(QuestionLimitExceeded):
        resolve(stored, resolver, ref, source)
    assert not hasattr(source._question_scope_local, 'connection')


def test_old_tail_is_skipped_by_utc_index_and_selection_never_materializes_thread(stored, monkeypatch):
    with stored.database.transaction() as db:
        for number in range(2000):
            insert(db, f'old-{number:04}', (NOW - timedelta(days=100, seconds=number)).isoformat())
        insert(db, 'recent', (NOW - timedelta(minutes=30)).isoformat())
        plan = db.execute("""EXPLAIN QUERY PLAN SELECT julianday(sent_at) FROM account_messages
            INDEXED BY account_messages_context WHERE creator_account_id=? AND chat_id=? AND is_deleted=0
            AND julianday(sent_at)>=? AND julianday(sent_at)<=? AND julianday(sent_at)>?
            ORDER BY julianday(sent_at) ASC LIMIT 1""", (ACCOUNT, LOCATION.conversation_id, 0, 9999999, 0)).fetchall()
        description = ' '.join(str(row[-1]) for row in plan)
        assert 'SEARCH' in description and 'account_messages_context' in description and 'TEMP B-TREE' not in description
    resolver, ref, source = prepared(stored)
    monkeypatch.setattr(source, 'conversation_read_model', lambda *a, **k: pytest.fail('whole conversation read'))
    response = resolve(stored, resolver, ref, source)
    assert len(response.items) == 2 and response.anchor_index == 0
    assert not response.has_earlier_stored_items


@pytest.mark.parametrize('fault', ['edit', 'delete', 'expiry'])
def test_neighbor_change_or_retention_during_selection_refuses(stored, monkeypatch, fault):
    with stored.database.transaction() as db:
        insert(db, 'neighbor', (NOW - timedelta(minutes=50)).isoformat())
    resolver, ref, source = prepared(stored)
    original = source.evidence_context_locations
    def select(*args, **kwargs):
        result = original(*args, **kwargs)
        if fault == 'expiry':
            resolver._clock = lambda: NOW + timedelta(days=31)
        else:
            with stored.database.transaction() as db:
                db.execute('UPDATE account_messages SET '+('text=?' if fault == 'edit' else 'is_deleted=?')+
                           ' WHERE creator_account_id=? AND message_id=?',
                           ('Changed' if fault == 'edit' else 1, ACCOUNT, 'neighbor'))
        return result
    monkeypatch.setattr(source, 'evidence_context_locations', select)
    with pytest.raises((EvidenceUnavailable, ProjectionUnavailable)):
        resolve(stored, resolver, ref, source)


def test_neighbor_retention_is_rechecked_while_anchor_lease_is_still_valid(stored):
    from app.analytics.historical_derivation import historical_retention_cutoff

    with stored.database.transaction() as db:
        insert(db, 'expiring-neighbor', (historical_retention_cutoff(NOW) + timedelta(seconds=1)).isoformat())
    clock = [NOW]
    resolver, ref, source = prepared(stored, clock=lambda: clock[0])
    source.prepare_question_identity(ACCOUNT)
    budget = QuestionBudget(QuestionLimits(max_records=128, wall_clock_ms=5000))
    with source.open_question_scope(ACCOUNT, budget, verify_source_token=True):
        response, entry = resolver.resolve_context(policy(), context_plan(ref), budget)
        assert len(response.items) == 2
        clock[0] += timedelta(seconds=2)
        assert entry.expires_at > clock[0]
        with pytest.raises(EvidenceUnavailable):
            resolver.check_context(policy(), response, budget, entry)


def test_current_generation_context_reuses_source_membership_and_no_new_locators(ready):
    request = source_plan(ready)
    entries = dict(ready.resources.evidence._entries)
    result = ready.resources.evidence_context(ready.policy, request)
    assert result.reference == request.reference and len(result.items) == 4
    assert result.projection_generation == request.expected_generation
    assert result.projection_digest == request.expected_projection_digest
    assert result.items[result.anchor_index].browser_span == SourceSpan(start=8, end=12)
    assert ready.resources.evidence._entries == entries
    assert ResolvedEvidenceContext.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize('fault', ['generation', 'projection', 'version', 'account'])
def test_stale_or_forged_context_refuses_before_neighbor_selection(ready, monkeypatch, fault):
    request = source_plan(ready).model_dump(mode='json')
    if fault == 'generation': request['expected_generation'] += 1
    elif fault == 'projection': request['expected_projection_digest'] = 'sha256:'+'f'*64
    elif fault == 'version': request['reference']['source_version_digest'] = 'sha256:'+'f'*64
    else: request['reference']['account_ref'] = 'a1:'+'f'*64
    monkeypatch.setattr(ready.fixture.source, 'evidence_context_locations', lambda *a, **k: pytest.fail('source selection'))
    with pytest.raises((EvidenceUnavailable, ProjectionUnavailable)):
        ready.resources.evidence_context(ready.policy, request)


def test_publication_is_rechecked_after_neighbors_and_cleanup_releases_owners(ready, monkeypatch):
    request = source_plan(ready)
    store = ready.fixture.stores.projections
    acquire = store.question_publications
    @contextmanager
    def publications(*args, **kwargs):
        with acquire(*args, **kwargs) as real:
            class Read:
                calls = 0
                def __call__(self, *a, **k):
                    self.calls += 1
                    if self.calls > 1: raise ProjectionUnavailable(availability='building')
                    return real(*a, **k)
                def findings(self, snapshot): return real.findings(snapshot)
            yield Read()
    monkeypatch.setattr(store, 'question_publications', publications)
    with pytest.raises(ProjectionUnavailable):
        ready.resources.evidence_context(ready.policy, request)
    assert not ready.resources.evidence._entries
    assert not hasattr(ready.fixture.source._question_scope_local, 'connection')
    assert not hasattr(ready.fixture.source._question_scope_local, 'lease')
    assert ready.resources._slots.acquire(blocking=False) and ready.resources._slots.acquire(blocking=False)
    ready.resources._slots.release(); ready.resources._slots.release()


@pytest.mark.parametrize('fault', ['cancel', 'deadline', 'neighbor_edit', 'neighbor_delete', 'retention'])
def test_final_fence_and_failure_cleanup(ready, monkeypatch, fault):
    request = source_plan(ready)
    original = ready.resources.evidence.check_context
    def check(policy, response, budget, entry):
        if fault == 'cancel': raise QuestionCancelled()
        if fault == 'deadline': budget._deadline = budget._clock()
        if fault == 'retention': ready.fixture.clock.now += timedelta(days=31)
        if fault.startswith('neighbor_'):
            neighbor = next(item for i, item in enumerate(response.items) if i != response.anchor_index)
            with ready.fixture.repositories.database.transaction() as db:
                column, value = ('text', 'Changed') if fault == 'neighbor_edit' else ('is_deleted', 1)
                changed = db.execute(f'UPDATE account_messages SET {column}=? WHERE creator_account_id=? AND text=?',
                                     (value, ACCOUNT, neighbor.text))
                assert changed.rowcount == 1
        return original(policy, response, budget, entry)
    monkeypatch.setattr(ready.resources.evidence, 'check_context', check)
    with pytest.raises((EvidenceUnavailable, ProjectionUnavailable, QuestionCancelled, QuestionLimitExceeded)):
        ready.resources.evidence_context(ready.policy, request)
    assert not ready.resources.evidence._entries
    assert not hasattr(ready.fixture.source._question_scope_local, 'connection')
    assert not hasattr(ready.fixture.source._question_scope_local, 'lease')
    assert ready.resources._slots.acquire(blocking=False) and ready.resources._slots.acquire(blocking=False)
    ready.resources._slots.release(); ready.resources._slots.release()


def test_context_http_is_closed_csrf_no_store_and_never_schedules_recovery(api, ready, monkeypatch):
    client, scheduler = api
    request = source_plan(ready).model_dump(mode='json')
    path = '/api/v1/insights/questions/evidence/context'
    response = client.post(path, json=request)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['reference'] == request['reference']
    assert 'location' not in response.json()['items'][0]
    for changes in ({'before': True}, {'after': '1'}, {'expected_generation': 2**53},
                    {'expected_generation': '1'}, {'location': {}}, {'before': 6}):
        refused = client.post(path, json=request | changes)
        assert refused.status_code == 422 and refused.headers['cache-control'] == 'no-store'
    assert client.post(path+'?extra=1', json=request).status_code == 422
    assert client.post(path, content='{"before":0,"before":1}',
                       headers={'Content-Type': 'application/json'}).status_code == 422
    assert client.post(path, json=request, headers={'x-csrf-token': 'wrong'}).status_code == 403
    with monkeypatch.context() as patch:
        patch.setattr('app.api.security._development_context_allowed', lambda: False)
        assert client.post(path, json=request, headers={'Origin': 'https://example.invalid'}).status_code == 403
    stale = client.post(path, json=request | {'expected_generation': request['expected_generation']+1})
    assert stale.status_code == 503 and stale.headers['cache-control'] == 'no-store'
    scheduler.request_recovery.assert_not_called()
