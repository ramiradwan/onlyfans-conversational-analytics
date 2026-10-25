"""Verified grants and durable sessions in real encrypted authentication stores."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.persistence.auth import (
    BridgeSessionIssue, RevocationKey, RevocationScopeType, SQLiteAuthenticationStore, WebAuthnCredential,
)
from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
from app.security.hosted_grants import HostedGrantClient, GrantVerificationRefused
from app.security.runtime_policy import AuthContext, RuntimeAuthorizationDenied
from app.security.text_analysis_identity import TextAnalysisIdentityOwner
from tests.test_hosted_grants import signed_bundle, _payload, _token

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


class NoTransport:
    def request(self, *args, **kwargs):
        pytest.fail('Unexpected transport request')


def make_identity_fixture(path, *, scope=None, creator_account_id='account', principal_id='person',
                          role='creator', authentication=None, now=NOW):
    clock = SimpleNamespace(now=now)
    auth = authentication or SQLiteAuthenticationStore(path, clock=lambda:clock.now)
    bundle = signed_bundle(iat=int(now.timestamp()) - 60)
    if scope is None:
        scope = {name:getattr(bundle.installation_key, name) for name in ('installation_key_id', 'installation_key_jkt')}
        first = _payload(bundle.tokens['installation_grant'])
        scope.update({name:first[name] for name in ('organization_id', 'installation_id')})
    key = replace(bundle.installation_key, installation_key_id=scope['installation_key_id'],
                  installation_key_jkt=scope['installation_key_jkt'])
    tokens = {}
    original = dict(bundle.tokens, creator_account_binding=next(iter(bundle.creator_bindings.values())))
    for kind in ACCOUNT_AUTHORITY_GRANT_TYPES:
        payload = dict(_payload(original[kind]), **scope)
        if kind == 'installation_grant':
            payload['sub'] = 'installation:' + scope['installation_id']
        elif kind == 'creator_account_binding':
            payload.update(creator_account_id=creator_account_id,
                           sub=f"installation:{scope['installation_id']}:creator:{creator_account_id}")
        else:
            payload['allowed_creator_account_ids'] = [creator_account_id]
        purpose = 'membership' if kind == 'membership_snapshot' else 'installation-binding'
        trust = next(row for row in bundle.trust_set['keys'] if row['purpose'] == purpose)
        protected_type = {'installation_grant':'installation', 'membership_snapshot':'membership',
                          'creator_account_binding':'creator-binding'}[kind]
        tokens[kind] = _token(payload, f'urn:bridge-clean:grant:{protected_type}:v1', trust['jwk'], bundle.purpose_keys[purpose])
    verifier = HostedGrantClient(NoTransport(), None, auth, clock=lambda:clock.now, trust_set=bundle.trust_set)
    identity = AuthContext(principal_id, creator_account_id, role)
    grants = verifier._verify_bundle(tokens, grant_types=ACCOUNT_AUTHORITY_GRANT_TYPES,
        organization_id=scope['organization_id'], installation_id=scope['installation_id'], key=key, identity=identity)
    auth.record_verified_grants(grants)
    private = ec.generate_private_key(ec.SECP256R1())
    credential_id = 'identity-credential-' + principal_id
    auth.register_webauthn_credential(WebAuthnCredential(credential_id, principal_id, grants[0].issuer,
        grants[0].subject, scope['installation_id'],
        private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo),
        0, now))
    session = auth.issue_bridge_session(BridgeSessionIssue(credential_id, principal_id, creator_account_id, role,
        now + timedelta(minutes=10), tuple(grant.reference_id for grant in grants)))
    policy = auth.read_bridge_session(session.session_value).policy
    owner = TextAnalysisIdentityOwner(auth)
    return SimpleNamespace(auth=auth, owner=owner, policy=policy, identity=owner.capture(policy), session=session,
        clock=clock, grants=grants, tokens=tokens, verifier=verifier, key=key, scope=scope)


@pytest.fixture
def identity(tmp_path):
    return make_identity_fixture(tmp_path/'auth.sqlite3')


def test_current_durable_session_captures_only_reverified_identity_lineage(identity):
    x = identity
    assert x.policy.identity_authority is None
    assert x.identity.principal_id == x.policy.identity.principal_id
    assert set(x.identity.grant_reference_ids) == set(x.policy.signed_object_reference_ids)
    assert 'session' not in json.dumps(x.identity.model_dump())
    assert x.auth.database.path.read_bytes()[:16] != b'SQLite format 3\x00'
    with x.auth.database.read() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM bridge_sessions')]
    baseline = x.owner.rebuild(x.identity)
    assert baseline.identity.session_id is None and baseline.identity.session_expires_at is None
    assert baseline.identity_authority.grant_reference_ids == x.identity.grant_reference_ids
    assert baseline.commercial_authority is None and baseline.text_analysis_authority is None
    assert baseline.text_analysis_fence is None and x.auth.runtime_policy_is_current(baseline)
    with x.auth.database.read() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM bridge_sessions')] == before


def test_background_identity_outlives_browser_session_without_renewing_grants(identity):
    x = identity
    x.clock.now += timedelta(minutes=11)
    assert x.auth.read_bridge_session(x.session.session_value) is None
    with pytest.raises(RuntimeAuthorizationDenied):
        x.owner.capture(x.policy)
    current = x.owner.rebuild(x.identity)
    assert current.expires_at == min(grant.expires_at for grant in x.grants)
    assert current.identity.session_id is None


def test_browser_session_revocation_does_not_replace_retained_grant_authority(identity):
    x = identity
    x.auth.revoke(RevocationKey(RevocationScopeType.BRIDGE_SESSION, x.policy.identity.session_id))
    with pytest.raises(RuntimeAuthorizationDenied):
        x.owner.capture(x.policy)
    current = x.owner.rebuild(x.identity)
    assert current.identity.session_id is None
    assert current.identity_authority.grant_reference_ids == x.identity.grant_reference_ids


def test_grant_revocation_during_recomposition_cannot_return_a_stale_snapshot(identity, monkeypatch):
    x = identity
    original = x.auth.build_runtime_policy_from_grants
    def rebuild_then_revoke(*args):
        policy = original(*args)
        x.auth.revoke(RevocationKey(RevocationScopeType.VERIFIED_GRANT, x.identity.grant_reference_ids[0]))
        return policy
    monkeypatch.setattr(x.auth, 'build_runtime_policy_from_grants', rebuild_then_revoke)
    with pytest.raises(RuntimeAuthorizationDenied):
        x.owner.rebuild(x.identity)


@pytest.mark.parametrize('kind', ['principal', 'account', 'installation', 'grant'])
def test_original_scope_revocation_refuses_fresh_background_recomposition(identity, kind):
    x = identity
    scope, value = {'principal':(RevocationScopeType.PRINCIPAL, x.identity.principal_id),
        'account':(RevocationScopeType.CREATOR_ACCOUNT, x.identity.creator_account_id),
        'installation':(RevocationScopeType.INSTALLATION, x.identity.installation_id),
        'grant':(RevocationScopeType.VERIFIED_GRANT, x.identity.grant_reference_ids[0])}[kind]
    x.auth.revoke(RevocationKey(scope, value))
    with pytest.raises(RuntimeAuthorizationDenied):
        x.owner.rebuild(x.identity)
    with pytest.raises(RuntimeAuthorizationDenied):
        x.owner.capture(x.policy)


@pytest.mark.parametrize('field', ['organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt',
                                  'creator_account_id', 'grant_reference_ids'])
def test_retained_scope_cannot_substitute_another_binding(identity, field):
    changed = identity.identity.model_copy(update={field:('missing-reference',) if field == 'grant_reference_ids' else 'different'})
    with pytest.raises(RuntimeAuthorizationDenied):
        identity.owner.rebuild(changed)


def test_missing_and_expired_original_grants_refuse_without_replacement_search(identity):
    x = identity
    x.clock.now = min(grant.expires_at for grant in x.grants)
    with pytest.raises(RuntimeAuthorizationDenied):
        x.owner.rebuild(x.identity)


@pytest.mark.parametrize('field', ['principal_id', 'creator_account_id', 'role', 'session_id'])
def test_capture_rejects_substituted_session_identity(identity, field):
    x = identity
    changed = replace(x.policy, identity=replace(x.policy.identity, **{field:'operator' if field == 'role' else 'different'}))
    with pytest.raises(RuntimeAuthorizationDenied):
        x.owner.capture(changed)


def test_capture_rejects_ad_hoc_or_substituted_reference_sets(identity):
    x = identity
    with pytest.raises(RuntimeAuthorizationDenied):
        x.owner.capture(x.auth.build_runtime_policy(AuthContext(x.identity.principal_id, x.identity.creator_account_id, 'creator')))
    with pytest.raises(RuntimeAuthorizationDenied):
        x.owner.capture(replace(x.policy, signed_object_reference_ids=x.policy.signed_object_reference_ids[:1]))
    rebuilt = x.owner.rebuild(x.identity)
    with pytest.raises(RuntimeAuthorizationDenied):
        x.owner.capture(replace(x.policy, identity_authority=replace(rebuilt.identity_authority, installation_key_id='different')))


def test_retained_model_validation_is_not_bypassed_by_copy(identity):
    for references in [(), ('same', 'same'), tuple(str(i) for i in range(65))]:
        with pytest.raises(RuntimeAuthorizationDenied):
            identity.owner.rebuild(identity.identity.model_copy(update={'grant_reference_ids':references}))
    with pytest.raises(RuntimeAuthorizationDenied):
        identity.owner.rebuild(identity.identity.model_copy(update={'role':'agent'}))
    with pytest.raises(ValueError):
        TextAnalysisIdentityOwner(object())


def test_fixture_grants_reach_real_signature_verification_before_storage(identity):
    x = identity
    tokens = dict(x.tokens)
    head, payload, signature = tokens['membership_snapshot'].split('.')
    tokens['membership_snapshot'] = head + '.' + payload + '.' + ('A' if signature[0] != 'A' else 'B') + signature[1:]
    with pytest.raises(GrantVerificationRefused):
        x.verifier._verify_bundle(tokens, grant_types=ACCOUNT_AUTHORITY_GRANT_TYPES,
            organization_id=x.identity.organization_id, installation_id=x.identity.installation_id,
            key=x.key, identity=x.policy.identity)
