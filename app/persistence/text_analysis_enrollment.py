"""Durable current-account selection with immutable command receipts."""
from contextlib import contextmanager
import hashlib
from uuid import uuid4

from app.models.text_analysis_enrollment import (
    CurrentEnrollment, EnrollmentCapsule, EnrollmentCommand, EnrollmentCommandReceipt,
    EnrollmentCommandResult, EnrollmentIdentity, EnrollmentLocator, EnrollmentBindingSuperseded,
)
from app.models.text_analysis_jobs import EnrollmentIntent
from app.nlp.profiles import artifact_closure_digest, canonical_bytes, read_profile
from app.persistence.contributions import _load, _typed
from app.security.runtime_policy import RuntimePolicy, authorized_account
from app.security.text_analysis_policy import decode_run_context, require_text_analysis_fence

MAX_VERSION = 9_007_199_254_740_991


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def _atomic(db):
    name = 'enrollment_' + uuid4().hex
    db.execute('SAVEPOINT ' + name)
    try:
        yield
    except BaseException:
        db.execute('ROLLBACK TO ' + name)
        raise
    finally:
        db.execute('RELEASE ' + name)


class TextAnalysisEnrollmentStore:
    def __init__(self, jobs):
        self.jobs = jobs
        self.repository = jobs.repository

    @staticmethod
    def _scope(policy):
        if type(policy) is not RuntimePolicy or policy.identity_authority is None:
            raise ValueError('text_analysis_receipt_scope_invalid')
        account = authorized_account(policy, None)
        return policy.identity_authority.installation_id, account, policy.identity.principal_id

    def read_current_in(self, db, *, policy):
        self.repository._owned_connection(db)
        installation, account, principal = self._scope(policy)
        row = db.execute('SELECT * FROM text_analysis_account_current WHERE installation_id=? AND creator_account_id=?',
                         (installation, account)).fetchone()
        if row is None:
            return CurrentEnrollment(installation_id=installation, creator_account_id=account, principal_id=principal,
                                     mode_version=0, enrollment_id=None, state='retired', capsule_available=False)
        if row['principal_id'] != principal:
            raise ValueError('text_analysis_receipt_scope_invalid')
        state, available = 'retired', False
        if row['enrollment_id'] is not None:
            receipt, _ = self.jobs.read_enrollment_in(db, row['enrollment_id'], policy=policy)
            state = receipt.state
            if state == 'retired':
                raise ValueError('text_analysis_current_corrupt')
            available = db.execute('SELECT 1 FROM text_analysis_enrollment_capsules WHERE enrollment_id=?',
                                   (row['enrollment_id'],)).fetchone() is not None
        return CurrentEnrollment(installation_id=installation, creator_account_id=account, principal_id=principal,
            mode_version=row['mode_version'], enrollment_id=row['enrollment_id'], state=state, capsule_available=available)

    @staticmethod
    def _capsule_digest(intent, identity_raw, profile_sha):
        return _sha(canonical_bytes(intent) + b'\n' + identity_raw + b'\n' + profile_sha.encode('ascii'))

    def read_capsule_in(self, db, enrollment_id, *, policy):
        _, intent = self.jobs.read_enrollment_in(db, enrollment_id, policy=policy)
        return self._capsule_in(db, enrollment_id, intent)

    def _capsule_in(self, db, enrollment_id, intent):
        row = db.execute('SELECT * FROM text_analysis_enrollment_capsules WHERE enrollment_id=?', (enrollment_id,)).fetchone()
        if row is None:
            raise ValueError('text_analysis_capsule_unavailable')
        profile_raw, identity_raw = bytes(row['profile']), bytes(row['identity'])
        identity = _load(EnrollmentIdentity, identity_raw)
        if (row['profile_bytes_sha256'] != _sha(profile_raw) or row['identity_sha256'] != _sha(identity_raw)
                or row['capsule_sha256'] != self._capsule_digest(intent, identity_raw, _sha(profile_raw))):
            raise ValueError('text_analysis_capsule_corrupt')
        self._validate_capsule(intent, identity, profile_raw)
        return EnrollmentCapsule(enrollment_id=enrollment_id, intent=intent, identity=identity,
            profile_bytes=profile_raw, profile_bytes_sha256=row['profile_bytes_sha256'], capsule_sha256=row['capsule_sha256'])

    def read_locator_identity_in(self, db, locator):
        """Read original lineage for subsequent authenticated reconstruction."""
        self.repository._owned_connection(db)
        locator = _typed(EnrollmentLocator, locator)
        row = db.execute('SELECT * FROM text_analysis_account_current WHERE installation_id=? AND creator_account_id=?',
                         (locator.installation_id, locator.creator_account_id)).fetchone()
        if (row is None or row['principal_id'] != locator.principal_id
                or row['enrollment_id'] != locator.enrollment_id or row['mode_version'] != locator.mode_version):
            raise EnrollmentBindingSuperseded()
        enrollment = db.execute('SELECT * FROM text_analysis_enrollments WHERE enrollment_id=?',
                                (locator.enrollment_id,)).fetchone()
        if (enrollment is None or enrollment['state'] not in ('enabled', 'paused')
                or (enrollment['installation_id'], enrollment['creator_account_id'], enrollment['principal_id']) !=
                   (locator.installation_id, locator.creator_account_id, locator.principal_id)
                or _sha(bytes(enrollment['meaning'])) != enrollment['meaning_sha256']):
            raise ValueError('text_analysis_locator_corrupt')
        intent = _load(EnrollmentIntent, bytes(enrollment['meaning']))
        if intent.run_context != bytes(enrollment['context']):
            raise ValueError('text_analysis_locator_corrupt')
        capsule = self._capsule_in(db, locator.enrollment_id, intent)
        identity = capsule.identity
        if (identity.installation_id, identity.creator_account_id, identity.principal_id) != (
                locator.installation_id, locator.creator_account_id, locator.principal_id):
            raise ValueError('text_analysis_locator_corrupt')
        return identity

    @staticmethod
    def _identity_policy(identity, policy):
        authority = policy.identity_authority
        if (authority is None or policy.identity is None
                or identity.role != policy.identity.role
                or identity.principal_id != policy.identity.principal_id
                or identity.creator_account_id != policy.identity.creator_account_id
                or identity.grant_reference_ids != authority.grant_reference_ids
                or any(getattr(identity, name) != getattr(authority, name) for name in
                       ('organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt'))):
            raise ValueError('text_analysis_capsule_identity_mismatch')

    @staticmethod
    def _validate_capsule(intent, identity, profile_bytes):
        if type(profile_bytes) is not bytes or not 1 <= len(profile_bytes) <= 1048576:
            raise ValueError('text_analysis_profile_limit')
        context = decode_run_context(intent.run_context)
        profile = read_profile(profile_bytes)
        for name in ('principal_id', 'creator_account_id', 'organization_id', 'installation_id',
                     'installation_key_id', 'installation_key_jkt'):
            if getattr(identity, name) != getattr(context, name):
                raise ValueError('text_analysis_capsule_scope_invalid')
        data = tuple(a for a in profile.artifacts if a.role in ('weights', 'tokenizer', 'configuration'))
        if (profile.identity_digest() != intent.parameters.algorithm_profile_sha256
                or artifact_closure_digest(data) != intent.parameters.algorithm_artifacts_sha256):
            raise ValueError('text_analysis_capsule_profile_mismatch')
        from app.persistence.text_analysis_jobs import TextAnalysisJobStore
        TextAnalysisJobStore._plan_context(intent.parameters, context)
        return context

    def read_command_in(self, db, command_key, *, policy):
        self.repository._owned_connection(db)
        from app.models.text_analysis_jobs import ExecutionIntent
        ExecutionIntent.uuid4(command_key)
        installation, account, principal = self._scope(policy)
        row = db.execute('SELECT * FROM text_analysis_enrollment_commands WHERE installation_id=? AND command_key=?',
                         (installation, command_key)).fetchone()
        if row is None:
            return None
        if (row['creator_account_id'], row['principal_id']) != (account, principal):
            raise ValueError('text_analysis_receipt_scope_invalid')
        receipt = _load(EnrollmentCommandReceipt, bytes(row['receipt']))
        if (receipt.command_key != command_key or receipt.meaning_sha256 != row['meaning_sha256']
                or _sha(bytes(row['meaning'])) != row['meaning_sha256']):
            raise ValueError('text_analysis_command_corrupt')
        return EnrollmentCommandResult(receipt=receipt, current=self.read_current_in(db, policy=policy))

    def apply_command_in(self, db, command, *, policy, intent=None, identity=None, profile_bytes=None):
        self.jobs._guard(db)
        command = _typed(EnrollmentCommand, command)
        installation, account, principal = self._scope(policy)
        raw = canonical_bytes(command)
        old = self.read_command_in(db, command.command_key, policy=policy)
        if old is not None:
            retained = db.execute('SELECT meaning FROM text_analysis_enrollment_commands WHERE installation_id=? AND command_key=?',
                                  (installation, command.command_key)).fetchone()
            if bytes(retained[0]) != raw:
                raise ValueError('text_analysis_command_conflict')
            return old
        current = self.read_current_in(db, policy=policy)
        if (current.mode_version, current.enrollment_id) != (command.expected_mode_version, command.expected_enrollment_id):
            raise ValueError('text_analysis_command_stale')
        if current.mode_version == MAX_VERSION:
            raise ValueError('text_analysis_mode_version_exhausted')
        creating = command.action in ('enable', 'replace')
        if creating:
            intent, identity = _typed(EnrollmentIntent, intent), _typed(EnrollmentIdentity, identity)
            context = self._validate_capsule(intent, identity, profile_bytes)
            if (intent.external_key != command.command_key
                    or intent.parameters.algorithm_profile_sha256 != command.expected_profile_sha256
                    or (context.installation_id, context.creator_account_id, context.principal_id) != (installation, account, principal)):
                raise ValueError('text_analysis_command_selection_mismatch')
            self._identity_policy(identity, policy)
            require_text_analysis_fence(policy, context, kind='license', phase='start', canonical_database=self.repository.database)
            if db.execute('SELECT 1 FROM text_analysis_enrollments WHERE installation_id=? AND external_key=?',
                          (installation, intent.external_key)).fetchone():
                raise ValueError('text_analysis_enrollment_conflict')
        elif any(value is not None for value in (intent, identity, profile_bytes)):
            raise ValueError('text_analysis_command_payload_unexpected')
        with _atomic(db):
            if creating:
                if current.enrollment_id is not None:
                    self.jobs.set_enrollment_mode_in(db, current.enrollment_id, 'retired', policy=policy)
                enrolled = self.jobs.enroll_in(db, intent, policy=policy)
                enrollment_id, state = enrolled.enrollment_id, enrolled.state
                identity_raw = canonical_bytes(identity)
                db.execute('INSERT INTO text_analysis_enrollment_capsules VALUES(?,?,?,?,?,?)',
                    (enrollment_id, profile_bytes, _sha(profile_bytes), identity_raw, _sha(identity_raw),
                     self._capsule_digest(intent, identity_raw, _sha(profile_bytes))))
            else:
                state = {'pause': 'paused', 'resume': 'enabled', 'retire': 'retired'}[command.action]
                if command.action == 'resume' and not current.capsule_available:
                    raise ValueError('text_analysis_capsule_unavailable')
                if command.action == 'resume':
                    capsule = self.read_capsule_in(db, current.enrollment_id, policy=policy)
                    self._identity_policy(capsule.identity, policy)
                self.jobs.set_enrollment_mode_in(db, current.enrollment_id, state, policy=policy)
                enrollment_id = None if state == 'retired' else current.enrollment_id
            version = current.mode_version + 1
            db.execute('INSERT INTO text_analysis_account_current VALUES(?,?,?,?,?) '
                'ON CONFLICT(installation_id,creator_account_id) DO UPDATE SET '
                'mode_version=excluded.mode_version,enrollment_id=excluded.enrollment_id',
                (installation, account, principal, version, enrollment_id))
            receipt = EnrollmentCommandReceipt(command_key=command.command_key, meaning_sha256=_sha(raw),
                mode_version=version, enrollment_id=enrollment_id, state=state)
            db.execute('INSERT INTO text_analysis_enrollment_commands VALUES(?,?,?,?,?,?,?)',
                       (installation, command.command_key, account, principal, raw, _sha(raw), canonical_bytes(receipt)))
            if self.repository._storage(db) > self.repository.max_storage_bytes:
                raise ValueError('knowledge_storage_full')
            return EnrollmentCommandResult(receipt=receipt, current=self.read_current_in(db, policy=policy))

    def list_current_locators_in(self, db, *, installation_id, after_account='', limit=64):
        """Return bounded discovery identities, without granting account access."""
        self.repository._owned_connection(db)
        if (type(installation_id) is not str or not 1 <= len(installation_id) <= 512
                or type(after_account) is not str or len(after_account) > 512
                or type(limit) is not int or not 1 <= limit <= 64):
            raise ValueError('text_analysis_page_invalid')
        rows = db.execute('SELECT c.* FROM text_analysis_account_current c '
            'JOIN text_analysis_enrollment_capsules p ON p.enrollment_id=c.enrollment_id '
            'JOIN text_analysis_enrollments e ON e.enrollment_id=c.enrollment_id '
            "WHERE c.installation_id=? AND c.creator_account_id>? AND e.state IN ('enabled','paused') "
            'ORDER BY c.creator_account_id LIMIT ?', (installation_id, after_account, limit)).fetchall()
        return tuple(EnrollmentLocator(installation_id=row['installation_id'], creator_account_id=row['creator_account_id'],
            principal_id=row['principal_id'], enrollment_id=row['enrollment_id'], mode_version=row['mode_version']) for row in rows)

    def view_in(self, db, *, policy):
        from app.models.text_analysis_enrollment import (
            EnrollmentView,
            EnrollmentProfileDisclosure,
        )

        current = self.read_current_in(db, policy=policy)
        disclosure = None
        if current.capsule_available:
            capsule = self.read_capsule_in(db, current.enrollment_id, policy=policy)
            profile = read_profile(capsule.profile_bytes)
            disclosure = EnrollmentProfileDisclosure(
                profile_sha256=profile.identity_digest(),
                profile_id=profile.profile_id,
                revision=profile.revision,
                languages=tuple(
                    sorted({component.language for component in profile.components})
                ),
                tasks=tuple(
                    sorted(
                        (
                            task
                            for component in profile.components
                            for task in component.tasks
                        ),
                        key=lambda task: ('message_topics', 'concept_mentions').index(
                            task.task
                        ),
                    )
                ),
                presentation=profile.presentation,
            )
        return EnrollmentView(current=current, profile=disclosure)
