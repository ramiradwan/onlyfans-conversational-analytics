"""Exact module references and fixture-only auth reservations."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence import sqlite_api
from app.persistence.database import CanonicalSQLite
from app.persistence.migrations import MigrationRunner
from app.persistence.text_analysis_authority import (
    FixtureTextAnalysisLifecycle, TextAnalysisAuthorityStore, TextAnalysisCurrentEvidence,
    trust_digest,
)
from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
from app.security.runtime_policy import AuthContext, IdentityAccountAuthority, RuntimeAuthorizationDenied
from app.security.text_analysis_authority import fixture_trust, verify_authority
from app.security.text_analysis_contract import TextAnalysisSchemas, canonical
from app.security.text_analysis_policy import (
    TextAnalysisRunContext, capability_binding, decode_run_context, encode_run_context,
    require_text_analysis, require_text_analysis_fence,
)
from app.security.text_analysis_references import PROFILE_SHA256, reference_from_verified

pytestmark = pytest.mark.ci_tier('fast')
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)
CASES = json.loads((Path(__file__).resolve().parents[1] / 'contracts/text-analysis-authority-v1/signed-cases.json').read_bytes())


@pytest.fixture
def basis(tmp_path):
    auth = SQLiteAuthenticationStore(tmp_path / 'auth.sqlite3', clock=lambda: NOW)
    store = TextAnalysisAuthorityStore(auth)
    trust, schemas = fixture_trust(environment='test'), TextAnalysisSchemas()
    case = next(item for item in CASES if item['case_id'] == 'license-accepted')
    ref = store.import_verified_reference(case['token'], kind='license', context=case['context'], trust=trust, schemas=schemas)
    p = case['payload']
    context = TextAnalysisRunContext(ref.reference_id, ref.compact_jws_sha256, 'person', 'account',
        p['organization_id'],p['installation_id'],p['installation_key_id'],p['installation_key_jkt'],
        p['seat_id'],p['seat_scope'],1,1,'text-analysis-artifact','a'*64,'b'*64,'c'*64)
    policy = replace(auth.build_runtime_policy(AuthContext('person','account','creator')),
        identity_authority=IdentityAccountAuthority(p['organization_id'],p['installation_id'],
            p['installation_key_id'],p['installation_key_jkt'],(),ACCOUNT_AUTHORITY_GRANT_TYPES),
        text_analysis_authority=ref)
    state = {'schema':'nlp-dependency-authority.v1','epoch':2,'trust_sha256':'d'*64,
             'catalog_sha256':'e'*64,'withdrawals':[]}
    with auth.database.transaction() as db:
        raw = canonical(state)
        db.execute('UPDATE nlp_dependency_authority SET state_json=?,state_sha256=?', (raw.decode(),hashlib.sha256(raw).hexdigest()))
        db.execute("INSERT INTO nlp_dependency_profiles VALUES(?,?,?,'available')", ('a'*64,b'{}','b'*64))
    evidence = TextAnalysisCurrentEvidence(trust,trust_digest(trust),policy.authorization_epoch.value,
        1,2,'d'*64,'e'*64,hashlib.sha256(encode_run_context(context)).hexdigest(),None,
        context.seat_id,context.installation_key_id,context.installation_key_jkt,True)
    activation = store.activate_reference(policy,context,kind='license',evidence=evidence,
        lifecycle=FixtureTextAnalysisLifecycle(auth.database),fixture_only=True)
    policy, evidence = activation.policy, activation.evidence
    return auth,store,case,ref,context,policy,evidence,schemas


@pytest.fixture
def preview_basis(basis,tmp_path):
    auth,store,_,_,context,policy,evidence,schemas = basis
    case = next(item for item in CASES if item['case_id']=='preview-accepted')
    ref = store.import_verified_reference(case['token'],kind='preview',context=case['context'],trust=evidence.trust,schemas=schemas)
    context = replace(context,reference_id=ref.reference_id,compact_jws_sha256=ref.compact_jws_sha256)
    policy = replace(policy,authorization_epoch=auth.read_epoch(),text_analysis_authority=ref)
    evidence = replace(evidence,authorization_epoch=policy.authorization_epoch.value,reference_version=1,
        context_sha256=hashlib.sha256(encode_run_context(context)).hexdigest())
    canonical_db = CanonicalSQLite(tmp_path / 'canonical.sqlite3')
    MigrationRunner(canonical_db,migrations_dir=Path(__file__).resolve().parents[1]/'app/persistence/sql',
        lock_path=tmp_path/'canonical-migration.lock',backups_dir=tmp_path/'canonical-backups').run()
    lifecycle = FixtureTextAnalysisLifecycle(auth.database,canonical_db)
    return auth,store,ref,context,policy,evidence,lifecycle


def _import_preview_admission(store,ref,context,policy,evidence,lifecycle, *, replacement=None):
    from app.persistence.contributions import ContributionRepository
    from app.persistence.text_analysis_jobs import TextAnalysisJobStore
    with store.transition_fence(policy,context,kind='preview',phase='import',evidence=evidence,
            lifecycle=lifecycle,fixture_only=True) as held:
        with lifecycle.canonical_database.transaction() as db:
            if replacement is None:
                TextAnalysisJobStore(ContributionRepository(lifecycle.canonical_database)).import_preview_in(
                    db,encode_run_context(context),policy=held)
            else:
                columns = ['installation_id','permit_id','reference_id','compact_jws_sha256','issuance_id']
                values = [context.installation_id,ref.object_id,ref.reference_id,ref.compact_jws_sha256,ref.issuance_id]
                values[columns.index(replacement)] = 'wrong'
                db.execute("INSERT INTO text_analysis_preview_admissions VALUES(?,?,?,?,?,'available',NULL,NULL)",values)


def test_original_compact_and_payload_and_contract_digests_are_distinct(basis):
    _,_,case,ref,context,policy,_,_ = basis
    assert ref.compact_jws_sha256 == hashlib.sha256(case['token'].encode('ascii')).hexdigest()
    assert ref.payload_sha256 == hashlib.sha256(canonical(case['payload'])).hexdigest()
    binding = capability_binding(ref,context)
    assert binding.profile_id == case['payload']['profile']
    assert binding.profile_sha256 == PROFILE_SHA256['license']
    assert len({binding.profile_sha256,ref.payload_sha256,binding.object_sha256,context.algorithm_profile_sha256}) == 4
    assert require_text_analysis(policy,context,kind='license',phase='prepare') == ref
    assert decode_run_context(encode_run_context(context)) == context
    assert policy.signed_object_reference_ids == ()


@pytest.mark.parametrize('field,value', [('reference_id','wrong'),('compact_jws_sha256','f'*64),
    ('principal_id','other'),('creator_account_id','other'),('organization_id','other'),
    ('installation_id','other'),('installation_key_id','other'),('installation_key_jkt','other'),
    ('seat_id','other'),('seat_scope','other'),('licensed_major_version',2),('selected_major_version',2),
    ('fallback_major_version',1)])
def test_independent_scope_substitution_refused(basis,field,value):
    *_,context,policy,evidence,schemas = basis
    with pytest.raises(RuntimeAuthorizationDenied):
        require_text_analysis(policy,replace(context,**{field:value}),kind='license',phase='start')


def test_missing_and_starter_authority_do_not_substitute(basis):
    _,_,_,_,context,policy,_,_ = basis
    with pytest.raises(RuntimeAuthorizationDenied):
        require_text_analysis(replace(policy,text_analysis_authority=None),context,kind='license',phase='prepare')
    with pytest.raises(RuntimeAuthorizationDenied):
        require_text_analysis(policy,context,kind='preview',phase='prepare')
    with pytest.raises(RuntimeAuthorizationDenied):
        require_text_analysis_fence(policy,context,kind='license',phase='commit')


def test_exact_reimport_does_not_reset_revocation_or_epoch(basis):
    auth,store,case,ref,_,_,evidence,schemas = basis
    epoch = auth.read_epoch()
    store.import_verified_reference(case['token'],kind='license',context=case['context'],trust=evidence.trust,schemas=schemas)
    assert auth.read_epoch() == epoch
    store.revoke(ref.reference_id)
    revoked_epoch = auth.read_epoch()
    store.import_verified_reference(case['token'],kind='license',context=case['context'],trust=evidence.trust,schemas=schemas)
    assert auth.read_epoch() == revoked_epoch
    with auth.database.read() as db:
        assert db.execute('SELECT state,version FROM text_analysis_authority_references').fetchone()['state'] == 'revoked'


def test_purpose_qualified_object_collision_and_original_byte_collision(basis):
    auth,store,case,ref,_,_,evidence,schemas = basis
    # A second independently valid signature over the same object must not replace its bytes.
    other = next(item for item in CASES if item['kind']=='license' and item['expected']=='accepted'
        and item['token'] != case['token'] and item['payload']['license_id']==case['payload']['license_id'])
    with pytest.raises(RuntimeAuthorizationDenied, match='object_collision'):
        store.import_verified_reference(other['token'],kind='license',context=other['context'],trust=evidence.trust,schemas=schemas)
    preview = next(item for item in CASES if item['case_id']=='preview-accepted')
    second = store.import_verified_reference(preview['token'],kind='preview',context=preview['context'],trust=evidence.trust,schemas=schemas)
    assert second.kind != ref.kind
    with auth.database.read() as db:
        assert db.execute('SELECT count(*) FROM text_analysis_authority_references').fetchone()[0] == 2


def test_fixture_fence_is_live_only_inside_held_transaction(basis):
    auth,store,_,_,context,policy,evidence,_ = basis
    with store.transition_fence(policy,context,kind='license',phase='start', evidence=evidence,
            lifecycle=FixtureTextAnalysisLifecycle(auth.database),fixture_only=True) as held:
        assert require_text_analysis_fence(held,context,kind='license',phase='commit') == held.text_analysis_authority
        assert held.text_analysis_fence._connection.in_transaction
        with auth.database.read() as competing:
            competing.execute('PRAGMA busy_timeout=1')
            with pytest.raises(sqlite_api.OperationalError, match='locked'):
                competing.execute('BEGIN IMMEDIATE')
    with pytest.raises(RuntimeAuthorizationDenied,match='fence_missing'):
        require_text_analysis_fence(held,context,kind='license',phase='commit')


@pytest.mark.parametrize('mode', ['missing','production','wrong-store','stale-epoch','wrong-seat','withdrawn'])
def test_final_evidence_refusals(basis, mode):
    auth,store,_,_,context,policy,evidence,_ = basis
    lifecycle = FixtureTextAnalysisLifecycle(auth.database)
    fixture = True
    if mode == 'missing': evidence = None
    if mode == 'production': fixture = False
    if mode == 'wrong-store': lifecycle = FixtureTextAnalysisLifecycle(object())
    if mode == 'stale-epoch': evidence = replace(evidence,authorization_epoch=evidence.authorization_epoch+1)
    if mode == 'wrong-seat': evidence = replace(evidence,seat_id='other')
    if mode == 'withdrawn':
        with auth.database.transaction() as db:
            row = db.execute('SELECT state_json FROM nlp_dependency_authority').fetchone()
            state = json.loads(row[0]); state['withdrawals'] = ['a'*64]
            raw = canonical(state)
            db.execute('UPDATE nlp_dependency_authority SET state_json=?,state_sha256=?',(raw.decode(),hashlib.sha256(raw).hexdigest()))
    with pytest.raises(RuntimeAuthorizationDenied):
        with store.transition_fence(policy,context,kind='license',phase='commit',evidence=evidence,
                lifecycle=lifecycle,fixture_only=fixture):
            pytest.fail('unexpected reservation')


@pytest.mark.parametrize('value', [True,1.0,0,2147483648])
def test_context_strict_integer_domain(basis,value):
    context = basis[4]
    with pytest.raises(ValueError):
        replace(context,selected_major_version=value)


def test_signed_update_rights(basis):
    _,_,_,_,context,policy,evidence,schemas = basis
    case = next(item for item in CASES if item['case_id']=='license-no-update-rights')
    accepted_context = dict(case['context'], requested_update_mode=False)
    verified = verify_authority(case['token'],kind='license',context=accepted_context,trust=evidence.trust,schemas=schemas)
    assert verified.accepted
    ref = reference_from_verified(verified.authority)
    selected = replace(context,reference_id=ref.reference_id,compact_jws_sha256=ref.compact_jws_sha256)
    current = replace(policy,text_analysis_authority=ref)
    assert require_text_analysis(current,selected,kind='license',phase='prepare') == ref
    with pytest.raises(RuntimeAuthorizationDenied,match='update_denied'):
        require_text_analysis(current,replace(selected,activation_lineage_sha256='f'*64),kind='license',phase='prepare')


def test_closed_context_and_owned_claims(basis):
    _,_,_,ref,context,_,_,_ = basis
    with pytest.raises(TypeError):
        ref.claims['seat_id'] = 'other'
    value = json.loads(encode_run_context(context)); value['extra'] = 1
    with pytest.raises(ValueError):
        decode_run_context(canonical(value))


def test_fallback_requires_original_licensed_major_and_explicit_selection(basis):
    _,_,_,_,context,policy,evidence,schemas = basis
    case = next(item for item in CASES if item['case_id']=='license-explicit-fallback')
    verified = verify_authority(case['token'],kind='license',context=case['context'],trust=evidence.trust,schemas=schemas)
    ref = reference_from_verified(verified.authority)
    context = replace(context,reference_id=ref.reference_id,compact_jws_sha256=ref.compact_jws_sha256,
        licensed_major_version=2,selected_major_version=1,fallback_major_version=1)
    policy = replace(policy,text_analysis_authority=ref)
    assert require_text_analysis(policy,context,kind='license',phase='prepare') == ref
    for changed in (replace(context,fallback_major_version=None),replace(context,licensed_major_version=1)):
        with pytest.raises(RuntimeAuthorizationDenied):
            require_text_analysis(policy,changed,kind='license',phase='prepare')


def test_persisted_reference_hash_corruption_is_not_authority(basis):
    auth,store,case,ref,_,_,evidence,schemas = basis
    with auth.database.transaction() as db:
        db.execute('UPDATE text_analysis_authority_references SET payload_sha256=?', ('f'*64,))
    with auth.database.read() as db:
        with pytest.raises(RuntimeAuthorizationDenied,match='reference_corrupt'):
            store.get_reference_in(db,ref.reference_id,context=case['context'],trust=evidence.trust,schemas=schemas)


def test_preview_binding_has_only_original_output_policy(basis):
    _,_,_,_,context,policy,evidence,schemas = basis
    case = next(item for item in CASES if item['case_id']=='preview-accepted')
    verified = verify_authority(case['token'],kind='preview',context=case['context'],trust=evidence.trust,schemas=schemas)
    ref = reference_from_verified(verified.authority)
    context = replace(context,reference_id=ref.reference_id,compact_jws_sha256=ref.compact_jws_sha256)
    assert require_text_analysis(replace(policy,text_analysis_authority=ref),context,kind='preview',phase='prepare') == ref
    assert set(ref.claims['output_policy']) == {'profile','version'}
    assert capability_binding(ref,context).profile_sha256 == PROFILE_SHA256['preview']


def test_staged_import_fence_cannot_authorize_execution(preview_basis):
    _,store,_,context,policy,evidence,lifecycle = preview_basis
    with store.transition_fence(policy,context,kind='preview',phase='prepare',evidence=evidence,
            lifecycle=lifecycle,fixture_only=True) as held:
        assert require_text_analysis_fence(held,context,kind='preview',phase='prepare')
        with pytest.raises(RuntimeAuthorizationDenied):
            require_text_analysis_fence(held,context,kind='preview',phase='start')
    with pytest.raises(RuntimeAuthorizationDenied):
        with store.transition_fence(policy,context,kind='preview',phase='start',evidence=evidence,
                lifecycle=lifecycle,fixture_only=True):
            pytest.fail('staged reference admitted')


@pytest.mark.parametrize('changed', [None,'installation_id','permit_id','reference_id','compact_jws_sha256','issuance_id'])
def test_preview_activation_requires_exact_committed_admission(preview_basis,changed):
    auth,store,ref,context,policy,evidence,lifecycle = preview_basis
    if changed is not None:
        _import_preview_admission(store,ref,context,policy,evidence,lifecycle,replacement=changed)
    epoch = auth.read_epoch()
    with pytest.raises(RuntimeAuthorizationDenied,match='canonical_admission_mismatch'):
        store.activate_reference(policy,context,kind='preview',evidence=evidence,lifecycle=lifecycle,fixture_only=True)
    assert auth.read_epoch() == epoch
    with auth.database.read() as db:
        row = db.execute('SELECT state,version FROM text_analysis_authority_references WHERE reference_id=?',(ref.reference_id,)).fetchone()
        assert tuple(row) == ('staged',1)


def test_preview_import_crash_recovers_exactly_and_activation_is_idempotent(preview_basis):
    auth,store,ref,context,policy,evidence,lifecycle = preview_basis
    _import_preview_admission(store,ref,context,policy,evidence,lifecycle)
    # The committed import survives a fresh facade while auth remains staged.
    store = TextAnalysisAuthorityStore(auth)
    epoch = auth.read_epoch().value
    with pytest.raises(RuntimeAuthorizationDenied):
        with store.transition_fence(policy,context,kind='preview',phase='start',evidence=evidence,
                lifecycle=lifecycle,fixture_only=True):
            pytest.fail('staged reference admitted')
    activated = store.activate_reference(policy,context,kind='preview',evidence=evidence,lifecycle=lifecycle,fixture_only=True)
    assert activated.activated
    assert activated.policy.authorization_epoch.value == epoch + 1
    assert activated.evidence.reference_version == 2
    assert activated.policy.text_analysis_fence is None
    with store.transition_fence(activated.policy,context,kind='preview',phase='start',evidence=activated.evidence,
            lifecycle=lifecycle,fixture_only=True) as current:
        assert require_text_analysis_fence(current,context,kind='preview',phase='start') == ref
    retried = store.activate_reference(activated.policy,context,kind='preview',evidence=activated.evidence,
        lifecycle=lifecycle,fixture_only=True)
    assert not retried.activated
    assert retried.evidence == activated.evidence
    assert auth.read_epoch().value == epoch + 1
    with lifecycle.canonical_database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0] == 'available'


def test_uncommitted_preview_import_is_not_activation_evidence(preview_basis):
    _,store,ref,context,policy,evidence,lifecycle = preview_basis
    with store.transition_fence(policy,context,kind='preview',phase='import',evidence=evidence,
            lifecycle=lifecycle,fixture_only=True) as held:
        with lifecycle.canonical_database.transaction() as db:
            db.execute("INSERT INTO text_analysis_preview_admissions VALUES(?,?,?,?,?,'available',NULL,NULL)",
                (context.installation_id,ref.object_id,ref.reference_id,ref.compact_jws_sha256,ref.issuance_id))
            with pytest.raises(RuntimeAuthorizationDenied,match='canonical_admission_mismatch'):
                store.activate_reference_in(held,context,kind='preview',lifecycle=lifecycle)


def test_activation_rechecks_original_lifecycle_and_revocation(preview_basis):
    auth,store,ref,context,policy,evidence,lifecycle = preview_basis
    _import_preview_admission(store,ref,context,policy,evidence,lifecycle)
    with store.transition_fence(policy,context,kind='preview',phase='import',evidence=evidence,
            lifecycle=lifecycle,fixture_only=True) as held:
        substituted = FixtureTextAnalysisLifecycle(auth.database,lifecycle.canonical_database)
        with pytest.raises(RuntimeAuthorizationDenied,match='owner_mismatch'):
            store.activate_reference_in(held,context,kind='preview',lifecycle=substituted)
    store.revoke(ref.reference_id)
    policy = replace(policy,authorization_epoch=auth.read_epoch())
    evidence = replace(evidence,authorization_epoch=auth.read_epoch().value,reference_version=2)
    with pytest.raises(RuntimeAuthorizationDenied,match='reference_unavailable'):
        store.activate_reference(policy,context,kind='preview',evidence=evidence,lifecycle=lifecycle,fixture_only=True)
    with auth.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_authority_references WHERE reference_id=?',(ref.reference_id,)).fetchone()[0] == 'revoked'


def test_active_retry_still_rechecks_durable_admission(preview_basis):
    auth,store,ref,context,policy,evidence,lifecycle = preview_basis
    _import_preview_admission(store,ref,context,policy,evidence,lifecycle)
    activated = store.activate_reference(policy,context,kind='preview',evidence=evidence,lifecycle=lifecycle,fixture_only=True)
    other = CanonicalSQLite(lifecycle.canonical_database.path.parent/'other-canonical.sqlite3')
    MigrationRunner(other,migrations_dir=Path(__file__).resolve().parents[1]/'app/persistence/sql',
        lock_path=other.path.parent/'other.lock',backups_dir=other.path.parent/'other-backups').run()
    with pytest.raises(RuntimeAuthorizationDenied,match='canonical_admission_mismatch'):
        store.activate_reference(activated.policy,context,kind='preview',evidence=activated.evidence,
            lifecycle=FixtureTextAnalysisLifecycle(auth.database,other),fixture_only=True)


def test_live_fence_requires_exact_canonical_lifecycle_owner(basis,tmp_path):
    auth,store,_,_,context,policy,evidence,_ = basis
    owned = CanonicalSQLite(tmp_path/'owned.sqlite3')
    other = CanonicalSQLite(tmp_path/'other.sqlite3')
    same_path_other_owner = CanonicalSQLite(owned.path)
    lifecycle = FixtureTextAnalysisLifecycle(auth.database,owned)
    with store.transition_fence(policy,context,kind='license',phase='start',evidence=evidence,
            lifecycle=lifecycle,fixture_only=True) as held:
        assert require_text_analysis_fence(held,context,kind='license',phase='commit',canonical_database=owned)
        assert held.text_analysis_fence.owns_canonical_database(owned)
        for unowned in (other,same_path_other_owner,object()):
            assert not held.text_analysis_fence.owns_canonical_database(unowned)
            with pytest.raises(RuntimeAuthorizationDenied,match='canonical_owner_mismatch'):
                require_text_analysis_fence(held,context,kind='license',phase='commit',canonical_database=unowned)
        stale = replace(held,authorization_epoch=type(held.authorization_epoch)(held.authorization_epoch.value+1))
        with pytest.raises(RuntimeAuthorizationDenied,match='fence_missing'):
            require_text_analysis_fence(stale,context,kind='license',phase='commit',canonical_database=owned)
    assert not held.text_analysis_fence.owns_canonical_database(owned)
    with pytest.raises(RuntimeAuthorizationDenied,match='fence_missing'):
        require_text_analysis_fence(held,context,kind='license',phase='commit',canonical_database=owned)


def test_fence_without_canonical_lifecycle_owner_cannot_authorize_canonical_write(basis,tmp_path):
    auth,store,_,_,context,policy,evidence,_ = basis
    canonical_db = CanonicalSQLite(tmp_path/'canonical.sqlite3')
    with store.transition_fence(policy,context,kind='license',phase='start',evidence=evidence,
            lifecycle=FixtureTextAnalysisLifecycle(auth.database),fixture_only=True) as held:
        with pytest.raises(RuntimeAuthorizationDenied,match='canonical_owner_mismatch'):
            require_text_analysis_fence(held,context,kind='license',phase='commit',canonical_database=canonical_db)
