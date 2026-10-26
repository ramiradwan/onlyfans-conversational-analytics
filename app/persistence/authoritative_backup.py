"""Encrypted matched AUTH/CANONICAL snapshots and offline journaled restore."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from app.persistence import sqlite_api as sqlite3
from app.persistence.authoritative_pair import (
    AuthoritativePairOwner, PairIdentity, RESTORE_JOURNAL_FILENAME, audit_crosslinks, pair_identity_in,
    RuntimeRootLease, FixtureAuthoritativeRootLease,
)
from app.persistence.database import AuthSQLite, CanonicalSQLite, open_encrypted_sqlite
from app.persistence.backup import (
    SQLiteBackupError, BackupManifest, _canonical_high_water, _witnesses, _manifest_path,
    _key_path, _identity_preflight, _stage_verified_file_unlocked, _publish_staged_file,
    _exclusive_targets, _verify_database, create_online_backup, verify_backup,
    _pinned_read_snapshot, _require_exclusive,
)
from app.persistence.private_files import (
    reject_path_aliases, private_file_identity, apply_private_file_security,
    apply_private_directory_security, file_sha256, sync_directory, sync_file,
)
from app.security.local_data_key import protect_local_secret, unprotect_local_secret, LocalDataKeyError, database_key

SET_MANIFEST_FILENAME = 'authoritative-set.dpapi'
_PURPOSE = 'authoritative-backup-set:v1'
_JOURNAL_PURPOSE = 'authoritative-restore:v1'
_MAX_METADATA = 65536
SECURITY_PROJECTION_VERSION = 'auth-security-projection.v1'
_OFFLINE_OWNER_SEAL = object()
# The checked migration catalog binds all columns of this compiled inventory.
# Conservatively include presentation records which can own consumed entry,
# continuation or proof state. Unknown tables cannot silently become exclusions.
SECURITY_TABLES = (
    'auth_store_identity','installation_key_reference','auth_revocation_state',
    'verified_grant_references','hosted_grant_tombstones','webauthn_credentials',
    'agent_pairings','agent_pairing_grants','companion_pairing_generations','companion_pairing_windows',
    'bridge_sessions','bridge_session_grants','auth_challenges','runtime_tickets','runtime_ticket_grants',
    'auth_revocation_bindings','authorized_account_bindings','authorized_account_binding_grants',
    'capability_license_references','capability_license_pending_deliveries',
    'capability_license_redemption_journal','capability_license_activation_deliveries',
    'text_analysis_authority_references','nlp_dependency_authority','signing_publication_authority',
    'desktop_authentication_commands','desktop_authentication_closures','desktop_browser_ceremonies',
    'desktop_pairing_operations','desktop_activation_sources','desktop_admission_entries',
    'desktop_admission_sessions','desktop_authentication_projections','desktop_browser_observations',
    'desktop_browser_tasks','desktop_decision_projections','desktop_presentations',
    'desktop_setup_journeys','desktop_setup_operations','first_enrollment_contexts',
    'installation_continuation_contexts','onboarding_journeys','onboarding_native_selection_receipts',
    'onboarding_presentation_retirements','onboarding_presentation_sessions','onboarding_progress_outbox',
    'onboarding_transfer_intents','onboarding_transfer_proofs','onboarding_transfer_relays',
    'onboarding_uncertain_receipts','onboarding_workspace_recovery','provisioning_browser_sessions',
    'provisioning_candidates','provisioning_claim_submissions','runtime_activation_workspace_receipts',
)
_NON_SECURITY_COLUMNS = {
    'authoritative_pair':('singleton','auth_store_id','canonical_store_id','installation_id','incarnation','valid'),
    'authorization_epoch':('singleton','value'),
    'schema_migrations':('version','name','checksum','applied_at'),
    '__backup_metadata':('metadata_key','metadata_value'),
    'nlp_dependency_profiles':('digest','document','closure_digest','state'),
    'nlp_dependency_operations':('operation_id','document'),
}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',',':'), allow_nan=False).encode('utf-8')


def _cell(value):
    if type(value) is bytes:
        return ['bytes',value.hex()]
    if value is None or type(value) in (str,int,float):
        return [type(value).__name__,value]
    raise SQLiteBackupError('authoritative_row_type_invalid')


def _rows_digest(db, tables):
    digest = hashlib.sha256()
    for table in tables:
        # Names originate solely in compiled inventories or sqlite_master.
        quoted = '"' + table.replace('"','""') + '"'
        cursor = db.execute('SELECT * FROM ' + quoted)
        columns = [item[0] for item in cursor.description]
        head = _json([table,columns])
        digest.update(len(head).to_bytes(8,'big')); digest.update(head)
        # Row digests bound memory and retain duplicate multiplicity.
        rows = sorted(hashlib.sha256(_json([_cell(v) for v in row])).digest() for row in cursor)
        for row in rows:
            digest.update(row)
        digest.update(len(rows).to_bytes(8,'big'))
    return digest.hexdigest()


def auth_high_water(db):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' AND name<>'__backup_metadata' ORDER BY name")]
    return {'schema':'auth-high-water.v1','rows_sha256':_rows_digest(db,tables)}


def security_projection(db):
    _verify_database(db,'auth')
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
    if tables - _NON_SECURITY_COLUMNS.keys() != set(SECURITY_TABLES):
        raise SQLiteBackupError('authoritative_security_inventory_invalid')
    for table in tables & _NON_SECURITY_COLUMNS.keys():
        if tuple(r[1] for r in db.execute('PRAGMA table_info('+table+')')) != _NON_SECURITY_COLUMNS[table]:
            raise SQLiteBackupError('authoritative_security_inventory_invalid')
    for table in ('nlp_dependency_authority','signing_publication_authority'):
        row = db.execute('SELECT state_json,state_sha256 FROM ' + table + ' WHERE singleton=1').fetchone()
        if row is None or ((row[0] is None) != (row[1] is None)) or (row[0] is not None and hashlib.sha256(row[0].encode('utf-8')).hexdigest() != row[1]):
            raise SQLiteBackupError('authoritative_security_state_invalid')
    return {'schema':SECURITY_PROJECTION_VERSION,'sha256':_rows_digest(db,SECURITY_TABLES)}


def _release_identity():
    from app.core.resource_paths import resource_path
    from contracts.loader import verify_snapshot_integrity
    from app.security.grant_verifier import load_pinned_trust_set
    if getattr(sys,'frozen',False):
        from app.core.desktop_release import current_desktop_release, DesktopReleaseError, DesktopRelease
        try:
            release=current_desktop_release()
        except DesktopReleaseError:
            raise SQLiteBackupError('authoritative_release_unavailable') from None
        if type(release) is not DesktopRelease:
            raise SQLiteBackupError('authoritative_release_unavailable')
        product={'kind':'desktop-release','descriptor':asdict(release)}
    else:
        product={'kind':'source-catalog','identity':'source-catalog/v1'}
    root = resource_path('contracts')
    verify_snapshot_integrity(root)
    return {'contracts_sha256':file_sha256(root / 'manifest.json'),
            'consumer_pin_sha256':file_sha256(root / 'consumer-pin.json'),
            'product':product,
            'grant_trust_sha256':hashlib.sha256(_json(load_pinned_trust_set('production/grant-profile-v1/trust-set.json'))).hexdigest()}


def _file_record(path):
    before = private_file_identity(path,repair_permissions=False)
    record = {'bytes':path.stat().st_size,'sha256':file_sha256(path)}
    if private_file_identity(path,repair_permissions=False) != before:
        raise SQLiteBackupError('authoritative_file_changed')
    return record


def _write_protected(path, value, purpose, *, replace=False):
    raw = _json(value)
    if len(raw) > _MAX_METADATA:
        raise SQLiteBackupError('authoritative_metadata_oversize')
    payload = protect_local_secret(raw,purpose=purpose)
    path = reject_path_aliases(path)
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.tmp')
    if path.exists() and not replace:
        raise SQLiteBackupError('authoritative_metadata_exists')
    descriptor = os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'wb') as stream:
        apply_private_file_security(temporary)
        stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary,path)
    private_file_identity(path,repair_permissions=False)
    sync_directory(path.parent)


def _read_protected(path,purpose):
    before = private_file_identity(path,repair_permissions=False)
    with path.open('rb') as stream:
        payload = stream.read(_MAX_METADATA*2+1)
    if len(payload)>_MAX_METADATA*2 or private_file_identity(path,repair_permissions=False)!=before:
        raise SQLiteBackupError('authoritative_metadata_changed')
    try:
        plain = unprotect_local_secret(payload,purpose=purpose)
    except LocalDataKeyError:
        raise SQLiteBackupError('authoritative_metadata_unavailable') from None
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:
                raise ValueError('duplicate')
            result[key]=value
        return result
    try:
        if len(plain)>_MAX_METADATA:
            raise ValueError()
        value=json.loads(plain,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if _json(value)!=plain:
            raise ValueError()
        return value
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise SQLiteBackupError('authoritative_metadata_invalid') from None


@dataclass(frozen=True, slots=True)
class AuthoritativeSetManifest:
    schema: str
    set_id: str
    pair: PairIdentity
    security: dict
    release: dict
    files: dict
    high_water: dict
    schema_catalogs: dict


@dataclass(frozen=True, slots=True, init=False)
class OfflineAuthoritativeRestoreOwner:
    """Existing-key facades without application constructors or database opens."""
    root: Path
    root_lease: RuntimeRootLease | FixtureAuthoritativeRootLease
    auth_database: AuthSQLite
    canonical_database: CanonicalSQLite
    installation_id: str

    def __init__(self, seal, *, root_lease, auth_database, canonical_database, installation_id):
        if seal is not _OFFLINE_OWNER_SEAL:
            raise SQLiteBackupError('authoritative_offline_owner_invalid')
        for key,value in (('root',root_lease.root),('root_lease',root_lease),
                ('auth_database',auth_database),('canonical_database',canonical_database),
                ('installation_id',installation_id)):
            object.__setattr__(self,key,value)

    @classmethod
    def from_existing(cls, *, root_lease, auth_path, canonical_path, installation_id):
        import re
        if (type(root_lease) not in (RuntimeRootLease,FixtureAuthoritativeRootLease)
                or type(installation_id) is not str
                or re.fullmatch('[A-Za-z0-9][A-Za-z0-9._~-]{0,127}',installation_id) is None):
            raise SQLiteBackupError('authoritative_offline_owner_invalid')
        root_lease.assert_current(root_lease.root,allow_restore=True)
        paths=_identity_preflight(auth=auth_path,canonical=canonical_path)
        keys={}
        for name,path in paths.items():
            if not path.is_relative_to(root_lease.root) or not path.parent.is_dir() or not path.is_file():
                raise SQLiteBackupError('authoritative_current_store_missing')
            private_file_identity(path,repair_permissions=False)
            keys[name]=database_key(path,name,create_if_missing=False)
        return cls(_OFFLINE_OWNER_SEAL,root_lease=root_lease,
            auth_database=AuthSQLite(paths['auth'],encryption_key=keys['auth']),
            canonical_database=CanonicalSQLite(paths['canonical'],encryption_key=keys['canonical']),
            installation_id=installation_id)

    def open_current(self, name):
        """Only after the coordinator holds both offline lifecycle exclusions."""
        self.root_lease.assert_current(self.root,allow_restore=True)
        database={'auth':self.auth_database,'canonical':self.canonical_database}[name]
        _require_exclusive(database.path)
        # The coordinator first proves all native connections and WAL absent.
        # Immutable mode then avoids header changes and new WAL/SHM files.
        return open_encrypted_sqlite(database.path,database._encryption_key,read_only=True,immutable=True)


def _catalog(db):
    return [[r[0],r[1],r[2]] for r in db.execute('SELECT version,name,checksum FROM schema_migrations ORDER BY version')]


def create_authoritative_backup(owner:AuthoritativePairOwner,directory)->AuthoritativeSetManifest:
    if type(owner) is not AuthoritativePairOwner:
        raise SQLiteBackupError('authoritative_owner_missing')
    owner.root_lease.assert_current(owner.root)
    directory=reject_path_aliases(directory)
    _identity_preflight(auth=owner.auth_database.path,canonical=owner.canonical_database.path,
                        destination=directory)
    directory.mkdir(parents=True,exist_ok=False)
    apply_private_directory_security(directory)
    # Both snapshots are forced while BOTH writers are excluded. Copy only
    # after releasing those writers, from these exact retained read snapshots.
    with ExitStack() as reads:
        with owner.auth_database.dependency_authority_gate:
            with owner.auth_database.transaction() as aw, owner.canonical_database.transaction() as cw:
                pair=owner.validate_in(aw,cw)
                audit_crosslinks(aw,cw,owner.installation_id)
                if aw.execute("SELECT 1 FROM text_analysis_authority_references WHERE kind='preview' AND state='staged'").fetchone():
                    raise SQLiteBackupError('authoritative_preview_import_incomplete')
                auth_snapshot=reads.enter_context(_pinned_read_snapshot(owner.auth_database))
                canonical_snapshot=reads.enter_context(_pinned_read_snapshot(owner.canonical_database))
                ar,cr=auth_snapshot.connection,canonical_snapshot.connection
                owner.validate_read_in(ar,cr)
            # Do not copy a connection which itself owns an active writer.
        security=security_projection(ar)
        release=_release_identity()
        catalogs={'auth':_catalog(ar),'canonical':_catalog(cr)}
        waters={'auth':auth_high_water(ar),'canonical':_canonical_high_water(cr)}
        for name,database,source in (('auth',owner.auth_database,auth_snapshot),('canonical',owner.canonical_database,canonical_snapshot)):
            create_online_backup(database,directory/(name+'.sqlite3'),store_name=name,
                high_water_reader=auth_high_water if name=='auth' else _canonical_high_water,_pinned_source=source)
    files={name:_file_record(directory/name) for name in (
        'auth.sqlite3','auth.sqlite3.manifest.json','auth.sqlite3.key.dpapi',
        'canonical.sqlite3','canonical.sqlite3.manifest.json','canonical.sqlite3.key.dpapi')}
    result=AuthoritativeSetManifest('authoritative-backup-set.v1',uuid4().hex,pair,security,release,files,waters,catalogs)
    _write_protected(directory/SET_MANIFEST_FILENAME,asdict(result),_PURPOSE)
    return result


def verify_authoritative_backup(directory)->AuthoritativeSetManifest:
    directory=reject_path_aliases(directory)
    value=_read_protected(directory/SET_MANIFEST_FILENAME,_PURPOSE)
    if type(value) is not dict or set(value)!={'schema','set_id','pair','security','release','files','high_water','schema_catalogs'} or value['schema']!='authoritative-backup-set.v1':
        raise SQLiteBackupError('authoritative_set_invalid')
    import re
    if type(value['set_id']) is not str or re.fullmatch('[0-9a-f]{32}',value['set_id']) is None:
        raise SQLiteBackupError('authoritative_set_invalid')
    names={'auth.sqlite3','auth.sqlite3.manifest.json','auth.sqlite3.key.dpapi','canonical.sqlite3','canonical.sqlite3.manifest.json','canonical.sqlite3.key.dpapi'}
    if type(value['files']) is not dict or set(value['files'])!=names or {p.name for p in directory.iterdir()}!=names|{SET_MANIFEST_FILENAME}:
        raise SQLiteBackupError('authoritative_set_members_invalid')
    for name,record in value['files'].items():
        if _file_record(directory/name)!=record:
            raise SQLiteBackupError('authoritative_set_hash_mismatch')
    if value['release']!=_release_identity():
        raise SQLiteBackupError('authoritative_release_mismatch')
    manifests={name:verify_backup(directory/(name+'.sqlite3'),expected_store=name) for name in ('auth','canonical')}
    from app.persistence.backup import _backup_key
    from app.persistence.database import open_encrypted_sqlite
    with ExitStack() as stack:
        connections=[]
        for name in ('auth','canonical'):
            db=open_encrypted_sqlite(directory/(name+'.sqlite3'),_backup_key(directory/(name+'.sqlite3'),name),read_only=True)
            stack.callback(db.close); connections.append(db)
            if manifests[name].high_water!=value['high_water'][name] or _catalog(db)!=value['schema_catalogs'][name]:
                raise SQLiteBackupError('authoritative_set_rows_mismatch')
        a,c=connections
        pair=pair_identity_in(a,c,value['pair']['installation_id'])
        if asdict(pair)!=value['pair'] or security_projection(a)!=value['security']:
            raise SQLiteBackupError('authoritative_set_identity_mismatch')
        audit_crosslinks(a,c,pair.installation_id)
    return AuthoritativeSetManifest(value['schema'],value['set_id'],pair,value['security'],value['release'],value['files'],value['high_water'],value['schema_catalogs'])


def restore_authoritative_backup(owner:OfflineAuthoritativeRestoreOwner,directory,*,resume=False,now=None)->PairIdentity:
    """Offline only: complete the exact journal, or leave it blocking startup."""
    if type(owner) is not OfflineAuthoritativeRestoreOwner:
        raise SQLiteBackupError('authoritative_offline_owner_missing')
    owner.root_lease.assert_current(owner.root,allow_restore=True)
    directory=reject_path_aliases(directory)
    manifest=verify_authoritative_backup(directory)
    if manifest.pair.installation_id!=owner.installation_id:
        raise SQLiteBackupError('authoritative_installation_mismatch')
    journal_path=owner.root/RESTORE_JOURNAL_FILENAME
    if journal_path.exists()!=resume:
        raise SQLiteBackupError('authoritative_restore_resume_required')
    paths={'auth':owner.auth_database.path,'canonical':owner.canonical_database.path}
    journal=None
    with owner.auth_database.dependency_authority_gate, _exclusive_targets(list(paths.values())):
        if resume:
            journal=_read_protected(journal_path,_JOURNAL_PURPOSE)
            expected={'schema','source','set_manifest_sha256','set_id','pair','stages','targets','security'}
            if (type(journal) is not dict or set(journal)!=expected or journal['schema']!='authoritative-restore.v1'
                    or journal['source']!=str(directory) or journal['set_id']!=manifest.set_id
                    or journal['set_manifest_sha256']!=file_sha256(directory/SET_MANIFEST_FILENAME)
                    or journal['targets']!={k:str(v) for k,v in paths.items()} or journal['security']!=manifest.security):
                raise SQLiteBackupError('authoritative_restore_journal_mismatch')
            current=owner.open_current('auth')
            try:
                if security_projection(current)!=manifest.security:
                    raise SQLiteBackupError('authoritative_security_incompatible')
            finally:
                current.close()
        else:
            # No permissive merge of current security state. Missing AUTH is
            # ambiguous; same-installation restores require its original key.
            if not all(path.is_file() for path in paths.values()):
                raise SQLiteBackupError('authoritative_current_store_missing')
            current=owner.open_current('auth')
            try:
                if security_projection(current)!=manifest.security:
                    raise SQLiteBackupError('authoritative_security_incompatible')
            finally:
                current.close()
            from app.persistence.retention_restore import _merge_authority
            current=owner.open_current('canonical')
            try:
                policies=[dict(row) for row in current.execute('SELECT * FROM archive_policies')]
                barriers=[dict(row) for row in current.execute('SELECT * FROM deletion_barriers')]
            finally:
                current.close()
            new_pair=PairIdentity(manifest.pair.auth_store_id,manifest.pair.canonical_store_id,owner.installation_id,uuid4().hex)
            stages={}
            for name,database in (('auth',owner.auth_database),('canonical',owner.canonical_database)):
                stage=paths[name].with_name('.'+paths[name].name+'.'+uuid4().hex+'.authoritative-restore.tmp')
                source=directory/(name+'.sqlite3')
                single=verify_backup(source,expected_store=name)
                _identity_preflight(source=source,destination=paths[name],stage=stage)
                _stage_verified_file_unlocked(source,stage,single,database,_preserve_pair=True)
                db=database.open_detached(stage)
                try:
                    db.execute('PRAGMA foreign_keys=ON'); db.execute('BEGIN IMMEDIATE')
                    if name=='canonical':
                        _merge_authority(db,policies,barriers)
                        from app.persistence.contributions import reconcile_restored
                        reconcile_restored(db,now or datetime.now(timezone.utc))
                    db.execute('UPDATE authoritative_pair SET incarnation=?,valid=1',(new_pair.incarnation,))
                    db.commit(); _verify_database(db,name)
                finally:
                    db.close()
                sync_file(stage)
                stages[name]={'path':str(stage),**_file_record(stage)}
            journal={'schema':'authoritative-restore.v1','source':str(directory),
                'set_manifest_sha256':file_sha256(directory/SET_MANIFEST_FILENAME),'set_id':manifest.set_id,
                'pair':asdict(new_pair),'stages':stages,'targets':{k:str(v) for k,v in paths.items()},'security':manifest.security}
            _write_protected(journal_path,journal,_JOURNAL_PURPOSE)
        # Verify BOTH candidates before any publication, including a candidate
        # already published by an interrupted earlier invocation.
        chosen={}
        with ExitStack() as stack:
            conns=[]
            for name,database in (('auth',owner.auth_database),('canonical',owner.canonical_database)):
                record=journal['stages'][name]; stage=reject_path_aliases(record['path'])
                if stage.parent!=paths[name].parent or not stage.name.startswith('.'+paths[name].name+'.') or not stage.name.endswith('.authoritative-restore.tmp'):
                    raise SQLiteBackupError('authoritative_restore_stage_invalid')
                chosen[name]=stage if stage.exists() else paths[name]
                if _file_record(chosen[name])!={k:record[k] for k in ('bytes','sha256')}:
                    raise SQLiteBackupError('authoritative_restore_stage_changed')
                db=database.open_detached(chosen[name],read_only=True); stack.callback(db.close); conns.append(db)
                _verify_database(db,name)
            a,c=conns
            if security_projection(a)!=manifest.security or asdict(pair_identity_in(a,c,owner.installation_id))!=journal['pair']:
                raise SQLiteBackupError('authoritative_restore_stage_mismatch')
            audit_crosslinks(a,c,owner.installation_id)
        for name in ('auth','canonical'):
            owner.root_lease.assert_current(owner.root,allow_restore=True)
            if chosen[name]!=paths[name]:
                _publish_staged_file(chosen[name],paths[name])
        # A durable journal remains until both exact, re-keyed publications
        # and their mirrored fresh incarnation are established.
        for name in ('auth','canonical'):
            if _file_record(paths[name])!={k:journal['stages'][name][k] for k in ('bytes','sha256')}:
                raise SQLiteBackupError('authoritative_restore_publish_changed')
        journal_path.unlink(); sync_directory(owner.root)
        return PairIdentity(**journal['pair'])
