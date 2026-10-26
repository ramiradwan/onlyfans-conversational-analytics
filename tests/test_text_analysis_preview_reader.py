"""Shown-only saved reads retain source integrity without execution admission."""
import hashlib
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.nlp.profiles import canonical_bytes
from app.persistence.retention import CreatorVaultRetention
from app.persistence.auth import BridgeSessionIssue, RevocationKey, RevocationScopeType, WebAuthnCredential
from app.security.runtime_policy import RuntimeAuthorizationDenied
from tests.continuous_analytics_fixture import ACCOUNT, advance, insert_message
from tests.text_analysis_preview_fixture import (
    make_preview_fixture, close_preview_fixture, prepare_preview, accumulated_output,
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
    hidden = x.binding.sources[1]
    CreatorVaultRetention(x.repository.database, clock=lambda: x.f.clock.now).delete_message(
        ACCOUNT, hidden.location.message_id)
    with pytest.raises(ValueError, match='output_unavailable'):
        x.service.read_shown(x.http_policy(), x.receipt.contribution_id)
    result = x.service.read_outcome(x.http_policy(), x.body.external_key)
    assert result.receipt.state == 'result_deleted' and result.scope is None
    assert result.receipt.contribution_id == x.receipt.contribution_id
    with x.repository.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'spent'
        assert not db.execute('SELECT 1 FROM text_analysis_preview_outputs').fetchone()


def test_unrelated_addition_preserves_original_preview_bytes(shown):
    x = shown
    with x.repository.database.transaction() as db:
        insert_message(db, 'chat-0', 'later-independent', x.f.clock.now, 7)
        advance(db)
    assert canonical_bytes(x.service.read_shown(x.http_policy(), x.receipt.contribution_id)) == x.frame


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
    with pytest.raises(ValueError, match='receipt_scope_invalid'):
        x.service.read_outcome(policy, x.body.external_key)


def test_retention_expiry_never_returns_saved_shown_evidence(shown):
    x = shown
    policy = x.http_policy()
    x.f.clock.now += timedelta(days=91)
    # Baseline grants are also time-bound, so any stale authority refuses before content.
    assert x.auth.read_bridge_session(x.identity.session.session_value) is None
    with pytest.raises(RuntimeAuthorizationDenied):
        x.service.read_shown(policy, x.receipt.contribution_id)


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
        x.f.clock.now += timedelta(seconds=2)
        with pytest.raises(ValueError, match='output_unavailable'):
            x.service.read_shown(x.http_policy(), receipt.contribution_id)
        with x.repository.database.read() as db:
            assert not db.execute('SELECT 1 FROM text_analysis_preview_outputs').fetchone()
            assert not db.execute('SELECT 1 FROM text_analysis_preview_capsules').fetchone()
            assert not db.execute('SELECT 1 FROM knowledge_jobs').fetchone()
            assert db.execute('SELECT state FROM text_analysis_intents WHERE job_id=?', (receipt.job_id,)).fetchone()[0] == 'result_deleted'
            assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'spent'
        assert x.service.read_outcome(x.http_policy(), body.external_key).receipt.state == 'result_deleted'
    finally:
        close_preview_fixture(x)
