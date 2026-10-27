"""Original-operation persistence, recovery and atomic local adoption."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.persistence.auth import SQLiteAuthenticationStore, InstallationKeyReservation, InstallationKeyReference
from app.persistence.authoritative_backup import security_projection
from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
from app.persistence.text_analysis_reissue import (
    TextAnalysisReissueStore, ReissueJournalError, BODY_LEDGER_BYTES, METADATA_MAX,
    RESERVATION_BYTES, INSTALLATION_BUDGET_BYTES,
)
from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
from app.security.runtime_policy import AuthContext, IdentityAccountAuthority, RuntimeAuthorizationDenied
from app.security.text_analysis_contract import TextAnalysisSchemas, canonical
from app.security.text_analysis_reissue import (
    PROFILE, TextAnalysisReissueError, validate_reissue_projection, validate_confirmation,
    validate_reissue_result, build_read_request, build_recover_request, build_finalize_request,
)
from app.services.text_analysis_reissue import (
    TextAnalysisReissueService, configure_reissue_service, reissue_service,
)
from tests.test_text_analysis_reissue import context, NOW, RESPONSES
from tests.test_text_analysis_authority import local_signer  # noqa: F401

pytestmark = pytest.mark.ci_tier('fast')


@pytest.fixture
def basis(tmp_path):
    from app.security.text_analysis_authority import TextAnalysisKey, TextAnalysisTrust
    from app.security.signed_objects import public_key
    from contracts.loader import load_trust_set
    value = load_trust_set('text-analysis-license-reissue-v2/trust-set.json', environment='development')
    key = value['keys'][0]
    trust = TextAnalysisTrust({key['jwk']['kid']: TextAnalysisKey(key['purpose'],
        public_key({name: key['jwk'][name] for name in ('kty', 'crv', 'x', 'y')}))}, True)
    now = [NOW]
    auth = SQLiteAuthenticationStore(tmp_path/'auth.sqlite3', clock=lambda: now[0])
    original = replace(context(), operation_expires_at=None)
    binding = original.binding
    reservation = InstallationKeyReservation('fixture-provider', 'fixture-key', 'ES256', NOW)
    auth.reserve_installation_key(reservation)
    keyref = InstallationKeyReference(reservation.provider_name, reservation.provider_key_name, reservation.algorithm,
        binding['installation_key_id'], binding['installation_key_jkt'], original.public_key_jwk.decode(), NOW, NOW)
    auth.activate_installation_key(keyref)
    policy = replace(auth.build_runtime_policy(AuthContext('person', 'account', 'creator')),
        identity_authority=IdentityAccountAuthority(**binding, grant_reference_ids=(), grant_types=ACCOUNT_AUTHORITY_GRANT_TYPES))
    schemas = TextAnalysisSchemas()
    prepared_value = dict(profile=PROFILE, operation_id=original.operation_id, binding=binding,
        state='awaiting-authorization', expires_at='2026-10-26T00:30:00.000Z', entry_reference='A'*43)
    prepared = validate_reissue_projection(canonical(prepared_value), operation='prepare', context=original, schemas=schemas, now=NOW)
    confirmation = validate_confirmation(canonical(RESPONSES[0]['confirmation']), context=prepared.context, schemas=schemas)
    authorized = validate_reissue_projection(canonical(dict(profile=PROFILE, operation_id=original.operation_id,
        binding=binding, state='authorized', expires_at=confirmation.expires_at,
        confirmation=json.loads(confirmation.confirmation_bytes), confirmation_digest=confirmation.confirmation_digest)),
        operation='read', context=prepared.context, schemas=schemas, now=NOW, previous=prepared)
    inner = build_finalize_request(confirmation, schemas=schemas, now=NOW)
    result = validate_reissue_result(b' '+canonical(RESPONSES[0]['result'])+b'\n', confirmation=confirmation,
        original_finalization_request=inner, authority_context=RESPONSES[0]['context'], trust=trust, schemas=schemas)
    store = TextAnalysisReissueStore(auth, clock=lambda: now[0])
    return SimpleNamespace(auth=auth, original=original, prepared=prepared, authorized=authorized,
        result=result, inner=inner, schemas=schemas, trust=trust, key=keyref, policy=policy, now=now, store=store)


def reserve(b, context_override=None):
    c = context_override or b.original
    return b.store.begin(origin='https://authority.example', context=c,
        provider_name=b.key.provider_name, provider_key_name=b.key.provider_key_name,
        read_request=build_read_request(c, schemas=b.schemas), recover_request=build_recover_request(c, schemas=b.schemas))


def ready(b):
    r = b.store.prepared(reserve(b), b.prepared)
    r = b.store.authorized(r, b.authorized)
    return b.store.finalization(r, b.inner)


class FixtureClient:
    def __init__(self, b):
        self.b = b
        self.schemas, self.trust = b.schemas, b.trust
        self.calls, self.ensure_calls, self.reopen_calls = [], 0, 0
        self.key_authority = self
        self.read_value = b.authorized
        self.lost_finalize = False

    def ensure_ready(self):
        self.ensure_calls += 1
        return self.b.key

    def reopen_existing(self):
        self.reopen_calls += 1
        return self.b.key

    def _exchange(self, name, context, before_send):
        before_send()
        # A separate writer can reserve while an exchange is in progress.
        with self.b.auth.database.transaction() as db:
            assert db.execute('SELECT 1').fetchone()[0] == 1
        self.calls.append((name, context))

    def prepare(self, context, *, before_send):
        self._exchange('prepare', context, before_send)
        return self.b.prepared

    def read(self, context, *, before_send, **kwargs):
        self._exchange('read', context, before_send)
        return self.read_value

    def finalize(self, confirmation, *, before_send, **kwargs):
        self._exchange('finalize', confirmation.context, before_send)
        assert kwargs['original_finalization_request'] == self.b.inner
        if self.lost_finalize:
            raise TextAnalysisReissueError('temporarily_unavailable')
        return self.b.result

    def recover(self, context, *, before_send, **kwargs):
        self._exchange('recover', context, before_send)
        assert kwargs['original_finalization_request'] == self.b.inner
        return self.b.result


def service(b, client=None, factory=None):
    client = client or FixtureClient(b)
    owner = TextAnalysisReissueService(b.auth, client, provider_origin='https://authority.example',
        context_factory=factory or (lambda policy, key: b.original),
        authority_context_factory=lambda confirmation: RESPONSES[0]['context'], clock=lambda: b.now[0])
    return owner, client


def test_complete_ledger_reservation_and_raw_restart(basis):
    b = basis
    assert BODY_LEDGER_BYTES == 110592 and BODY_LEDGER_BYTES+METADATA_MAX == 124*1024
    assert RESERVATION_BYTES == 128*1024 and INSTALLATION_BUDGET_BYTES//RESERVATION_BYTES == 64
    record = reserve(b)
    restarted = TextAnalysisReissueStore(SQLiteAuthenticationStore(b.auth.database.path), clock=lambda: NOW)
    assert restarted.get(record.flow_id) == record
    assert record.bodies['prepare_request'] == b.original.original_prepare
    assert reserve(b).flow_id == record.flow_id


def test_confirmation_and_short_deadline_are_frozen_separately(basis):
    b = basis
    r = b.store.prepared(reserve(b), b.prepared)
    r = b.store.authorized(r, b.authorized)
    assert r.metadata['operation_expires_at'] != r.metadata['confirmation_expires_at']
    changed = replace(b.authorized, confirmation=replace(b.authorized.confirmation, confirmation_bytes=b'{}'))
    with pytest.raises(ReissueJournalError, match='reissue_conflict'):
        b.store.authorized(r, changed)
    # Keep the first read bytes while later equivalent protected reads can differ in framing.
    assert b.store.authorized(r, replace(b.authorized, response_bytes=b' '+b.authorized.response_bytes)).bodies['authorized_response'] == b.authorized.response_bytes


def test_compare_and_swap_refuses_stale_mutation(basis):
    b = basis
    original = reserve(b)
    current = b.store.prepared(original, b.prepared)
    with pytest.raises(ReissueJournalError, match='reissue_conflict'):
        b.store.expired(original)
    assert b.store.get(original.flow_id) == current


def test_atomic_result_jws_reference_and_exact_winner(basis):
    b = basis
    r = ready(b)
    authorities = TextAnalysisAuthorityStore(b.auth)
    result = b.store.adopt(r, b.result, authority_store=authorities,
        authority_context=RESPONSES[0]['context'], trust=b.trust, schemas=b.schemas)
    assert result.state == 'adopted' and result.bodies['result'] == b.result.response_bytes
    with b.auth.database.read() as db:
        row = db.execute('SELECT * FROM text_analysis_authority_references').fetchone()
        assert row['compact_jws'] == b.result.authority.compact_jws and row['state'] == 'staged'
    assert b.store.adopt(r, b.result, authority_store=authorities,
        authority_context=RESPONSES[0]['context'], trust=b.trust, schemas=b.schemas) == result
    with pytest.raises(ReissueJournalError, match='reissue_result_invalid'):
        b.store.adopt(r, replace(b.result, response_bytes=b.result.response_bytes+b' '), authority_store=authorities,
            authority_context=RESPONSES[0]['context'], trust=b.trust, schemas=b.schemas)


def test_failed_atomic_adoption_rolls_back_authority_and_epoch(basis, monkeypatch):
    b = basis
    r, epoch = ready(b), b.auth.read_epoch()
    def fail(*args, **kwargs):
        raise RuntimeError('injected commit failure')
    monkeypatch.setattr(b.store, '_change', fail)
    with pytest.raises(RuntimeError, match='injected'):
        b.store.adopt(r, b.result, authority_store=TextAnalysisAuthorityStore(b.auth),
            authority_context=RESPONSES[0]['context'], trust=b.trust, schemas=b.schemas)
    with b.auth.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_authority_references').fetchone()[0] == 0
    assert b.auth.read_epoch() == epoch and b.store.get(r.flow_id) == r


def test_completed_recovery_after_local_expiry_retains_exact_result(basis):
    b = basis
    r = b.store.expired(ready(b))
    b.now[0] += timedelta(days=4)
    adopted = b.store.adopt(r, b.result, authority_store=TextAnalysisAuthorityStore(b.auth),
        authority_context=RESPONSES[0]['context'], trust=b.trust, schemas=b.schemas)
    assert adopted.state == 'adopted'


@pytest.mark.parametrize('field', ['metadata_sha256', 'prepare_request', 'confirmation'])
def test_corrupt_journal_refuses_instead_of_reconstructing(basis, field):
    b = basis
    r = ready(b)
    with b.auth.database.transaction() as db:
        db.execute('UPDATE text_analysis_reissue_operations SET '+field+'=? WHERE flow_id=?',
                   ('0'*64 if field == 'metadata_sha256' else b'{}', r.flow_id))
    with pytest.raises(ReissueJournalError, match='reissue_journal_corrupt'):
        b.store.get(r.flow_id)


def test_closed_budget_preserves_all_prior_intents(basis):
    b = basis
    for n in range(64):
        value = json.loads(b.original.original_prepare)
        value['operation_id'] = f'019a1957-0000-7000-8000-{n:012x}'
        c = replace(b.original, original_prepare=canonical(value))
        r = reserve(b, c)
        projection = replace(b.prepared, context=replace(c, operation_expires_at=b.prepared.context.operation_expires_at))
        b.store.expired(b.store.prepared(r, projection))
    value['operation_id'] = '019a1957-0000-7000-8000-000000000100'
    with pytest.raises(ReissueJournalError, match='reissue_capacity_exhausted'):
        reserve(b, replace(b.original, original_prepare=canonical(value)))
    with b.auth.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_reissue_operations').fetchone()[0] == 64


def test_status_is_inert_and_begin_returns_only_navigation_state(basis):
    b = basis
    owner, client = service(b)
    assert owner.status(b.policy) is None and client.calls == [] and client.ensure_calls == 0
    status = owner.begin(b.policy)
    assert status.state == 'awaiting_authorization' and status.navigation_available
    assert set(vars(status)) == {'flow_id', 'state', 'navigation_available'}
    assert owner.status(b.policy) == status
    assert [x[0] for x in client.calls] == ['prepare']
    assert owner.navigation(b.policy, status.flow_id) == 'https://authority.example/v2/onboarding/text-analysis-license-reissues/'+'A'*43
    assert owner.begin(b.policy) == status and client.ensure_calls == 1


def test_explicit_resume_finishes_after_hosted_confirmation_without_new_selection(basis):
    b = basis
    first, client = service(b)
    status = first.begin(b.policy)
    def forbidden(*args):
        pytest.fail('resume selected a new release or key')
    resumed, _ = service(b, client, forbidden)
    client.ensure_ready = forbidden
    assert resumed.resume(b.policy, status.flow_id).state == 'adopted'
    assert [x[0] for x in client.calls] == ['prepare', 'read', 'finalize']
    assert all(x[1].original_prepare == b.original.original_prepare for x in client.calls)


def test_lost_finalize_response_restart_recovers_original_after_expiry(basis):
    b = basis
    owner, client = service(b)
    status = owner.begin(b.policy)
    client.lost_finalize = True
    with pytest.raises(TextAnalysisReissueError):
        owner.resume(b.policy, status.flow_id)
    saved = owner.store.get(status.flow_id)
    assert saved.state == 'authorized' and saved.bodies['finalize_request'] == b.inner
    b.now[0] += timedelta(days=2)
    resumed, _ = service(b, client, lambda *args: pytest.fail('new context'))
    assert resumed.resume(b.policy, status.flow_id).state == 'adopted'
    assert [x[0] for x in client.calls] == ['prepare', 'read', 'finalize', 'recover']


def test_current_binding_key_and_policy_are_checked_before_network(basis):
    b = basis
    owner, client = service(b)
    status = owner.begin(b.policy)
    client.calls.clear()
    client.b.key = replace(b.key, provider_key_name='different')
    with pytest.raises(ReissueJournalError, match='reissue_key_changed'):
        owner.resume(b.policy, status.flow_id)
    assert client.calls == []


def test_stale_policy_refuses_inert_discovery(basis):
    b = basis
    owner, client = service(b)
    with b.auth.database.transaction() as db:
        b.auth._increment_authorization_epoch(db)
    with pytest.raises(RuntimeAuthorizationDenied):
        owner.status(b.policy)
    assert client.calls == [] and client.ensure_calls == 0


def test_journal_is_security_state_for_matched_backup(basis):
    b = basis
    with b.auth.database.read() as db:
        before = security_projection(db)
    reserve(b)
    with b.auth.database.read() as db:
        assert security_projection(db) != before


def test_unconfigured_provider_remains_absent():
    configure_reissue_service(None)
    assert reissue_service() is None


def test_changed_authority_during_prepare_retains_intent_without_accepting_response(basis):
    b = basis
    owner, client = service(b)
    original = client.prepare
    def changed(context, *, before_send):
        response = original(context, before_send=before_send)
        with b.auth.database.transaction() as db:
            b.auth._increment_authorization_epoch(db)
        return response
    client.prepare = changed
    with pytest.raises(RuntimeAuthorizationDenied):
        owner.begin(b.policy)
    with b.auth.database.read() as db:
        row = db.execute('SELECT * FROM text_analysis_reissue_operations').fetchone()
        assert row['state'] == 'prepared' and row['prepare_response'] is None


def test_missing_replacement_key_is_not_recreated_during_status_or_resume(basis):
    b = basis
    owner, client = service(b)
    status = owner.begin(b.policy)
    with b.auth.database.transaction() as db:
        db.execute('DELETE FROM installation_key_reference')
    with pytest.raises(ReissueJournalError, match='reissue_key_changed'):
        owner.status(b.policy, status.flow_id)
    client.reopen_existing = lambda: None
    with pytest.raises(ReissueJournalError, match='reissue_key_changed'):
        owner.resume(b.policy, status.flow_id)
    assert client.ensure_calls == 1 and [x[0] for x in client.calls] == ['prepare']


def test_authority_import_requires_the_exact_owned_transaction(basis):
    b = basis
    authorities = TextAnalysisAuthorityStore(b.auth)
    with b.auth.database.read() as db:
        with pytest.raises(RuntimeAuthorizationDenied, match='text_analysis_auth_owner_mismatch'):
            authorities.import_verified_reference_in(db, b.result.authority.compact_jws,
                kind='license', context=RESPONSES[0]['context'], trust=b.trust, schemas=b.schemas)


def test_matched_backup_preserves_journal_and_refuses_older_intent_state(tmp_path, basis):
    from app.persistence.authoritative_backup import create_authoritative_backup, verify_authoritative_backup, restore_authoritative_backup
    from app.persistence.backup import SQLiteBackupError
    from tests.test_authoritative_pair import make_pair
    from tests.test_authoritative_backup import offline
    owner = make_pair(tmp_path/'pair')
    owner.establish()
    b = basis
    owner.authentication.reserve_installation_key(InstallationKeyReservation(b.key.provider_name,
        b.key.provider_key_name, b.key.algorithm, b.key.created_at))
    owner.authentication.activate_installation_key(b.key)
    store = TextAnalysisReissueStore(owner.authentication, clock=lambda: NOW)
    record = store.begin(origin='https://authority.example', context=b.original,
        provider_name=b.key.provider_name, provider_key_name=b.key.provider_key_name,
        read_request=build_read_request(b.original, schemas=b.schemas), recover_request=build_recover_request(b.original, schemas=b.schemas))
    manifest = create_authoritative_backup(owner, tmp_path/'set')
    assert verify_authoritative_backup(tmp_path/'set') == manifest
    restore_authoritative_backup(offline(owner), tmp_path/'set')
    assert store.get(record.flow_id) == record
    store.prepared(record, b.prepared)
    with pytest.raises(SQLiteBackupError):
        restore_authoritative_backup(offline(owner), tmp_path/'set')
    assert store.get(record.flow_id).state == 'awaiting_authorization'


def test_prejournal_backup_catalog_refuses_without_manufacturing_history(basis):
    from app.persistence.backup import _verify_database, SQLiteBackupError
    b = basis
    with b.auth.database.transaction() as db:
        db.execute('DROP TABLE text_analysis_reissue_operations')
        db.execute('DELETE FROM schema_migrations WHERE version=37')
        db.execute('PRAGMA user_version=36')
    with b.auth.database.read() as db:
        with pytest.raises(SQLiteBackupError, match='backup migration catalog differs'):
            _verify_database(db, 'auth')


def test_real_proofs_transport_grants_journal_and_adoption_compose(tmp_path, local_signer):
    from tests.text_analysis_reissue_fixture import make_reissue_fixture
    x = make_reissue_fixture(tmp_path, local_signer)
    policy = x.http_policy()
    assert policy.identity_authority is None and policy.commercial_authority is None and policy.text_analysis_authority is None
    assert x.service.status(policy) is None and x.requests == []
    prepared = x.service.begin(policy)
    assert prepared.state == 'awaiting_authorization'
    assert x.service.resume(x.http_policy(), prepared.flow_id).state == 'adopted'
    retained = x.service.store.get(prepared.flow_id)
    assert retained.bodies['result'][:1] == b' ' and retained.bodies['result'][-1:] == b'\n'
    assert len(x.challenges) == 3 and all(used for _, used in x.challenges.values())
    assert x.service.status(x.http_policy(), prepared.flow_id).state == 'adopted'
    assert len(x.requests) == 6


def test_begin_never_creates_a_key_after_durable_reference_loss(basis):
    b = basis
    owner, client = service(b)
    with b.auth.database.transaction() as db:
        db.execute('DELETE FROM installation_key_reference')
    with pytest.raises(ReissueJournalError, match='reissue_key_changed'):
        owner.begin(b.policy)
    assert client.ensure_calls == 0 and client.calls == []


def test_backup_refuses_missing_original_replacement_key(basis):
    from app.persistence.backup import SQLiteBackupError
    b = basis
    reserve(b)
    with b.auth.database.transaction() as db:
        db.execute('DELETE FROM installation_key_reference')
    with b.auth.database.read() as db:
        with pytest.raises(SQLiteBackupError, match='authoritative_reissue_journal_invalid'):
            security_projection(db)


@pytest.mark.parametrize('field,value', [('operation_id', '019a1957-0000-7000-8000-000000000099'),
    ('finalization_request_digest', '0'*64), ('seat_id', 'different-seat')])
def test_first_adoption_revalidates_envelope_before_authority_or_epoch(basis, field, value):
    b = basis
    record = ready(b)
    raw = json.loads(b.result.response_bytes)
    raw[field] = value
    forged = replace(b.result, response_bytes=canonical(raw))
    epoch = b.auth.read_epoch()
    with pytest.raises(ReissueJournalError, match='reissue_result_invalid'):
        b.store.adopt(record, forged, authority_store=TextAnalysisAuthorityStore(b.auth),
            authority_context=RESPONSES[0]['context'], trust=b.trust, schemas=b.schemas)
    with b.auth.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_authority_references').fetchone()[0] == 0
    assert b.auth.read_epoch() == epoch and b.store.get(record.flow_id) == record
    assert 'different-seat' not in repr(record) and 'prepare_request' not in repr(record)


def test_explicit_new_begin_after_expired_resume_retains_old_operation(basis):
    b = basis
    owner, client = service(b)
    first = owner.begin(b.policy)
    b.now[0] += timedelta(minutes=31)
    client.read_value = validate_reissue_projection(canonical(dict(profile=PROFILE,
        operation_id=b.original.operation_id, binding=b.original.binding, state='expired',
        expires_at=b.prepared.context.operation_expires_at)), operation='read', context=b.prepared.context,
        schemas=b.schemas, now=b.now[0], previous=b.prepared)
    assert owner.resume(b.policy, first.flow_id).state == 'expired'
    assert owner.status(b.policy, first.flow_id).state == 'expired'
    with pytest.raises(ReissueJournalError, match='reissue_conflict'):
        owner.begin(b.policy)
    original = json.loads(b.original.original_prepare)
    original['operation_id'] = '019a1957-0000-7000-8000-000000000099'
    fresh = replace(b.original, original_prepare=canonical(original))
    owner.context_factory = lambda policy, key: fresh
    raw = json.loads(b.prepared.response_bytes)
    raw.update(operation_id=fresh.operation_id,
        expires_at=(b.now[0]+timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%M:%S.000Z'))
    b.prepared = validate_reissue_projection(canonical(raw), operation='prepare', context=fresh,
        schemas=b.schemas, now=b.now[0])
    second = owner.begin(b.policy)
    assert second.flow_id != first.flow_id and second.state == 'awaiting_authorization'
    assert owner.store.get(first.flow_id).metadata['operation_id'] == b.original.operation_id
    assert owner.store.get(first.flow_id).state == 'expired'
    assert owner.store.get(second.flow_id).metadata['operation_id'] == fresh.operation_id
