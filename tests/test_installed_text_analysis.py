"""Release descriptors and first adoption in matched encrypted stores."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.nlp.execution_release import RELEASE_TYPE
from app.nlp.preparation import parse_preparation_release
from app.persistence.auth import InstallationKeyReservation
from app.persistence.authoritative_pair import AuthoritativePairOwner, RUNTIME_LOCK_FILENAME, RuntimeRootLease
from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import InstallationMigrationLock, MigrationRunner
from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
from app.security.runtime_policy import RuntimeAuthorizationDenied
from app.security.signed_objects import decode, encode
from app.security.text_analysis_acquisition import IssuanceContext, validate_authorized_confirmation
from app.security.text_analysis_authority import ORDER
from app.security.text_analysis_contract import PREFIX, SUPPORTED, TextAnalysisSchemas, canonical
from app.services.installed_text_analysis import InstalledTextAnalysisAuthorities, expected_installation_seat
from tests.test_nlp_preparation_release import fixture
from tests.test_nlp_profile_authentication import keys, sign
from tests.test_text_analysis_delivery import SIGNED, example
from tests.test_text_analysis_identity import make_identity_fixture

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def release_inputs(profile_digest=None):
    document, desktop, package = fixture()
    if profile_digest:
        document['profiles'] = [profile_digest]
    signers = {}
    projection = {'environment': 'private-qa', 'production_usable': False, 'keys': []}
    for kind, code in [('license', 'tl'), ('preview', 'tp')]:
        key = ec.generate_private_key(ec.SECP256R1())
        jwk = keys(key)[0]['jwk']
        thumb = hashlib.sha256(canonical(jwk)).digest()
        kid = f'bc1.{code}.{encode(thumb[:16])}'
        signers[kind] = (kid, key)
        projection['keys'].append({'purpose': 'text-analysis-' + kind, 'jwk': dict(jwk, kid=kid),
                                   'thumbprint': encode(thumb)})
    manifest_key = ec.generate_private_key(ec.SECP256R1())
    signers['release'] = ('example-release', manifest_key)
    document.update(schema='nlp-preparation-release.v2', execution={
        'profile_sha256': document['profiles'][0], 'contract_manifest_sha256': 'c'*64,
        'vector_manifest_sha256': 'e'*64, 'trust_set_sha256': digest(canonical(projection)),
        'authority_projection': canonical(projection).decode(), 'authority_projection_sha256': digest(canonical(projection)),
        'release_manifest_key': keys(manifest_key, 'example-release')[0],
    })
    return document, desktop, package, signers


def signed(raw, typ, signer):
    kid, key = signer
    header, body, signature = sign(raw, typ=typ, kid=kid, private_key=key).decode().split('.')
    pair = decode(signature, 64)
    s = int.from_bytes(pair[32:], 'big')
    return (header + '.' + body + '.' + encode(pair[:32] + min(s, ORDER - s).to_bytes(32, 'big'))).encode()


def envelope(release, signer):
    identity = dict(json.loads(release.release_pins), artifact_sha256='a'*64)
    body = dict(package=json.loads(release.package_binding), profile_sha256=release.profile_sha256,
                supported_profiles=list(SUPPORTED), release_identity=identity)
    return signed(canonical(body), RELEASE_TYPE, signer), body


@pytest.fixture(scope='module')
def schemas():
    return TextAnalysisSchemas()


def test_release_signature_binds_package_scope_and_signer(schemas):
    document, desktop, package, signers = release_inputs()
    release = parse_preparation_release(canonical(document), desktop, package).execution
    signature, body = envelope(release, signers['release'])
    assert release.verify_identity(signature, schemas) == canonical(body['release_identity'])
    for changed in (dict(profile_sha256='1'*64), dict(package=dict(body['package'], version='2.0.0.0')),
                    dict(supported_profiles=list(reversed(SUPPORTED))),
                    dict(release_identity=dict(body['release_identity'], trust_set_sha256='0'*64))):
        with pytest.raises(ValueError):
            release.verify_identity(signed(canonical(dict(body, **changed)), RELEASE_TYPE, signers['release']), schemas)
    with pytest.raises(ValueError):
        release.verify_identity(signed(canonical(body), RELEASE_TYPE, signers['preview']), schemas)
    previous = dict(body, package=dict(body['package'], version='1.0.2.0', desktop_sha256='d'*64),
        profile_sha256='f'*64, release_identity=dict(body['release_identity'], release_version='1.0.2.0',
            contract_manifest_sha256='2'*64))
    previous_envelope = signed(canonical(previous), RELEASE_TYPE, signers['release'])
    assert release.verify_retained_identity(previous_envelope, schemas, desktop_sha256='d'*64) == canonical(previous['release_identity'])
    with pytest.raises(ValueError):
        release.verify_identity(previous_envelope, schemas)
    with pytest.raises(ValueError):
        release.verify_retained_identity(previous_envelope, schemas, desktop_sha256='1'*64)
    foreign = dict(previous, package=dict(previous['package'], family='other-family'))
    with pytest.raises(ValueError):
        release.verify_retained_identity(signed(canonical(foreign), RELEASE_TYPE, signers['release']),
                                         schemas, desktop_sha256='d'*64)
    document['execution']['release_manifest_key'] = keys(signers['license'][1], 'release')[0]
    with pytest.raises(ValueError):
        parse_preparation_release(canonical(document), desktop, package)


@contextmanager
def registered(tmp_path, schemas, *, profile_digest=None):
    document, desktop, package, signers = release_inputs(profile_digest)
    release = parse_preparation_release(canonical(document), desktop, package).execution
    identity = make_identity_fixture(tmp_path/'auth.sqlite3')
    auth, key = identity.auth, identity.key
    auth.reserve_installation_key(InstallationKeyReservation(key.provider_name, key.provider_key_name, key.algorithm, key.created_at))
    auth.activate_installation_key(key)
    canonical_db = CanonicalSQLite(tmp_path/'canonical.sqlite3')
    MigrationRunner(canonical_db, migrations_dir=Path(__file__).parents[1]/'app/persistence/sql',
                    lock_path=tmp_path/'migration.lock', backups_dir=tmp_path/'backups').run()
    with InstallationMigrationLock(tmp_path/RUNTIME_LOCK_FILENAME) as lock:
        pair = AuthoritativePairOwner(auth, canonical_db, installation_id=identity.identity.installation_id,
                                     root_lease=RuntimeRootLease.from_held_lock(tmp_path, lock))
        owner = InstalledTextAnalysisAuthorities(release, TextAnalysisAuthorityStore(auth), pair, schemas)
        yield owner, identity, signers


def delivery(kind, owner, identity, signers, release_id, *, seat=None):
    original, response, _ = example(kind)
    payload = dict(next(c['payload'] for c in SIGNED if c['case_id'] == kind+'-accepted'))
    binding = identity.scope
    seat = seat or expected_installation_seat(binding['organization_id'], binding['installation_id'])
    payload.update(binding, seat_id=seat, sub=f"organization:{binding['organization_id']}:installation:{binding['installation_id']}:seat:{seat}:capability:text-analysis")
    fields = ['seat_id', 'seat_scope', 'capability', 'licensed_major_version', 'compatible_artifact_family',
              'update_rights', 'fallback_major_versions']
    if kind == 'preview':
        fields += ['execution_limit', 'output_policy']
    scope = {k: payload[k] for k in fields}
    original.update(binding=binding, release_identity=release_id, supported_profiles={
        'profile': PREFIX+'supported-profiles:v1', 'profiles': list(SUPPORTED)})
    confirmation = {k: original[k] for k in ('authority_profile', 'binding', 'operation_id',
                        'issuance_authorization_id', 'supported_profiles', 'release_identity')}
    confirmation['scope'] = scope
    trusted = validate_authorized_confirmation(canonical(confirmation), context=IssuanceContext(
        kind, original['operation_id'], original['issuance_authorization_id'], canonical(binding), canonical(scope),
        SUPPORTED, canonical(release_id), '2026-10-25T00:05:00.000Z'), schemas=owner.schemas)
    original['confirmation_digest'] = trusted.confirmation_digest
    response.update(binding=binding, seat_id=seat, issuance_request_digest=digest(canonical(original)),
                    signed_authority=signed(canonical(payload), PREFIX+kind+':v1', signers[kind]).decode())
    return dict(issued_from=trusted, original_issuance_request=canonical(original), delivery=canonical(response))


@pytest.mark.parametrize('kind', ['license', 'preview'])
def test_fresh_account_adopts_exact_delivery_and_refuses_unmatched_restore(tmp_path, schemas, kind, monkeypatch):
    with registered(tmp_path, schemas) as (owner, identity, signers):
        policy = lambda: identity.auth.read_bridge_session(identity.session.session_value).policy
        assert owner.read_current(policy(), kind=kind) is None
        assert owner.preview_state(policy()) is None
        empty, seat = owner.acquisition_basis(policy())
        assert empty is None and seat == expected_installation_seat(identity.scope['organization_id'], identity.scope['installation_id'])
        with owner.authority.database.read() as db, owner.pair_owner.canonical_database.read() as c:
            assert db.execute('SELECT * FROM authoritative_pair').fetchone() is None
            assert c.execute('SELECT * FROM authoritative_pair').fetchone() is None
        signature, body = envelope(owner.release, signers['release'])
        from types import SimpleNamespace
        from app.services import nlp_distribution, nlp_setup
        monkeypatch.setattr(nlp_setup, 'load_preparation_release', lambda: SimpleNamespace(
            execution=owner.release, distribution_base_url='https://example.test/'))
        calls = []
        def fetch(transport, path, *_a, **_kw):
            calls.append((transport.base, path))
            return signature
        monkeypatch.setattr(nlp_distribution.ProfileDistribution, 'read', fetch)
        basis, _ = owner.acquisition_basis(policy(), refresh=True)
        assert len(calls) == 1 and calls[0][1] == 'releases/'+owner.release.desktop_sha256
        assert basis == canonical(body['release_identity'])
        wrong = delivery(kind, owner, identity, signers, body['release_identity'], seat='different-seat')
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.adopt(policy(), **wrong)
        actual = delivery(kind, owner, identity, signers, body['release_identity'])
        ref = owner.adopt(policy(), **actual)
        assert not ref.fixture_only and ref.kind == kind
        assert owner.adopt(policy(), **actual) == owner.read_current(policy(), kind=kind) == ref
        assert owner.preview_state(policy()) == ('available' if kind == 'preview' else None)
        with owner.authority.database.read() as db:
            row = db.execute('SELECT response FROM text_analysis_deliveries').fetchone()
            assert row[0] == actual['delivery']
        with owner.pair_owner.canonical_database.transaction() as c:
            c.execute('UPDATE authoritative_pair SET valid=0')
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.read_current(policy(), kind=kind)
        assert len(calls) == 1


def test_first_licensed_command_activates_current_scope_and_rechecks_revocation(tmp_path, schemas, monkeypatch):
    from app.core import optional_execution
    from app.models.text_analysis_enrollment import EnrollmentCommand
    from app.nlp.profiles import artifact_closure_digest, canonical_bytes
    from app.persistence.contributions import ContributionRepository
    from app.persistence.nlp_dependencies import DependencyPersistence
    from app.persistence.text_analysis_enrollment import TextAnalysisEnrollmentStore
    from app.persistence.text_analysis_jobs import TextAnalysisJobStore
    from app.services import installed_text_analysis_selection as selections
    from app.services.text_analysis_execution import TextAnalysisExecutionService
    from tests.test_nlp_task_subsets import selected

    profile = selected('mentions')
    profile_digest = profile.identity_digest()
    closure = artifact_closure_digest(tuple(a for a in profile.artifacts if a.role in ('weights', 'configuration', 'tokenizer')))
    with registered(tmp_path, schemas, profile_digest=profile_digest) as (owner, identity, signers):
        for module in (optional_execution, selections):
            monkeypatch.setattr(module, 'installed_execution_release', lambda: owner.release)
        http_policy = lambda: identity.auth.read_bridge_session(identity.session.session_value).policy
        envelope_bytes, manifest = envelope(owner.release, signers['release'])
        with owner.authority.database.transaction() as db:
            db.execute('INSERT INTO text_analysis_release_identities VALUES(?,?)', (owner.release.desktop_sha256, envelope_bytes))
            state = canonical({'schema':'nlp-dependency-authority.v1','epoch':2,'trust_sha256':'d'*64,
                               'catalog_sha256':'e'*64,'withdrawals':[]})
            db.execute('UPDATE nlp_dependency_authority SET state_json=?,state_sha256=?', (state.decode(), digest(state)))
            db.execute("INSERT INTO nlp_dependency_profiles VALUES(?,?,?,'available')", (profile_digest, canonical_bytes(profile), closure))
        store = TextAnalysisEnrollmentStore(TextAnalysisJobStore(ContributionRepository(owner.pair_owner.canonical_database)))
        source = SimpleNamespace(history=SimpleNamespace(database=owner.pair_owner.canonical_database))
        deps = SimpleNamespace(persistence=DependencyPersistence(identity.auth), source=source,
            gate=owner.authority.database.dependency_authority_gate, execution_owner=None,
            _admit=lambda current: current == profile, ready=lambda current: current == profile,
            preparation_admission=SimpleNamespace(execution=owner.release),
            findings=SimpleNamespace(reconcile_enrollment=lambda *args, **kwargs: None))
        selection = selections.InstalledLicenseSelection(owner, store)
        service = TextAnalysisExecutionService(identity.auth, selection, source, deps)
        command = EnrollmentCommand(command_key=str(uuid4()), action='enable', expected_enrollment_id=None,
            expected_mode_version=0, expected_profile_sha256=profile_digest)
        with pytest.raises(RuntimeAuthorizationDenied):
            service.command(http_policy(), command)
        ref = owner.adopt(http_policy(), **delivery('license', owner, identity, signers, manifest['release_identity']))
        before = http_policy().authorization_epoch.value
        outcome = service.command(http_policy(), command)
        assert outcome.current.state == 'enabled' and http_policy().authorization_epoch.value == before + 1
        assert service.command(http_policy(), command).receipt == outcome.receipt
        binding = service.bind_current(service.current_locators()[0])
        service.authorize_binding(binding, identity.identity.creator_account_id)
        with binding.execution._fence('resume') as held:
            assert held.text_analysis_authority == ref
        owner.authority.revoke(ref.reference_id)
        with pytest.raises(RuntimeAuthorizationDenied):
            with binding.execution._fence('resume'):
                pytest.fail('Revoked reference admitted')


def test_registered_account_can_prepare_acquisition_before_download(tmp_path, schemas, monkeypatch):
    from app.core import customer_release
    from app.services import installed_findings_configuration as configuration
    from app.services.nlp_distribution import ProfileDistribution
    with registered(tmp_path, schemas) as (owner, identity, _):
        monkeypatch.setattr(configuration, 'installed_execution_release', lambda: owner.release)
        monkeypatch.setattr(customer_release, 'load_customer_release_config', lambda *a, **k:
                            SimpleNamespace(hosted_api_origin='https://identity.example.com'))
        def forbid_fetch(*args, **kwargs):
            pytest.fail('Passive status requested release or model files')
        monkeypatch.setattr(ProfileDistribution, 'read', forbid_fetch)
        selected = configuration.InstalledExecutionConfiguration(identity.auth, owner.release)
        assert selected.installation_id() == identity.identity.installation_id
        acquisition = selected.acquisition(owner)
        policy = identity.auth.read_bridge_session(identity.session.session_value).policy
        assert acquisition.status(policy, 'preview') == dict(eligible=True, reason=None, current=None)
        paid = acquisition.status(policy, 'paid')
        has_paid_profile = schemas.has_schema('schemas/paid-text-analysis-acquisition/v1/prepare-response.schema.json')
        assert paid['eligible'] is has_paid_profile
        assert paid['current'] is None
        with identity.auth.database.read() as db:
            for name in ('authoritative_pair', 'text_analysis_native_acquisitions', 'text_analysis_release_identities'):
                assert not db.execute(f'SELECT 1 FROM {name}').fetchone()
