"""Shown-only saved reads retain source integrity without execution admission."""
import hashlib
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.analytics.nlp_preview import decode_preview
from app.models.question_evidence import SourceSpan
from app.nlp.profiles import canonical_bytes
from app.persistence.retention import CreatorVaultRetention
from app.persistence.auth import BridgeSessionIssue, RevocationKey, RevocationScopeType, WebAuthnCredential
from app.security.runtime_policy import RuntimeAuthorizationDenied
from tests.continuous_analytics_fixture import ACCOUNT, advance, insert_message
from tests.text_analysis_preview_fixture import (
    make_preview_fixture, close_preview_fixture, prepare_preview, accumulated_output,
    preview_http_client,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


@pytest.fixture
def shown(tmp_path):
    x = make_preview_fixture(tmp_path)
    try:
        x.body, prepared = prepare_preview(x)
        x.binding = x.service.bind_pending(x.service.pending_locators()[0])
        x.token = x.service.claim(x.binding)
        x.frame = accumulated_output(x, x.binding, findings=(0,))
        x.receipt = x.service.commit(x.binding, x.token, x.frame, validation_wall_clock_ms=30000)
        yield x
    finally:
        close_preview_fixture(x)


@pytest.mark.parametrize('side,change', [
    ('auth', 'invalid'), ('auth', 'missing'), ('auth', 'mismatch'),
    ('canonical', 'invalid'), ('canonical', 'missing'), ('canonical', 'mismatch'),
])
def test_baseline_read_survives_pair_invalidation_module_cache_and_projection_loss(shown, monkeypatch, side, change):
    x = shown
    x.authority.revoke(x.reference.reference_id)
    with x.auth.database.transaction() as db:
        db.execute('DELETE FROM nlp_dependency_profiles')
    database = x.auth.database if side == 'auth' else x.repository.database
    with database.transaction() as db:
        if change == 'invalid':
            db.execute('UPDATE authoritative_pair SET valid=0')
        elif change == 'missing':
            db.execute('DELETE FROM authoritative_pair')
        else:
            db.execute('UPDATE authoritative_pair SET incarnation=?', ('f'*32,))
    def absent(*args, **kwargs):
        pytest.fail('Saved preview tried to open an Engine')
    monkeypatch.setattr(x.environment.engine, 'open', absent)
    output = x.service.read_shown(x.http_policy(), x.receipt.contribution_id)
    assert canonical_bytes(output) == x.frame
    assert {summary.kind for summary in output.summaries} == {'topic_category', 'mention_category'}
    assert all({e.ordinal for e in summary.evidence} == {0} for summary in output.summaries)
    for summary in output.summaries:
        for entry in summary.evidence:
            resolved = x.service.read_saved_evidence(x.http_policy(), x.receipt.contribution_id, entry.reference)
            assert resolved.reference == entry.reference and resolved.text == 'Thanks pricing'
            assert resolved.location.model_dump() == x.binding.sources[entry.ordinal].location.model_dump()
    assert x.service.read_outcome(x.http_policy(), x.body.external_key).receipt.state == 'committed'
    assert x.service.prepare(x.http_policy(), x.body).receipt.state == 'committed'
    with x.repository.database.read() as db:
        assert not db.execute('SELECT 1 FROM knowledge_members').fetchone()
        assert not db.execute('SELECT 1 FROM knowledge_contributions').fetchone()
    with x.dependencies.findings.database.read() as db:
        assert not db.execute('SELECT 1 FROM nlp_records').fetchone()
        assert not db.execute('SELECT 1 FROM nlp_record_origins').fetchone()


def test_unshown_member_erasure_removes_whole_preview_without_refund(shown):
    x = shown
    reference = decode_preview(x.frame).summaries[0].evidence[0].reference
    hidden = x.binding.sources[1]
    CreatorVaultRetention(x.repository.database, clock=lambda: x.f.clock.now).delete_message(
        ACCOUNT, hidden.location.message_id)
    with pytest.raises(ValueError, match='output_unavailable'):
        x.service.read_saved_evidence(x.http_policy(), x.receipt.contribution_id, reference)
    result = x.service.read_outcome(x.http_policy(), x.body.external_key)
    assert result.receipt.state == 'result_deleted' and result.scope is None
    assert result.receipt.contribution_id == x.receipt.contribution_id
    with x.repository.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'spent'
        assert not db.execute('SELECT 1 FROM text_analysis_preview_outputs').fetchone()


def test_unrelated_addition_preserves_original_preview_bytes(shown):
    x = shown
    reference = decode_preview(x.frame).summaries[0].evidence[0].reference
    with x.repository.database.transaction() as db:
        insert_message(db, 'chat-0', 'later-independent', x.f.clock.now, 7)
        advance(db)
    assert canonical_bytes(x.service.read_shown(x.http_policy(), x.receipt.contribution_id)) == x.frame
    resolved = x.service.read_saved_evidence(x.http_policy(), x.receipt.contribution_id, reference)
    assert resolved.reference == reference
    with x.repository.database.read() as db:
        assert db.execute('SELECT canonical_revision FROM account_heads').fetchone()[0] > reference.source_revision


def test_saved_output_hash_corruption_is_refused(shown):
    x = shown
    with x.repository.database.transaction() as db:
        db.execute('DROP TRIGGER text_analysis_preview_immutable')
        db.execute("UPDATE text_analysis_preview_outputs SET document_sha256=?", ('0'*64,))
    with pytest.raises(ValueError, match='output_corrupt'):
        x.service.read_shown(x.http_policy(), x.receipt.contribution_id)
    with x.repository.database.read() as db:
        assert db.execute('SELECT 1 FROM text_analysis_preview_outputs').fetchone()
        assert db.execute('SELECT 1 FROM text_analysis_preview_capsules').fetchone()
        assert db.execute('SELECT state FROM text_analysis_intents').fetchone()[0] == 'committed'


def test_shown_read_allows_current_account_principal_but_original_receipt_stays_owned(shown, tmp_path):
    x = shown
    credential = x.auth.webauthn_credential('identity-credential-'+x.identity.identity.principal_id,
                                           principal_id=x.identity.identity.principal_id)
    assert credential is not None
    with x.auth.database.read() as db:
        original_grants = [tuple(row) for row in db.execute(
            'SELECT reference_id,compact_jws FROM verified_grant_references ORDER BY reference_id')]
    references = x.identity.identity.grant_reference_ids
    # Local credential re-enrollment reuses the original verified grant bytes.
    x.auth.revoke(RevocationKey(RevocationScopeType.WEBAUTHN_CREDENTIAL, credential.credential_id), reason='replaced')
    public_key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    replacement = WebAuthnCredential('preview-reader-credential', 'another-preview-reader',
        credential.external_issuer, credential.external_subject, credential.installation_id,
        public_key, 0, x.f.clock.now)
    x.auth.register_webauthn_credential(replacement)
    session = x.auth.issue_bridge_session(BridgeSessionIssue(replacement.credential_id,
        replacement.principal_id, ACCOUNT, 'creator', x.f.clock.now+timedelta(minutes=10), references))
    policy = x.auth.read_bridge_session(session.session_value).policy
    current = x.service.identity.capture(policy)
    assert current.principal_id != x.identity.identity.principal_id
    assert current.creator_account_id == ACCOUNT and current.organization_id == x.context.organization_id
    assert current.installation_id == x.context.installation_id and set(current.grant_reference_ids) == set(references)
    with x.auth.database.read() as db:
        assert [tuple(row) for row in db.execute(
            'SELECT reference_id,compact_jws FROM verified_grant_references ORDER BY reference_id')] == original_grants
    assert canonical_bytes(x.service.read_shown(policy, x.receipt.contribution_id)) == x.frame
    reference = decode_preview(x.frame).summaries[0].evidence[0].reference
    assert x.service.read_saved_evidence(policy, x.receipt.contribution_id, reference).reference == reference
    with pytest.raises(ValueError, match='receipt_scope_invalid'):
        x.service.read_outcome(policy, x.body.external_key)


def test_retention_expiry_never_returns_saved_shown_evidence(shown):
    x = shown
    policy = x.http_policy()
    reference = decode_preview(x.frame).summaries[0].evidence[0].reference
    x.f.clock.now += timedelta(days=91)
    # Baseline grants are also time-bound, so any stale authority refuses before content.
    assert x.auth.read_bridge_session(x.identity.session.session_value) is None
    with pytest.raises(RuntimeAuthorizationDenied):
        x.service.read_shown(policy, x.receipt.contribution_id)
    with pytest.raises(RuntimeAuthorizationDenied):
        x.service.read_saved_evidence(policy, x.receipt.contribution_id, reference)


def test_hidden_member_expiry_commits_retirement_without_replenishing_spend(tmp_path):
    x = make_preview_fixture(tmp_path)
    try:
        with x.repository.database.transaction() as db:
            db.execute('UPDATE account_messages SET sent_at=? WHERE creator_account_id=? AND message_id=?',
                ((x.f.clock.now-timedelta(days=90)+timedelta(seconds=1)).isoformat(), ACCOUNT, 'm-0-1'))
            advance(db)
        body, _ = prepare_preview(x)
        binding = x.service.bind_pending(x.service.pending_locators()[0])
        assert binding.sources[0].location.message_id == 'm-0-1'
        token = x.service.claim(binding)
        frame = accumulated_output(x, binding, findings=(1,))
        receipt = x.service.commit(binding, token, frame, validation_wall_clock_ms=30000)
        reference = decode_preview(frame).summaries[0].evidence[0].reference
        x.f.clock.now += timedelta(seconds=2)
        with pytest.raises(ValueError, match='output_unavailable'):
            x.service.read_saved_evidence(x.http_policy(), receipt.contribution_id, reference)
        with x.repository.database.read() as db:
            assert not db.execute('SELECT 1 FROM text_analysis_preview_outputs').fetchone()
            assert not db.execute('SELECT 1 FROM text_analysis_preview_capsules').fetchone()
            assert not db.execute('SELECT 1 FROM knowledge_jobs').fetchone()
            assert db.execute('SELECT state FROM text_analysis_intents WHERE job_id=?', (receipt.job_id,)).fetchone()[0] == 'result_deleted'
            assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'spent'
        assert x.service.read_outcome(x.http_policy(), body.external_key).receipt.state == 'result_deleted'
    finally:
        close_preview_fixture(x)


@pytest.mark.parametrize('mutation', ['account', 'conversation', 'message', 'version', 'revision', 'time', 'span', 'unshown'])
def test_saved_evidence_rejects_forged_or_unshown_reference_without_erasing_output(shown, mutation):
    x = shown
    reference = decode_preview(x.frame).summaries[0].evidence[0].reference
    fields = {
        'account': {'account_ref': 'a1:'+'0'*64},
        'conversation': {'conversation_ref': 'c1:'+'0'*64},
        'message': {'message_ref': 'm1:'+'0'*64},
        'version': {'source_version_digest': 'sha256:'+'0'*64},
        'revision': {'source_revision': reference.source_revision+1},
        'time': {'sent_at': reference.sent_at+timedelta(seconds=1)},
        'span': {'span': SourceSpan(start=1, end=3)},
    }
    if mutation == 'unshown':
        with x.repository.database.read() as db:
            source = x.repository._source(db, x.binding.spec, x.binding.sources[1], x.repository._budget())
        requested = source.reference()
    else:
        requested = reference.model_copy(update=fields[mutation])
    with pytest.raises(ValueError, match='preview_evidence_unavailable'):
        x.service.read_saved_evidence(x.http_policy(), x.receipt.contribution_id, requested)
    assert canonical_bytes(x.service.read_shown(x.http_policy(), x.receipt.contribution_id)) == x.frame
    with x.repository.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'spent'


@pytest.mark.parametrize('scope', [RevocationScopeType.BRIDGE_SESSION, RevocationScopeType.PRINCIPAL,
                                 RevocationScopeType.CREATOR_ACCOUNT, RevocationScopeType.INSTALLATION])
def test_saved_evidence_refuses_revoked_session_or_original_account_scope(shown, scope):
    x = shown
    reference = decode_preview(x.frame).summaries[0].evidence[0].reference
    policy = x.http_policy()
    key = {RevocationScopeType.BRIDGE_SESSION: policy.identity.session_id,
           RevocationScopeType.PRINCIPAL: policy.identity.principal_id,
           RevocationScopeType.CREATOR_ACCOUNT: ACCOUNT,
           RevocationScopeType.INSTALLATION: x.context.installation_id}[scope]
    x.auth.revoke(RevocationKey(scope, key), reason='unavailable')
    with pytest.raises(RuntimeAuthorizationDenied):
        x.service.read_saved_evidence(policy, x.receipt.contribution_id, reference)
    with x.repository.database.read() as db:
        assert bytes(db.execute('SELECT document FROM text_analysis_preview_outputs').fetchone()[0]) == x.frame


def test_actual_saved_evidence_route_preserves_repeated_unicode_spans_without_analysis(tmp_path, monkeypatch):
    x = make_preview_fixture(tmp_path)
    try:
        text = '😀 café café'
        with x.repository.database.transaction() as db:
            db.execute('UPDATE account_messages SET text=?', (text,))
            advance(db)
        body, _ = prepare_preview(x)
        binding = x.service.bind_pending(x.service.pending_locators()[0])
        token = x.service.claim(binding)
        frame = accumulated_output(x, binding, mention_spans=((2, 6), (7, 11)))
        receipt = x.service.commit(binding, token, frame, validation_wall_clock_ms=30000)
        output = decode_preview(frame)
        x.authority.revoke(x.reference.reference_id)
        with x.auth.database.transaction() as db:
            db.execute('DELETE FROM nlp_dependency_profiles')
        def unavailable(*args, **kwargs):
            pytest.fail('Saved evidence attempted analysis or preparation')
        monkeypatch.setattr(x.environment.engine, 'open', unavailable)
        monkeypatch.setattr(type(x.selection), 'choose', unavailable)
        with x.repository.database.transaction() as db:
            insert_message(db, 'chat-0', 'after-original-preview', x.f.clock.now, 8)
            advance(db)
        reopened = x.configuration.bind(x.source, x.dependencies)
        with preview_http_client(x, service=reopened) as client:
            for summary in output.summaries:
                for entry in summary.evidence:
                    result = client.post(f'/api/v1/nlp/previews/results/{receipt.contribution_id}/evidence',
                                         json=entry.reference.model_dump(mode='json'))
                    assert result.status_code == 200, result.text
                    assert result.headers['Cache-Control'] == 'no-store'
                    value = result.json()
                    assert value['reference'] == entry.reference.model_dump(mode='json')
                    assert value['text'] == text
                    assert value['location'] == binding.sources[entry.ordinal].location.model_dump(mode='json')
                    expected_span = None if entry.reference.span is None else {
                        'start': entry.reference.span.start+1, 'end': entry.reference.span.end+1}
                    assert value['browser_span'] == expected_span
                    if expected_span is not None:
                        raw = value['text'].encode('utf-16-le')
                        assert raw[expected_span['start']*2:expected_span['end']*2].decode('utf-16-le') == 'café'
        assert x.service.read_outcome(x.http_policy(), body.external_key).receipt.state == 'committed'
        with x.repository.database.read() as db:
            assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'spent'
            assert not db.execute('SELECT 1 FROM knowledge_members').fetchone()
    finally:
        close_preview_fixture(x)


def test_actual_saved_evidence_route_requires_csrf_origin_and_closed_bounded_json(shown):
    x = shown
    reference = decode_preview(x.frame).summaries[0].evidence[0].reference.model_dump(mode='json')
    path = f'/api/v1/nlp/previews/results/{x.receipt.contribution_id}/evidence'
    with preview_http_client(x) as client:
        assert client.post(path, json=reference, headers={'Origin': 'https://untrusted.invalid'}).status_code == 403
        csrf = client.headers.pop('X-CSRF-Token')
        assert client.post(path, json=reference).status_code == 403
        client.headers['X-CSRF-Token'] = csrf
        assert client.post(path, json={**reference, 'location': {}}).status_code == 422
        assert client.post(path+'?message_id=unshown', json=reference).status_code == 422
        assert client.post(path, content=b'{}'*8193, headers={'Content-Type': 'application/json'}).status_code == 413
        assert client.post(path, content=b'{"source_revision":1,"source_revision":2}',
                           headers={'Content-Type': 'application/json'}).status_code == 422
        assert client.post(path, json=reference, headers={'Content-Encoding': 'gzip'}).status_code == 415
        assert client.post(path, content='not-json').status_code == 415
        assert client.post(path, json=reference).status_code == 200
        x.auth.revoke(RevocationKey(RevocationScopeType.BRIDGE_SESSION, x.http_policy().identity.session_id), reason='logout')
        assert client.post(path, json=reference).status_code == 401
