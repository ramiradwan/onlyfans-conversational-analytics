"""Matched encrypted snapshots, security compatibility and interrupted restore."""
from dataclasses import asdict
import hashlib

import pytest

from app.persistence import authoritative_backup as module
from app.persistence.authoritative_backup import (
    create_authoritative_backup, verify_authoritative_backup, restore_authoritative_backup,
    security_projection, auth_high_water, SET_MANIFEST_FILENAME,
    OfflineAuthoritativeRestoreOwner,
)
from app.persistence.authoritative_pair import RESTORE_JOURNAL_FILENAME
from app.persistence.backup import SQLiteBackupError, create_online_backup, restore_backup, backup_canonical_database
from app.security.runtime_policy import RuntimeAuthorizationDenied
from tests.test_authoritative_pair import make_pair
from tests.test_text_analysis_jobs import (
    execution, contribution_data, authority_basis, prepare, activate, preview_result,
)

pytestmark = [pytest.mark.ci_tier('integration'), pytest.mark.windows_compat]


def offline(owner):
    return OfflineAuthoritativeRestoreOwner.from_existing(root_lease=owner.root_lease,
        auth_path=owner.auth_database.path,canonical_path=owner.canonical_database.path,
        installation_id=owner.installation_id)


def test_matched_snapshot_is_encrypted_closed_and_restores_new_incarnation(tmp_path):
    owner=make_pair(tmp_path/'live')
    before=owner.establish()
    manifest=create_authoritative_backup(owner,tmp_path/'set')
    expected_members=set(manifest.files)|{SET_MANIFEST_FILENAME}
    assert {path.name for path in (tmp_path/'set').iterdir()}==expected_members
    assert verify_authoritative_backup(tmp_path/'set')==manifest
    assert {path.name for path in (tmp_path/'set').iterdir()}==expected_members
    assert manifest.pair==before
    for name in ('auth','canonical'):
        assert not (tmp_path/'set'/(name+'.sqlite3')).read_bytes().startswith(b'SQLite format 3')
    restored=restore_authoritative_backup(offline(owner),tmp_path/'set')
    assert {path.name for path in (tmp_path/'set').iterdir()}==expected_members
    assert restored.incarnation!=before.incarnation
    assert (restored.auth_store_id,restored.canonical_store_id)==(before.auth_store_id,before.canonical_store_id)
    assert owner.establish()==restored
    assert not (owner.root/RESTORE_JOURNAL_FILENAME).exists()


def test_pinned_snapshots_survive_later_auth_and_canonical_commits(tmp_path,monkeypatch):
    owner=make_pair(tmp_path/'live')
    original=module.create_online_backup
    calls=[]
    def changed(database,*args,**kwargs):
        calls.append(kwargs['store_name'])
        if len(calls)==1:
            # Snapshot readers remain open, but neither copying source owns a
            # writer. These commits prove both the lock release and pinning.
            with owner.auth_database.transaction() as a:
                a.execute("INSERT INTO auth_revocation_state VALUES('principal','later',1,'now','test')")
            with owner.canonical_database.transaction() as c:
                c.execute("INSERT INTO knowledge_retirement_epochs VALUES('later','',1)")
        return original(database,*args,**kwargs)
    monkeypatch.setattr(module,'create_online_backup',changed)
    manifest=create_authoritative_backup(owner,tmp_path/'set')
    assert verify_authoritative_backup(tmp_path/'set')==manifest
    with owner.auth_database.read() as a:
        assert security_projection(a)!=manifest.security
    with pytest.raises(SQLiteBackupError,match='security_incompatible'):
        restore_authoritative_backup(offline(owner),tmp_path/'set')


@pytest.mark.parametrize('mutation',['revocation','withdrawal','module_revocation','counter'])
def test_security_projection_rejects_newer_denial_or_counter_without_publish(tmp_path,mutation):
    # The actual original module fixture is imported only for its own case.
    if mutation=='module_revocation':
        from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
        from app.security.text_analysis_authority import fixture_trust
        from app.security.text_analysis_contract import TextAnalysisSchemas
        from tests.test_text_analysis_execution_authority import CASES
        case=next(c for c in CASES if c['case_id']=='license-accepted')
        owner=make_pair(tmp_path/'live',installation_id=case['payload']['installation_id'])
        authority=TextAnalysisAuthorityStore(owner.authentication)
        ref=authority.import_verified_reference(case['token'],kind='license',context=case['context'],
            trust=fixture_trust(environment='test'),schemas=TextAnalysisSchemas())
    else:
        owner=make_pair(tmp_path/'live')
    create_authoritative_backup(owner,tmp_path/'set')
    with owner.auth_database.transaction() as a:
        if mutation=='revocation':
            a.execute("INSERT INTO auth_revocation_state VALUES('principal','person',1,'now','test')")
        elif mutation=='withdrawal':
            import json
            state=json.loads(a.execute('SELECT state_json FROM nlp_dependency_authority').fetchone()[0])
            state['withdrawals']=['a'*64]
            raw=json.dumps(state,separators=(',',':'),sort_keys=True)
            a.execute('UPDATE nlp_dependency_authority SET state_json=?,state_sha256=?',(raw,hashlib.sha256(raw.encode()).hexdigest()))
        elif mutation=='module_revocation':
            a.execute("UPDATE text_analysis_authority_references SET state='revoked',version=version+1 WHERE reference_id=?",(ref.reference_id,))
        else:
            a.execute("INSERT INTO webauthn_credentials VALUES('credential','person','issuer','subject','installation',x'01',1,'now',NULL)")
    before=owner.canonical_database.path.read_bytes()
    with pytest.raises(SQLiteBackupError,match='security_incompatible'):
        restore_authoritative_backup(offline(owner),tmp_path/'set')
    assert owner.canonical_database.path.read_bytes()==before
    assert not (owner.root/RESTORE_JOURNAL_FILENAME).exists()


@pytest.mark.parametrize('role',['desktop','browser_task'])
def test_actual_admission_consumption_refuses_older_restore_without_epoch_change(tmp_path,role):
    from uuid import uuid4
    from app.onboarding.admission import ScopedAdmissions, AdmissionRefused
    owner=make_pair(tmp_path/'live')
    admissions=ScopedAdmissions(owner.authentication,stage='runtime')
    code=admissions.prepare_desktop(str(uuid4()))
    if role=='browser_task':
        desktop=admissions.consume(code,role='desktop')
        _,code=admissions.prepare_task(desktop.identifier,generation=desktop.generation,
            journey_id=desktop.journey_id,operation_id=str(uuid4()),purpose='browser.authentication',
            scope_digest='a'*64,source_revision=0,deadline=owner.authentication._now().timestamp()+600)
    epoch=owner.authentication.read_epoch()
    create_authoritative_backup(owner,tmp_path/'set')
    session=admissions.consume(code,role=role)
    assert owner.authentication.read_epoch()==epoch
    before=owner.canonical_database.path.read_bytes()
    with pytest.raises(SQLiteBackupError,match='security_incompatible'):
        restore_authoritative_backup(offline(owner),tmp_path/'set')
    assert owner.canonical_database.path.read_bytes()==before
    assert not (owner.root/RESTORE_JOURNAL_FILENAME).exists()
    assert admissions.require(session.identifier,role=role)==session
    with pytest.raises(AdmissionRefused):
        admissions.consume(code,role=role)


def test_unclassified_auth_table_refuses_projection_but_cache_availability_is_excluded(tmp_path):
    owner=make_pair(tmp_path/'live')
    with owner.auth_database.read() as db:
        before=security_projection(db)
    with owner.auth_database.transaction() as db:
        db.execute("INSERT INTO nlp_dependency_profiles VALUES(?,?,?,'planned')",('a'*64,b'{}','b'*64))
    with owner.auth_database.read() as db:
        assert security_projection(db)==before
    with owner.auth_database.transaction() as db:
        db.execute('CREATE TABLE unclassified_security_state(value TEXT)')
    with owner.auth_database.read() as db:
        with pytest.raises(SQLiteBackupError,match='security_inventory_invalid'):
            security_projection(db)


@pytest.mark.parametrize('changed',['product','consumer_pin_sha256'])
def test_current_product_release_or_consumer_pin_mismatch_refuses_set(tmp_path,monkeypatch,changed):
    owner=make_pair(tmp_path/'live')
    create_authoritative_backup(owner,tmp_path/'set')
    identity=module._release_identity()
    identity[changed]=({'kind':'source-catalog','identity':'other-source-catalog/v1'}
                       if changed=='product' else '0'*64)
    monkeypatch.setattr(module,'_release_identity',lambda:identity)
    with pytest.raises(SQLiteBackupError,match='release_mismatch'):
        restore_authoritative_backup(offline(owner),tmp_path/'set')
    assert not (owner.root/RESTORE_JOURNAL_FILENAME).exists()


def test_unknown_frozen_product_release_refuses_set_identity(monkeypatch):
    from app.core import desktop_release
    monkeypatch.setattr(module.sys,'frozen',True,raising=False)
    monkeypatch.setattr(desktop_release,'current_desktop_release',lambda:None)
    with pytest.raises(SQLiteBackupError,match='release_unavailable'):
        module._release_identity()


@pytest.mark.parametrize('member',['auth.sqlite3','canonical.sqlite3.key.dpapi',SET_MANIFEST_FILENAME])
def test_changed_set_members_never_publish(tmp_path,member):
    owner=make_pair(tmp_path/'live'); create_authoritative_backup(owner,tmp_path/'set')
    file=tmp_path/'set'/member
    data=file.read_bytes(); file.write_bytes(data[:-1]+bytes([data[-1]^1]))
    with pytest.raises(SQLiteBackupError):
        restore_authoritative_backup(offline(owner),tmp_path/'set')
    assert not (owner.root/RESTORE_JOURNAL_FILENAME).exists()


def test_interrupted_first_publication_blocks_transitions_and_resumes_exact_set(tmp_path,monkeypatch):
    owner=make_pair(tmp_path/'live'); manifest=create_authoritative_backup(owner,tmp_path/'set')
    original=module._publish_staged_file
    calls=[]
    def interrupted(source,target):
        calls.append(target)
        if len(calls)==2:
            raise OSError('interrupted')
        return original(source,target)
    monkeypatch.setattr(module,'_publish_staged_file',interrupted)
    with pytest.raises(OSError):
        restore_authoritative_backup(offline(owner),tmp_path/'set')
    assert (owner.root/RESTORE_JOURNAL_FILENAME).exists()
    from app.core.startup_maintenance import inspect_retained_state, StartupRefused
    with pytest.raises(StartupRefused,match='authoritative_restore_incomplete'):
        inspect_retained_state(owner.root)
    with pytest.raises(RuntimeAuthorizationDenied):
        owner.root_lease.assert_current(owner.root)
    monkeypatch.setattr(module,'_publish_staged_file',original)
    from app.persistence.auth import SQLiteAuthenticationStore
    from app.persistence.database import LocalSQLite
    from app.persistence.migrations import MigrationRunner
    from app.persistence.authoritative_pair import FixtureAuthoritativeRootLease
    retained={name:path.read_bytes() for name,path in (
        ('auth',owner.auth_database.path),('canonical',owner.canonical_database.path))}
    def no_application_constructor(*args,**kwargs):
        pytest.fail('Offline restore opened an application or migrating connection')
    with monkeypatch.context() as patch:
        patch.setattr(SQLiteAuthenticationStore,'__init__',no_application_constructor)
        patch.setattr(MigrationRunner,'run',no_application_constructor)
        patch.setattr(LocalSQLite,'connect',no_application_constructor)
        reopened=OfflineAuthoritativeRestoreOwner.from_existing(
            root_lease=FixtureAuthoritativeRootLease(owner.root),auth_path=owner.auth_database.path,
            canonical_path=owner.canonical_database.path,installation_id=owner.installation_id)
        assert owner.auth_database.path.read_bytes()==retained['auth']
        assert owner.canonical_database.path.read_bytes()==retained['canonical']
        assert not any(path.exists() for database in (owner.auth_database,owner.canonical_database)
            for path in (database.path.with_name(database.path.name+'-wal'),database.path.with_name(database.path.name+'-shm')))
        result=restore_authoritative_backup(reopened,tmp_path/'set',resume=True)
    assert result.incarnation!=manifest.pair.incarnation
    assert owner.establish()==result


@pytest.mark.parametrize('store',['auth','canonical'])
def test_generic_single_store_restore_invalidates_pair(tmp_path,store):
    owner=make_pair(tmp_path/'live')
    source=tmp_path/'single.sqlite3'
    if store=='canonical':
        backup_canonical_database(owner.canonical_database,source)
    else:
        create_online_backup(owner.auth_database,source,store_name='auth',high_water_reader=auth_high_water)
    target=owner.auth_database if store=='auth' else owner.canonical_database
    if store=='auth':
        before=target.path.read_bytes()
        with pytest.raises(SQLiteBackupError,match='requires_matched_set'):
            restore_backup(source,target.path,expected_store=store,overwrite=True)
        assert target.path.read_bytes()==before
        assert owner.establish() is not None
        return
    restore_backup(source,target.path,expected_store=store,overwrite=True)
    with owner.auth_database.transaction() as a,owner.canonical_database.transaction() as c:
        with pytest.raises(RuntimeAuthorizationDenied):
            owner.validate_in(a,c)


def test_open_application_connections_refuse_offline_restore(tmp_path):
    owner=make_pair(tmp_path/'live'); create_authoritative_backup(owner,tmp_path/'set')
    with owner.canonical_database.read():
        with pytest.raises(SQLiteBackupError,match='in use'):
            restore_authoritative_backup(offline(owner),tmp_path/'set')
    assert not (owner.root/RESTORE_JOURNAL_FILENAME).exists()


def test_runtime_pair_owner_cannot_enter_offline_restore(tmp_path):
    owner=make_pair(tmp_path/'live')
    with pytest.raises(SQLiteBackupError,match='offline_owner_missing'):
        restore_authoritative_backup(owner,tmp_path/'absent-set')
    assert not (tmp_path/'absent-set').exists()


def test_offline_factory_never_creates_missing_target_or_parent(tmp_path):
    owner=make_pair(tmp_path/'live')
    missing=owner.root/'absent'/'canonical.sqlite3'
    with pytest.raises(SQLiteBackupError,match='current_store_missing'):
        OfflineAuthoritativeRestoreOwner.from_existing(root_lease=owner.root_lease,
            auth_path=owner.auth_database.path,canonical_path=missing,installation_id=owner.installation_id)
    assert not missing.parent.exists()


def test_retained_nonempty_auth_wal_refuses_before_immutable_inspection(tmp_path):
    owner=make_pair(tmp_path/'live')
    create_authoritative_backup(owner,tmp_path/'set')
    wal=owner.auth_database.path.with_name(owner.auth_database.path.name+'-wal')
    connection=owner.auth_database.connect()
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute("INSERT INTO auth_revocation_state VALUES('principal','retained-denial',1,'now','test')")
        connection.commit()
        retained=wal.read_bytes()
        assert len(retained)>32
    finally:
        connection.close()
    # Retain a real committed WAL cohort for the offline refusal boundary.
    # No replay, checkpoint or cleanup is an admission substitute.
    wal.write_bytes(retained)
    before=owner.auth_database.path.read_bytes()
    with pytest.raises(SQLiteBackupError,match='SQLite sidecars'):
        restore_authoritative_backup(offline(owner),tmp_path/'set')
    assert owner.auth_database.path.read_bytes()==before and wal.read_bytes()==retained
    assert not (owner.root/RESTORE_JOURNAL_FILENAME).exists()


def test_foreign_raw_and_expired_snapshots_refuse_before_creating_destination(tmp_path):
    from app.persistence.backup import _pinned_read_snapshot, _canonical_high_water
    owner=make_pair(tmp_path/'a'); other=make_pair(tmp_path/'b')
    output=tmp_path/'output.sqlite3'
    with _pinned_read_snapshot(other.canonical_database) as foreign:
        with pytest.raises(SQLiteBackupError,match='snapshot_invalid'):
            create_online_backup(owner.canonical_database,output,store_name='canonical',
                high_water_reader=_canonical_high_water,_pinned_source=foreign)
    with owner.canonical_database.transaction() as writer:
        writer.execute('PRAGMA query_only=ON')
        with pytest.raises(SQLiteBackupError,match='snapshot_invalid'):
            create_online_backup(owner.canonical_database,output,store_name='canonical',
                high_water_reader=_canonical_high_water,_pinned_source=writer)
    with _pinned_read_snapshot(owner.canonical_database) as expired:
        pass
    with pytest.raises(SQLiteBackupError,match='snapshot_invalid'):
        create_online_backup(owner.canonical_database,output,store_name='canonical',
            high_water_reader=_canonical_high_water,_pinned_source=expired)
    assert not output.exists()


def preview_pair(tmp_path,x,authority_basis):
    from app.persistence.authoritative_pair import AuthoritativePairOwner, FixtureAuthoritativeRootLease
    owner=AuthoritativePairOwner(authority_basis[0],x.repo.database,
        installation_id=x.context.installation_id,root_lease=FixtureAuthoritativeRootLease(tmp_path))
    owner.establish()
    return owner


def seal_preview(x):
    from app.analytics.nlp_preview import encode_preview
    first=prepare(x); activate(x,first)
    frame=encode_preview(preview_result(x,first,findings=True))
    with x.held() as current:
        token=x.repo.claim_attempt(first.job_id,expected_fence=0,lease_seconds=60,policy=current)
        with x.repo.database.transaction() as db:
            x.repo.commit_preview_in(db,token,frame,policy=current)
    return first,frame


def test_older_matching_available_admission_can_restore_when_auth_security_equal(tmp_path,execution,authority_basis):
    x=execution('preview'); owner=preview_pair(tmp_path,x,authority_basis)
    create_authoritative_backup(owner,tmp_path/'set')
    first,_=seal_preview(x)
    with x.repo.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0]=='spent'
    restore_authoritative_backup(offline(owner),tmp_path/'set',now=x.f.clock.now)
    with x.repo.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0]=='available'
        assert db.execute('SELECT 1 FROM text_analysis_intents WHERE job_id=?',(first.job_id,)).fetchone() is None


def test_restore_preserves_newer_source_deletion_and_spent_lifetime_receipt(tmp_path,execution,authority_basis):
    from app.persistence.retention import CreatorVaultRetention
    from tests.continuous_analytics_fixture import ACCOUNT
    x=execution('preview'); owner=preview_pair(tmp_path,x,authority_basis)
    first,frame=seal_preview(x)
    create_authoritative_backup(owner,tmp_path/'set')
    CreatorVaultRetention(x.repo.database,clock=lambda:x.f.clock.now).delete_message(ACCOUNT,'m-0-0')
    restore_authoritative_backup(offline(owner),tmp_path/'set',now=x.f.clock.now)
    with x.repo.database.read() as db:
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0]=='spent'
        assert db.execute('SELECT state FROM text_analysis_intents WHERE job_id=?',(first.job_id,)).fetchone()[0]=='result_deleted'
        assert db.execute('SELECT count(*) FROM text_analysis_preview_outputs').fetchone()[0]==0
        assert db.execute("SELECT 1 FROM account_messages WHERE message_id='m-0-0'").fetchone() is None


def test_saved_selected_output_bytes_survive_matched_restore(tmp_path,execution,authority_basis):
    x=execution('preview'); owner=preview_pair(tmp_path,x,authority_basis)
    first,frame=seal_preview(x)
    create_authoritative_backup(owner,tmp_path/'set')
    restore_authoritative_backup(offline(owner),tmp_path/'set',now=x.f.clock.now)
    with x.repo.database.read() as db:
        assert bytes(db.execute('SELECT document FROM text_analysis_preview_outputs WHERE contribution_id=?',(first.contribution_id,)).fetchone()[0])==frame
        assert db.execute('SELECT state FROM text_analysis_preview_admissions').fetchone()[0]=='spent'
