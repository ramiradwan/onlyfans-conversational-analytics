"""Original disclosures and current-account shown-output views."""

from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.models.text_analysis_enrollment import EnrollmentCommand
from app.nlp.profiles import canonical_bytes
from app.persistence.auth import (
    BridgeSessionIssue,
    RevocationKey,
    RevocationScopeType,
    WebAuthnCredential,
)
from tests.continuous_analytics_fixture import ACCOUNT
from tests.text_analysis_preview_fixture import (
    make_preview_fixture,
    close_preview_fixture,
    prepare_preview,
    accumulated_output,
    preview_http_client,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


def reenroll(x):
    credential = x.auth.webauthn_credential(
        'identity-credential-' + x.identity.identity.principal_id,
        principal_id=x.identity.identity.principal_id,
    )
    x.auth.revoke(
        RevocationKey(
            RevocationScopeType.WEBAUTHN_CREDENTIAL, credential.credential_id
        ),
        reason='replaced',
    )
    key = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    replacement = WebAuthnCredential(
        'views-reader-credential',
        'views-current-principal',
        credential.external_issuer,
        credential.external_subject,
        credential.installation_id,
        key,
        0,
        x.f.clock.now,
    )
    x.auth.register_webauthn_credential(replacement)
    session = x.auth.issue_bridge_session(
        BridgeSessionIssue(
            replacement.credential_id,
            replacement.principal_id,
            ACCOUNT,
            'creator',
            x.f.clock.now + timedelta(minutes=10),
            x.identity.identity.grant_reference_ids,
        )
    )
    return x.auth.read_bridge_session(session.session_value).policy


@pytest.fixture
def data(tmp_path):
    x = make_preview_fixture(tmp_path)
    try:
        yield x
    finally:
        close_preview_fixture(x)


def shown(x):
    body, _ = prepare_preview(x)
    binding = x.service.bind_pending(x.service.pending_locators()[0])
    token = x.service.claim(binding)
    frame = accumulated_output(x, binding, findings=(0,))
    receipt = x.service.commit(binding, token, frame, validation_wall_clock_ms=30000)
    return body, binding, frame, receipt


def test_execution_view_uses_retained_profile_when_dependencies_disappear(data):
    x = data
    assert x.license_service.read_view(x.http_policy()).profile is None
    command = EnrollmentCommand(
        command_key=str(uuid4()),
        expected_enrollment_id=None,
        expected_mode_version=0,
        action='enable',
        expected_profile_sha256=x.profile.identity_digest(),
    )
    accepted = x.license_service.command(x.http_policy(), command)
    original = x.license_service.read_view(x.http_policy())
    assert (
        original.current == accepted.current
        and original.profile.profile_sha256 == x.profile.identity_digest()
    )
    assert original.profile.languages == ('en',)
    with x.auth.database.transaction() as db:
        db.execute('DELETE FROM nlp_dependency_profiles')
        db.execute('UPDATE authoritative_pair SET valid=0')
    assert x.license_service.read_view(x.http_policy()) == original
    with pytest.raises(ValueError, match='receipt_scope_invalid'):
        x.license_service.read_view(reenroll(x))


def test_prepare_and_saved_view_have_original_languages_without_receipt_leak(data):
    x = data
    body, binding, frame, receipt = shown(x)
    original = x.service.read_outcome(x.http_policy(), body.external_key)
    assert original.disclosure.languages == ('en',)
    view = x.service.read_saved_view(x.http_policy(), receipt.contribution_id)
    assert canonical_bytes(view.output) == frame and view.scope == original.scope
    assert view.disclosure.languages == ('en',)
    assert view.disclosure.profile_sha256 == original.disclosure.profile_sha256
    assert view.disclosure.tasks == original.disclosure.tasks
    raw = canonical_bytes(view)
    for forbidden in (
        b'external_key',
        b'confirmation',
        b'grant_reference_ids',
        b'principal_id',
        b'compact_jws',
    ):
        assert forbidden not in raw
    assert [
        row.contribution_id for row in x.service.saved_list(x.http_policy()).items
    ] == [receipt.contribution_id]
    x.authority.revoke(x.reference.reference_id)
    with x.auth.database.transaction() as db:
        db.execute('DELETE FROM nlp_dependency_profiles')
        db.execute('UPDATE authoritative_pair SET valid=0')
    policy = reenroll(x)
    assert x.service.read_saved_view(policy, receipt.contribution_id) == view
    assert [row.contribution_id for row in x.service.saved_list(policy).items] == [
        receipt.contribution_id
    ]
    with pytest.raises(ValueError, match='receipt_scope_invalid'):
        x.service.read_outcome(policy, body.external_key)


def test_saved_view_read_validates_hidden_source_and_commits_retirement(data):
    from app.persistence.retention import CreatorVaultRetention

    x = data
    _, binding, _, receipt = shown(x)
    CreatorVaultRetention(
        x.repository.database, clock=lambda: x.f.clock.now
    ).delete_message(ACCOUNT, binding.sources[1].location.message_id)
    with pytest.raises(ValueError, match='output_unavailable'):
        x.service.read_saved_view(x.http_policy(), receipt.contribution_id)
    assert x.service.saved_list(x.http_policy()).items == ()
    with x.repository.database.read() as db:
        assert (
            db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[
                0
            ]
            == 'spent'
        )


def test_saved_static_routes_and_typed_response_schemas(data):
    x = data
    _, _, _, receipt = shown(x)
    with preview_http_client(x) as client:
        listing = client.get('/api/v1/nlp/previews/saved')
        assert listing.status_code == 200 and listing.json()['items'] == [
            {'contribution_id': receipt.contribution_id}
        ]
        view = client.get(
            '/api/v1/nlp/previews/results/' + receipt.contribution_id + '/view'
        )
        assert (
            view.status_code == 200
            and view.json()['output']['contribution_id'] == receipt.contribution_id
        )
        assert client.get('/api/v1/nlp/previews/saved?limit=65').status_code == 422


@pytest.mark.parametrize(
    'changed', ['task_order', 'taxonomy', 'presentation', 'enabled_category']
)
def test_saved_envelope_rejects_disclosure_substitution(data, changed):
    import json
    from app.models.text_analysis_preview_execution import PreviewSavedEnvelope

    x = data
    _, _, _, receipt = shown(x)
    raw = json.loads(
        x.service.read_saved_view(
            x.http_policy(), receipt.contribution_id
        ).model_dump_json()
    )
    if changed == 'task_order':
        raw['disclosure']['tasks'].reverse()
    elif changed == 'taxonomy':
        raw['disclosure']['tasks'][0]['taxonomy']['sha256'] = 'f' * 64
    elif changed == 'presentation':
        raw['disclosure']['presentation']['sha256'] = 'f' * 64
    else:
        raw['disclosure']['tasks'][0]['enabled_ids'] = []
    with pytest.raises(ValueError):
        PreviewSavedEnvelope.model_validate_json(json.dumps(raw))


def test_saved_page_rejects_duplicates_reordering_and_invented_cursor():
    from app.models.text_analysis_preview_execution import (
        PreviewSavedPage,
        PreviewSavedId,
    )

    values = sorted([str(uuid4()), str(uuid4())])
    for keys, cursor in (
        ([values[0], values[0]], None),
        ([values[1], values[0]], None),
        ([values[0]], values[1]),
    ):
        with pytest.raises(ValueError, match='page_invalid'):
            PreviewSavedPage(
                items=tuple(PreviewSavedId(contribution_id=key) for key in keys),
                next_after=cursor,
            )


@pytest.mark.parametrize('boundary', ['released', 'journal'])
def test_enrollment_view_requires_live_root_ownership_without_pair_coherence(
    data, monkeypatch, boundary
):
    from dataclasses import replace
    from app.persistence.authoritative_pair import (
        AuthoritativePairOwner,
        RuntimeRootLease,
        RUNTIME_LOCK_FILENAME,
        RESTORE_JOURNAL_FILENAME,
    )
    from app.persistence.migrations import InstallationMigrationLock

    x = data
    with InstallationMigrationLock(x.pair_owner.root / RUNTIME_LOCK_FILENAME) as lock:
        pair = AuthoritativePairOwner(
            x.auth,
            x.repository.database,
            installation_id=x.context.installation_id,
            root_lease=RuntimeRootLease.from_held_lock(x.pair_owner.root, lock),
        )
        monkeypatch.setattr(
            x.license_service,
            'selection',
            replace(x.license_service.selection, pair_owner=pair),
        )
        original = x.license_service.read_view(x.http_policy())
        with x.auth.database.transaction() as db:
            db.execute('UPDATE authoritative_pair SET valid=0')
        assert x.license_service.read_view(x.http_policy()) == original
        if boundary == 'journal':
            (x.pair_owner.root / RESTORE_JOURNAL_FILENAME).write_bytes(b'incomplete')
            with pytest.raises(ValueError, match='authoritative_pair_unavailable'):
                x.license_service.read_view(x.http_policy())
    with pytest.raises(ValueError, match='authoritative_pair_unavailable'):
        x.license_service.read_view(x.http_policy())


@pytest.mark.parametrize(
    'changed', ['cutoff', 'key', 'policy', 'profile', 'job', 'inventory', 'detail']
)
def test_preview_outcome_binds_original_confirmation_and_disclosure(changed):
    from pathlib import Path
    import json
    from app.models.text_analysis_preview_execution import PreviewOutcome

    path = (
        Path(__file__).resolve().parents[1]
        / 'shared/nlp/application/v1/fixtures/preview-outcome.json'
    )
    original = PreviewOutcome.model_validate_json(path.read_bytes())
    raw = original.model_dump(mode='json')
    if changed == 'cutoff':
        cutoff = original.scope.cutoff + timedelta(days=1)
        raw['scope']['cutoff'] = cutoff.isoformat()
        raw['scope']['lower_exclusive'] = (cutoff - timedelta(days=90)).isoformat()
    elif changed == 'key':
        raw['confirmation']['external_key'] = str(uuid4())
    elif changed == 'policy':
        raw['disclosure']['selection_policy']['sha256'] = 'f' * 64
    elif changed == 'profile':
        raw['disclosure']['profile_sha256'] = 'f' * 64
    elif changed == 'job':
        raw['receipt']['job_id'] = str(uuid4())
    elif changed == 'inventory':
        raw['scope']['inventory_sha256'] = 'f' * 64
    else:
        raw.update(scope=None, disclosure=None, detailed_scope_available=False)
    with pytest.raises(ValueError, match='preview_outcome_'):
        PreviewOutcome.model_validate_json(json.dumps(raw))
