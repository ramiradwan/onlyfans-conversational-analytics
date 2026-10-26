"""Explicit lifecycle ownership and matched authoritative-store identities."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from uuid import uuid4

from app.persistence.database import AuthSQLite, CanonicalSQLite
from app.persistence.private_files import reject_path_aliases, private_file_identity
from app.security.runtime_policy import RuntimeAuthorizationDenied

RESTORE_JOURNAL_FILENAME = '.ofca-authoritative-restore.dpapi'
RUNTIME_LOCK_FILENAME = '.ofca-runtime-writer.lock'


def _deny():
    raise RuntimeAuthorizationDenied('authoritative_pair_unavailable')


class RuntimeRootLease:
    """An observation of the exact native root lock retained by startup."""
    __slots__ = ('root', '_lock', '_handle', '_identity', '_pid')

    def __init__(self, root, lock):
        from app.persistence.migrations import InstallationMigrationLock
        self.root = reject_path_aliases(root)
        if type(lock) is not InstallationMigrationLock or reject_path_aliases(lock.path) != self.root / RUNTIME_LOCK_FILENAME:
            _deny()
        handle = lock._handle
        if handle is None or handle.closed:
            _deny()
        self._lock, self._handle, self._pid = lock, handle, os.getpid()
        self._identity = private_file_identity(lock.path, repair_permissions=False)
        self.assert_current(self.root, allow_restore=True)

    @classmethod
    def from_held_lock(cls, root, lock):
        return cls(root, lock)

    def assert_current(self, root, *, allow_restore=False):
        if (reject_path_aliases(root) != self.root or os.getpid() != self._pid
                or self._lock._handle is not self._handle or self._handle.closed):
            _deny()
        if os.name == 'nt':
            import ctypes
            import msvcrt
            from app.persistence.private_files import _WindowsFileInformation
            kernel = ctypes.WinDLL('kernel32',use_last_error=True)
            info = kernel.GetFileInformationByHandle
            info.argtypes = [ctypes.c_void_p,ctypes.POINTER(_WindowsFileInformation)]
            info.restype = ctypes.c_int
            observed = _WindowsFileInformation()
            if not info(msvcrt.get_osfhandle(self._handle.fileno()),ctypes.byref(observed)):
                _deny()
            identity = (int(observed.volume),(int(observed.index_high)<<32)|int(observed.index_low))
        else:
            observed = os.fstat(self._handle.fileno())
            identity = (int(observed.st_dev),int(observed.st_ino))
        if (identity != self._identity
                or private_file_identity(self._lock.path, repair_permissions=False) != self._identity):
            _deny()
        if not allow_restore and os.path.lexists(self.root / RESTORE_JOURNAL_FILENAME):
            _deny()


class FixtureAuthoritativeRootLease:
    """Explicit process-local conformance ownership, never production admission."""
    __slots__ = ('root', '_pid')

    def __init__(self, root):
        from app.core.optional_execution import require_optional_execution
        require_optional_execution()
        from app.core.config import get_settings
        if get_settings().environment != 'test':
            _deny()
        self.root, self._pid = reject_path_aliases(root), os.getpid()

    def assert_current(self, root, *, allow_restore=False):
        from app.core.optional_execution import require_optional_execution
        require_optional_execution()
        if self._pid != os.getpid() or reject_path_aliases(root) != self.root:
            _deny()
        if not allow_restore and os.path.lexists(self.root / RESTORE_JOURNAL_FILENAME):
            _deny()


@dataclass(frozen=True, slots=True)
class PairIdentity:
    auth_store_id: str
    canonical_store_id: str
    installation_id: str
    incarnation: str


def invalidate_pair_in(connection):
    """Generic authoritative replacement cannot retain matched admission."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='authoritative_pair'").fetchone():
        connection.execute('UPDATE authoritative_pair SET valid=0')


def pair_identity_in(auth_db, canonical_db, installation_id):
    auth_id = auth_db.execute('SELECT store_id FROM auth_store_identity WHERE singleton=1').fetchone()
    canonical_id = canonical_db.execute('SELECT store_id FROM knowledge_store_identity WHERE singleton=1').fetchone()
    left = auth_db.execute('SELECT auth_store_id,canonical_store_id,installation_id,incarnation,valid FROM authoritative_pair WHERE singleton=1').fetchone()
    right = canonical_db.execute('SELECT auth_store_id,canonical_store_id,installation_id,incarnation,valid FROM authoritative_pair WHERE singleton=1').fetchone()
    if (auth_id is None or canonical_id is None or left is None or right is None
            or tuple(left) != tuple(right) or left['valid'] != 1
            or tuple(left)[:3] != (auth_id[0], canonical_id[0], installation_id)
            or any(re.fullmatch('[0-9a-f]{32}', left[key]) is None
                   for key in ('auth_store_id','canonical_store_id','incarnation'))):
        _deny()
    return PairIdentity(*tuple(left)[:4])


def audit_crosslinks(auth_db, canonical_db, installation_id):
    """Verify original signed module objects and every retained preview link."""
    from app.security.signed_objects import decode
    from app.security.text_analysis_contract import parse_json, TextAnalysisSchemas, OUTPUT_POLICY
    from app.security.text_analysis_authority import fixture_trust, verify_authority
    from app.security.text_analysis_references import reference_from_verified
    rows = auth_db.execute('SELECT * FROM text_analysis_authority_references ORDER BY reference_id').fetchall()
    verified = {}
    schemas = TextAnalysisSchemas() if rows else None
    if rows and all(r['fixture_only'] == 1 for r in rows):
        from app.core.optional_execution import require_optional_execution
        require_optional_execution()
    trust = fixture_trust(environment='test') if rows and all(r['fixture_only'] == 1 for r in rows) else None
    for row in rows:
        if trust is None:
            _deny()  # No released production module-trust provider is installed.
        try:
            token = row['compact_jws']
            payload = parse_json(decode(token.split('.')[1], 16384), maximum=16384, require_canonical=True)
            context = { 'expected_subject': payload['sub'], 'expected_capability':'text-analysis',
                'expected_target_major':payload['licensed_major_version'],
                'expected_artifact_family':'text-analysis-artifact', 'requested_update_mode':False,
                'expected_fallback_major':None }
            for name in ('organization_id','installation_id','installation_key_id','installation_key_jkt','seat_scope'):
                context['expected_' + name] = payload[name]
            if row['kind'] == 'preview':
                context['expected_output_policy'] = dict(OUTPUT_POLICY)
            decision = verify_authority(token, kind=row['kind'], context=context, trust=trust, schemas=schemas)
            if not decision.accepted or payload['installation_id'] != installation_id:
                _deny()
            reference = reference_from_verified(decision.authority)
            fields = ('reference_id','kind','object_id','issuance_id','compact_jws','compact_jws_sha256','payload_sha256','signer_kid','fixture_only')
            if any(getattr(reference, name) != row[name] for name in fields):
                _deny()
            verified[row['reference_id']] = row
        except (ValueError, KeyError, IndexError, UnicodeError):
            _deny()
    for admission in canonical_db.execute('SELECT * FROM text_analysis_preview_admissions'):
        ref = verified.get(admission['reference_id'])
        if (ref is None or ref['kind'] != 'preview' or admission['installation_id'] != installation_id
                or (admission['permit_id'],admission['issuance_id'],admission['compact_jws_sha256']) !=
                   (ref['object_id'],ref['issuance_id'],ref['compact_jws_sha256'])):
            _deny()
        if admission['state'] in ('reserved','spent'):
            intent = canonical_db.execute('SELECT * FROM text_analysis_intents WHERE job_id=?', (admission['job_id'],)).fetchone()
            if (intent is None or intent['authority_kind'] != 'preview' or intent['reference_id'] != ref['reference_id']
                    or intent['compact_jws_sha256'] != ref['compact_jws_sha256']
                    or intent['installation_id'] != installation_id):
                _deny()
            if admission['state'] == 'spent':
                if intent['contribution_id'] != admission['contribution_id'] or intent['state'] not in ('committed','result_deleted'):
                    _deny()
                output = canonical_db.execute('SELECT * FROM text_analysis_preview_outputs WHERE contribution_id=?', (admission['contribution_id'],)).fetchone()
                if intent['state'] == 'committed':
                    if (output is None or output['job_id'] != intent['job_id']
                            or hashlib.sha256(output['document']).hexdigest() != output['document_sha256']):
                        _deny()
                elif output is not None:
                    _deny()
    from app.security.text_analysis_policy import decode_run_context
    for enrollment in canonical_db.execute('SELECT * FROM text_analysis_enrollments'):
        context = decode_run_context(enrollment['context'])
        ref = verified.get(context.reference_id)
        if (ref is None or ref['kind'] != 'license' or context.compact_jws_sha256 != ref['compact_jws_sha256']
                or context.installation_id != installation_id or enrollment['installation_id'] != installation_id):
            _deny()
    for intent in canonical_db.execute('SELECT * FROM text_analysis_intents'):
        ref = verified.get(intent['reference_id'])
        context = decode_run_context(intent['context'])
        if (ref is None or ref['kind'] != intent['authority_kind']
                or ref['compact_jws_sha256'] != intent['compact_jws_sha256']
                or context.reference_id != ref['reference_id']
                or context.compact_jws_sha256 != ref['compact_jws_sha256']
                or context.installation_id != installation_id or intent['installation_id'] != installation_id):
            _deny()
    for output in canonical_db.execute('SELECT * FROM text_analysis_preview_outputs'):
        intent = canonical_db.execute('SELECT * FROM text_analysis_intents WHERE job_id=?',(output['job_id'],)).fetchone()
        job = canonical_db.execute('SELECT * FROM knowledge_jobs WHERE job_id=?',(output['job_id'],)).fetchone()
        admission = canonical_db.execute("SELECT * FROM text_analysis_preview_admissions WHERE state='spent' AND contribution_id=?",(output['contribution_id'],)).fetchone()
        from app.models.text_analysis_preview import PreviewContribution
        from app.nlp.profiles import canonical_bytes
        preview = PreviewContribution.model_validate_json(output['document'])
        if canonical_bytes(preview) != bytes(output['document']):
            _deny()
        document = preview.model_dump(mode='json')
        if (intent is None or job is None or admission is None or intent['authority_kind'] != 'preview'
                or intent['state'] != 'committed' or job['state'] != 'committed'
                or admission['job_id'] != output['job_id']
                or (document['job_id'],document['contribution_id'],document['spec_sha256']) !=
                   (job['job_id'],job['contribution_id'],job['spec_sha256'])
                or (output['sealing_attempt_fence'],output['sealing_job_attempt_id']) !=
                   (job['attempt_fence'],job['job_attempt_id'])
                or hashlib.sha256(output['document']).hexdigest() != output['document_sha256']):
            _deny()
    for row in rows:
        if row['kind'] == 'preview' and row['state'] != 'staged':
            if canonical_db.execute('SELECT 1 FROM text_analysis_preview_admissions WHERE reference_id=?', (row['reference_id'],)).fetchone() is None:
                _deny()


class AuthoritativePairOwner:
    """One exact AUTH/CANONICAL pair under a concrete root lifecycle owner."""
    def __init__(self, authentication, canonical_database, *, installation_id, root_lease):
        from app.persistence.auth import SQLiteAuthenticationStore
        if (type(authentication) is not SQLiteAuthenticationStore
                or type(canonical_database) is not CanonicalSQLite
                or type(root_lease) not in (RuntimeRootLease, FixtureAuthoritativeRootLease)
                or type(installation_id) is not str or re.fullmatch('[A-Za-z0-9][A-Za-z0-9._~-]{0,127}', installation_id) is None):
            _deny()
        self.authentication, self.auth_database = authentication, authentication.database
        self.canonical_database, self.installation_id, self.root_lease = canonical_database, installation_id, root_lease
        self.root = root_lease.root
        if not all(db.path.is_relative_to(self.root) for db in (self.auth_database,self.canonical_database)):
            _deny()
        if self.auth_database.path == self.canonical_database.path:
            _deny()
        root_lease.assert_current(self.root, allow_restore=True)

    def validate_auth_in(self, auth_db):
        self.root_lease.assert_current(self.root)
        if not self.auth_database.owns_connection(auth_db) or not auth_db.in_transaction:
            _deny()

    def validate_read_in(self, auth_db, canonical_db):
        self.validate_auth_in(auth_db)
        if not self.canonical_database.owns_connection(canonical_db) or not canonical_db.in_transaction:
            _deny()
        return pair_identity_in(auth_db, canonical_db, self.installation_id)

    def validate_in(self, auth_db, canonical_db):
        return self.validate_read_in(auth_db, canonical_db)

    def establish(self):
        self.root_lease.assert_current(self.root)
        with self.auth_database.dependency_authority_gate, self.auth_database.transaction() as auth_db:
            with self.canonical_database.transaction() as canonical_db:
                audit_crosslinks(auth_db, canonical_db, self.installation_id)
                left = auth_db.execute('SELECT * FROM authoritative_pair').fetchone()
                right = canonical_db.execute('SELECT * FROM authoritative_pair').fetchone()
                if left is not None or right is not None:
                    return self.validate_in(auth_db, canonical_db)
                identity = PairIdentity(auth_db.execute('SELECT store_id FROM auth_store_identity').fetchone()[0],
                    canonical_db.execute('SELECT store_id FROM knowledge_store_identity').fetchone()[0],
                    self.installation_id, uuid4().hex)
                for db in (auth_db,canonical_db):
                    db.execute('INSERT INTO authoritative_pair VALUES(1,?,?,?,?,1)',
                        (identity.auth_store_id,identity.canonical_store_id,identity.installation_id,identity.incarnation))
                return identity
