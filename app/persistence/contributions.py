"""Dormant canonical enrichment storage and authenticated recovery reads."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from uuid import uuid4

from app.models.knowledge_contributions import (
    AttemptToken, ContributionManifest, JobSpec, MemberIdentity, SourceMemberSpec,
    inventory_digest, members_digest, record_digest,
)
from app.nlp import v2
from app.nlp.profiles import AnalysisProfile, SubsetAnalysisProfile, canonical_bytes, read_profile
from app.security.runtime_policy import RuntimePolicy, authorized_account

MAX_MEMBERS = 4096
PAGE_SIZE = 64


@dataclass(frozen=True, slots=True)
class ExecutionMember:
    state: str
    spec: JobSpec
    profile: AnalysisProfile | SubsetAnalysisProfile
    member: SourceMemberSpec
    attempt_fence: int
    job_attempt_id: str | None
    lease_expires_at: float | None
    staged_record: bytes | None


def _load(model, raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('knowledge_duplicate_key')
            result[key] = value
        return result
    json.loads(raw, object_pairs_hook=unique)
    return model.model_validate_json(raw)


def _typed(model, value):
    return _load(model, canonical_bytes(value))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def finding_ids(contribution_id, raw):
    """Stable identifiers derived from immutable source/task/occurrence coordinates."""
    record = v2.read_record(raw)
    result = []
    for task in record.tasks:
        for finding in task.result.payload.findings:
            value = finding.model_dump(mode='json')
            coordinates = [contribution_id, record.source.source_ref, task.result.payload.task,
                           value.get('topic_id', value.get('concept_id')), value.get('span')]
            encoded = json.dumps(coordinates, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
            result.append(_sha(b'knowledge-finding.v1\0' + encoded))
    return result


def _require_dormant_write():
    from app.core.optional_execution import require_optional_execution
    require_optional_execution()


class ContributionRepository:
    """Write methods retain the closed optional-execution environment guard.

    No production execution authorization or public write route is supplied.
    Recovery reads require identity and current sources, never an Engine.
    """

    def __init__(self, database, *, source_guard=None, clock=time.time, max_storage_bytes=256 * 1024 * 1024):
        if type(max_storage_bytes) is not int or not 1 <= max_storage_bytes <= 256 * 1024 * 1024:
            raise ValueError('knowledge_storage_limit_invalid')
        self.database, self.clock, self.max_storage_bytes = database, clock, max_storage_bytes
        self.source_guard = source_guard

    def _budget(self):
        if self.source_guard is None:
            raise ValueError('knowledge_source_guard_unavailable')
        return self.source_guard.budget()

    def _scope(self, policy, spec):
        if type(policy) is not RuntimePolicy:
            raise ValueError('knowledge_policy_invalid')
        authorized_account(policy, spec.creator_account_id)
        if policy.expires_at is not None and policy.expires_at.timestamp() <= self.clock():
            raise ValueError('knowledge_policy_expired')
        authority = policy.identity_authority
        if (authority is None or authority.organization_id != spec.organization_id
                or authority.installation_id != spec.installation_id):
            raise ValueError('knowledge_scope_invalid')

    @staticmethod
    def _storage(db):
        columns = [('knowledge_jobs', 'spec'), ('knowledge_jobs', 'profile'),
                   ('knowledge_sources', 'document'), ('knowledge_members', 'document')]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='text_analysis_intents'").fetchone():
            columns += [('text_analysis_enrollments', 'meaning'), ('text_analysis_enrollments', 'context'),
                        ('text_analysis_intents', 'context'), ('text_analysis_intents', 'confirmation'),
                        ('text_analysis_intents', 'preview_selection_policy'),
                        ('text_analysis_preview_outputs', 'document')]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='text_analysis_enrollment_capsules'").fetchone():
            columns += [(table, column) for table, names in (
                ('text_analysis_enrollment_capsules', ('enrollment_id', 'profile', 'profile_bytes_sha256',
                    'identity', 'identity_sha256', 'capsule_sha256')),
                ('text_analysis_account_current', ('installation_id', 'creator_account_id', 'principal_id',
                    'mode_version', 'enrollment_id')),
                ('text_analysis_enrollment_commands', ('installation_id', 'command_key', 'creator_account_id',
                    'principal_id', 'meaning', 'meaning_sha256', 'receipt')),
            ) for column in names]
        return sum(db.execute(f'SELECT COALESCE(sum(CASE WHEN typeof({column})=\'integer\' '
                              f'THEN 8 ELSE length({column}) END),0) FROM {table}').fetchone()[0]
                   for table, column in columns)

    def _job(self, db, job_id, policy):
        self._owned_connection(db)
        row = db.execute('SELECT * FROM knowledge_jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('knowledge_job_unavailable')
        spec = _load(JobSpec, row['spec'])
        if record_digest(spec) != row['spec_sha256']:
            raise ValueError('knowledge_job_corrupt')
        self._scope(policy, spec)
        profile = read_profile(bytes(row['profile']))
        if profile.identity_digest() != spec.algorithm_profile_sha256:
            raise ValueError('knowledge_profile_corrupt')
        return row, spec, profile

    def _sources(self, db, job_id):
        self._owned_connection(db)
        for row in db.execute('SELECT document FROM knowledge_sources WHERE job_id=? ORDER BY ordinal', (job_id,)):
            yield _load(SourceMemberSpec, row[0])

    def _source(self, db, spec, member, budget):
        self._owned_connection(db)
        self._budget()
        return self.source_guard.source(db, spec, member, budget, self.clock())

    def read_execution_member_in(self, db, job_id, ordinal=0, *, policy, attempt=None):
        """Read and validate one retained member without changing execution state."""
        self._owned_connection(db)
        if type(ordinal) is not int or not 0 <= ordinal < MAX_MEMBERS:
            raise ValueError('knowledge_member_invalid')
        row, spec, profile = self._job(db, job_id, policy)
        if attempt is not None:
            attempt = _typed(AttemptToken, attempt)
            if attempt.job_id != job_id:
                raise ValueError('knowledge_attempt_stale')
            self._write_scope(db, spec, policy, 'resume')
            self._attempt(row, attempt)
        stored = db.execute('SELECT document FROM knowledge_sources WHERE job_id=? AND ordinal=?',
                            (job_id, ordinal)).fetchone()
        if stored is None:
            raise ValueError('knowledge_member_missing')
        member = _load(SourceMemberSpec, stored[0])
        budget = self._budget()
        source = self._source(db, spec, member, budget)
        staged = db.execute('SELECT * FROM knowledge_members WHERE job_id=? AND ordinal=?', (job_id, ordinal)).fetchone()
        raw = None
        if staged is not None:
            raw = bytes(staged['document'])
            identity = _load(MemberIdentity, staged['identity'])
            if (identity.ordinal != ordinal or identity.source_member_sha256 != record_digest(member)
                    or identity.record_sha256 != _sha(raw)
                    or identity.producing_attempt_fence != staged['producing_attempt_fence']
                    or identity.producing_job_attempt_id != staged['producing_job_attempt_id']
                    or not db.execute('SELECT 1 FROM knowledge_attempts WHERE job_id=? AND attempt_fence=? AND job_attempt_id=?',
                        (job_id, identity.producing_attempt_fence, identity.producing_job_attempt_id)).fetchone()):
                raise ValueError('knowledge_member_corrupt')
            self.source_guard.record(profile, source, raw)
        budget.check()
        return ExecutionMember(row['state'], spec, profile, member, row['attempt_fence'],
            row['job_attempt_id'], row['lease_expires_at'], raw)

    def _current_input(self, db, spec, budget):
        self._owned_connection(db)
        self._budget()
        self.source_guard.current_input(db, spec, budget)

    def _owned_connection(self, db):
        if not self.database.owns_connection(db):
            raise ValueError('knowledge_connection_unowned')

    def _transaction(self, db):
        self._owned_connection(db)
        if not db.in_transaction:
            raise ValueError('knowledge_transaction_required')

    def _module_intent(self, db, job_id):
        self._owned_connection(db)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='text_analysis_intents'").fetchone():
            return None
        return db.execute('SELECT * FROM text_analysis_intents WHERE job_id=?', (job_id,)).fetchone()

    def _write_scope(self, db, spec, policy, phase):
        intent = self._module_intent(db, spec.job_id)
        if intent is None:
            if spec.capability.capability_id == 'text-analysis':
                raise ValueError('knowledge_module_intent_required')
            return
        from app.security.text_analysis_policy import (
            capability_binding, decode_run_context, require_text_analysis,
            require_text_analysis_fence,
        )
        context = decode_run_context(bytes(intent['context']))
        if phase != 'prepare' and intent['enrollment_id'] is not None:
            enrollment = db.execute('SELECT state FROM text_analysis_enrollments WHERE enrollment_id=?',
                                    (intent['enrollment_id'],)).fetchone()
            if enrollment is None or enrollment['state'] != 'enabled':
                raise ValueError('text_analysis_enrollment_unavailable')
        if phase == 'prepare':
            reference = require_text_analysis(policy, context, kind=intent['authority_kind'], phase=phase)
        else:
            reference = require_text_analysis_fence(policy, context, kind=intent['authority_kind'], phase=phase,
                                                    canonical_database=self.database)
        if (spec.capability != capability_binding(reference, context)
                or spec.creator_account_id != context.creator_account_id
                or spec.organization_id != context.organization_id
                or spec.installation_id != context.installation_id
                or spec.algorithm_profile_sha256 != context.algorithm_profile_sha256
                or spec.algorithm_artifacts_sha256 != context.algorithm_artifacts_sha256
                or spec.policy_identity_sha256 != context.policy_identity_sha256
                or record_digest(spec) != intent['spec_sha256']):
            raise ValueError('knowledge_module_binding_invalid')
        if intent['authority_kind'] == 'preview' and phase in ('resume', 'commit'):
            from app.persistence.text_analysis_jobs import TextAnalysisJobStore
            TextAnalysisJobStore._admission(db, intent, spec.job_id, allow_spent=phase == 'commit')

    def create_job(self, spec, sources, *, profile_bytes, policy):
        """The owning caller persists all request UUIDs before this call."""
        with self.database.transaction() as db:
            return self.create_job_in(db, spec, sources, profile_bytes=profile_bytes, policy=policy)

    def create_job_in(self, db, spec, sources, *, profile_bytes, policy, initial_state='pending'):
        self._transaction(db)
        if initial_state not in ('prepared', 'pending'):
            raise ValueError('knowledge_initial_state_invalid')
        _require_dormant_write()
        spec = _typed(JobSpec, spec)
        self._scope(policy, spec)
        profile = read_profile(profile_bytes)
        if profile.identity_digest() != spec.algorithm_profile_sha256:
            raise ValueError('knowledge_profile_mismatch')
        if spec.input.source_count > MAX_MEMBERS:
            raise ValueError('knowledge_source_limit')
        members = []
        for value in sources:
            if len(members) >= MAX_MEMBERS:
                raise ValueError('knowledge_source_limit')
            members.append(_typed(SourceMemberSpec, value))
        if (len(members) != spec.input.source_count or [m.ordinal for m in members] != list(range(len(members)))
                or any(m.source_revision != spec.input.canonical_revision for m in members)
                or len({m.source.source_ref for m in members}) != len(members)
                or inventory_digest(members) != spec.input.inventory_sha256):
            raise ValueError('knowledge_inventory_invalid')
        raw, profile_raw, budget = canonical_bytes(spec), canonical_bytes(profile), self._budget()
        if len(raw) > 16384 or len(profile_raw) > 1048576:
            raise ValueError('knowledge_document_limit')
        if self._module_intent(db, spec.job_id) and initial_state != 'prepared':
            raise ValueError('knowledge_module_prepare_required')
        retired = db.execute('SELECT 1 FROM knowledge_retired_intents WHERE installation_id=? AND request_id=?',
                             (spec.installation_id, spec.request_id)).fetchone()
        if retired:
            raise ValueError('knowledge_intent_retired')
        old = db.execute('SELECT * FROM knowledge_jobs WHERE job_id=? OR (installation_id=? AND request_id=?)',
                         (spec.job_id, spec.installation_id, spec.request_id)).fetchone()
        if old:
            if (bytes(old['spec']) != raw or bytes(old['profile']) != profile_raw
                    or list(self._sources(db, spec.job_id)) != members):
                raise ValueError('knowledge_intent_conflict')
            self._write_scope(db, spec, policy, 'prepare')
            return dict(old)
        self._write_scope(db, spec, policy, 'prepare')
        if initial_state == 'prepared' and not self._module_intent(db, spec.job_id):
            raise ValueError('knowledge_prepared_context_required')
        self._current_input(db, spec, budget)
        if (db.execute('SELECT count(*) FROM knowledge_jobs').fetchone()[0] >= 4096
                or self._storage(db) + len(raw) + len(profile_raw) + sum(len(canonical_bytes(m)) for m in members) > self.max_storage_bytes):
            raise ValueError('knowledge_storage_full')
        checked = [(m, self._source(db, spec, m, budget)) for m in members]
        db.execute('INSERT INTO knowledge_jobs(job_id,installation_id,request_id,contribution_id,creator_account_id,'
                   'spec,spec_sha256,profile,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (spec.job_id, spec.installation_id, spec.request_id, spec.contribution_id,
                    spec.creator_account_id, raw, _sha(raw), profile_raw, initial_state, self.clock()))
        for member, source in checked:
            db.execute('INSERT INTO knowledge_sources VALUES(?,?,?,?,?,?,?,?)',
                       (spec.job_id, member.ordinal, spec.creator_account_id,
                        member.location.conversation_id, member.location.message_id, source.sender_id,
                        member.source.source_ref, canonical_bytes(member)))
        return dict(db.execute('SELECT * FROM knowledge_jobs WHERE job_id=?', (spec.job_id,)).fetchone())

    def claim_attempt(self, job_id, *, expected_fence, lease_seconds, policy):
        with self.database.transaction() as db:
            return self.claim_attempt_in(db, job_id, expected_fence=expected_fence, lease_seconds=lease_seconds, policy=policy)

    def claim_attempt_in(self, db, job_id, *, expected_fence, lease_seconds, policy):
        self._transaction(db)
        _require_dormant_write()
        if (type(expected_fence) is not int or expected_fence < 0 or type(lease_seconds) is not int
                or not 1 <= lease_seconds <= 120):
            raise ValueError('knowledge_lease_invalid')
        row, spec, _ = self._job(db, job_id, policy)
        self._write_scope(db, spec, policy, 'resume')
        if (row['state'] not in ('pending', 'running') or row['attempt_fence'] != expected_fence
                or (row['lease_expires_at'] is not None and row['lease_expires_at'] > self.clock())):
            raise ValueError('knowledge_attempt_conflict')
        if expected_fence >= 1024:
            raise ValueError('knowledge_attempt_limit')
        budget = self._budget()
        for member in self._sources(db, job_id):
            self._source(db, spec, member, budget)
        token = AttemptToken(job_id=job_id, attempt_fence=expected_fence + 1,
                             job_attempt_id=str(uuid4()), lease_expires_at=float(self.clock() + lease_seconds))
        db.execute('INSERT INTO knowledge_attempts VALUES(?,?,?,?)',
                   (job_id, token.attempt_fence, token.job_attempt_id, self.clock()))
        db.execute("UPDATE knowledge_jobs SET state='running',attempt_fence=?,job_attempt_id=?,lease_expires_at=? WHERE job_id=?",
                   (token.attempt_fence, token.job_attempt_id, token.lease_expires_at, job_id))
        return token

    def _attempt(self, row, token):
        if (row['state'] != 'running' or row['attempt_fence'] != token.attempt_fence
                or row['job_attempt_id'] != token.job_attempt_id or row['lease_expires_at'] != token.lease_expires_at
                or token.lease_expires_at <= self.clock()):
            raise ValueError('knowledge_attempt_stale')

    def _source_valid_until(self, db, job_id):
        self._owned_connection(db)
        from app.persistence.retention import PARTICIPANT_ANALYTICS_MAX_DAYS
        return min((datetime.fromisoformat(r[0]).timestamp() + PARTICIPANT_ANALYTICS_MAX_DAYS * 86400
                    for r in db.execute('SELECT m.sent_at FROM knowledge_sources s JOIN account_messages m '
                        'ON m.creator_account_id=s.creator_account_id AND m.message_id=s.message_id WHERE s.job_id=?',
                        (job_id,))), default=None)

    def stage_member(self, token, ordinal, frame, *, policy):
        with self.database.transaction() as db:
            return self.stage_member_in(db, token, ordinal, frame, policy=policy)

    def stage_member_in(self, db, token, ordinal, frame, *, policy):
        self._transaction(db)
        _require_dormant_write()
        token = _typed(AttemptToken, token)
        if type(ordinal) is not int or not 0 <= ordinal < MAX_MEMBERS:
            raise ValueError('knowledge_member_invalid')
        row, spec, profile = self._job(db, token.job_id, policy)
        self._write_scope(db, spec, policy, 'resume')
        intent = self._module_intent(db, spec.job_id)
        if intent and intent['authority_kind'] == 'preview':
            raise ValueError('knowledge_preview_output_required')
        self._attempt(row, token)
        member_row = db.execute('SELECT document FROM knowledge_sources WHERE job_id=? AND ordinal=?',
                                (token.job_id, ordinal)).fetchone()
        if member_row is None:
            raise ValueError('knowledge_member_missing')
        member = _load(SourceMemberSpec, member_row[0])
        budget = self._budget()
        source = self._source(db, spec, member, budget)
        self.source_guard.record(profile, source, frame)
        budget.check()
        self._attempt(row, token)
        existing = db.execute('SELECT document,identity FROM knowledge_members WHERE job_id=? AND ordinal=?',
                              (token.job_id, ordinal)).fetchone()
        if existing:
            if bytes(existing[0]) != frame:
                raise ValueError('knowledge_member_conflict')
            return _load(MemberIdentity, existing[1])
        used = self._storage(db)
        if used + len(frame) > self.max_storage_bytes:
            raise ValueError('knowledge_storage_full')
        identity = MemberIdentity(ordinal=ordinal, source_member_sha256=record_digest(member),
            record_sha256=_sha(frame), producing_attempt_fence=token.attempt_fence,
            producing_job_attempt_id=token.job_attempt_id)
        db.execute('INSERT INTO knowledge_members VALUES(?,?,?,?,?,?)',
                   (token.job_id, ordinal, frame, canonical_bytes(identity),
                    token.attempt_fence, token.job_attempt_id))
        return identity

    def _members(self, db, job_id, profile, spec, budget):
        identities = []
        sources = list(self._sources(db, job_id))
        if (inventory_digest(sources) != spec.input.inventory_sha256
                or [s.ordinal for s in sources] != list(range(spec.input.source_count))):
            raise ValueError('knowledge_inventory_invalid')
        for member in sources:
            source = self._source(db, spec, member, budget)
            row = db.execute('SELECT * FROM knowledge_members WHERE job_id=? AND ordinal=?', (job_id, member.ordinal)).fetchone()
            if row is None:
                raise ValueError('knowledge_member_missing')
            identity = _load(MemberIdentity, row['identity'])
            if (identity.ordinal != member.ordinal or identity.source_member_sha256 != record_digest(member)
                    or identity.record_sha256 != _sha(bytes(row['document']))
                    or identity.producing_attempt_fence != row['producing_attempt_fence']
                    or identity.producing_job_attempt_id != row['producing_job_attempt_id']):
                raise ValueError('knowledge_member_corrupt')
            if not db.execute('SELECT 1 FROM knowledge_attempts WHERE job_id=? AND attempt_fence=? AND job_attempt_id=?',
                              (job_id, identity.producing_attempt_fence, identity.producing_job_attempt_id)).fetchone():
                raise ValueError('knowledge_producer_invalid')
            self.source_guard.record(profile, source, bytes(row['document']))
            identities.append(identity)
        if len(identities) != spec.input.source_count:
            raise ValueError('knowledge_inventory_invalid')
        return identities

    def commit(self, token, *, policy):
        """Seal complete members; no projection write occurs in this transaction."""
        with self.database.transaction() as db:
            return self.commit_in(db, token, policy=policy)

    def commit_in(self, db, token, *, policy):
        """Seal complete members; no projection write occurs in this transaction."""
        self._transaction(db)
        _require_dormant_write()
        token = _typed(AttemptToken, token)
        row, spec, profile = self._job(db, token.job_id, policy)
        self._write_scope(db, spec, policy, 'commit')
        intent = self._module_intent(db, spec.job_id)
        if intent and intent['authority_kind'] == 'preview':
            raise ValueError('knowledge_preview_output_required')
        existing = db.execute('SELECT * FROM knowledge_contributions WHERE job_id=?', (token.job_id,)).fetchone()
        if existing:
            if (existing['sealing_attempt_fence'] != token.attempt_fence
                    or existing['sealing_job_attempt_id'] != token.job_attempt_id):
                raise ValueError('knowledge_attempt_stale')
            self._verified(db, existing, policy)
            return _load(ContributionManifest, existing['manifest'])
        self._attempt(row, token)
        budget = self._budget()
        identities = self._members(db, token.job_id, profile, spec, budget)
        budget.check()
        valid_until = self._source_valid_until(db, spec.job_id)
        self._attempt(row, token)
        if valid_until is not None and valid_until <= self.clock():
            raise ValueError('knowledge_source_expired')
        manifest = ContributionManifest(contribution_id=spec.contribution_id, job_id=spec.job_id,
            attempt_fence=token.attempt_fence, job_attempt_id=token.job_attempt_id,
            scope_sha256=record_digest(spec), member_count=len(identities),
            ordered_members_sha256=members_digest(identities), profile_sha256=profile.identity_digest(),
            authorization_epoch=policy.authorization_epoch.value, source_valid_until=valid_until)
        db.execute('INSERT INTO knowledge_contributions VALUES(?,?,?,?,?,?,?)',
                   (spec.contribution_id, spec.job_id, canonical_bytes(manifest), record_digest(manifest),
                    token.attempt_fence, token.job_attempt_id, valid_until))
        db.execute("UPDATE knowledge_jobs SET state='committed',lease_expires_at=NULL,committed_at=? WHERE job_id=?",
                   (self.clock(), spec.job_id))
        return manifest

    def commit_preview_in(self, db, token, frame, *, policy):
        from app.persistence.text_analysis_jobs import TextAnalysisJobStore
        return TextAnalysisJobStore(self).commit_preview_in(db, token, frame, policy=policy)

    def finish_without_output_in(self, db, token, outcome, *, policy):
        from app.persistence.text_analysis_jobs import TextAnalysisJobStore
        return TextAnalysisJobStore(self).finish_without_output_in(db, token, outcome, policy=policy)

    def _verified(self, db, contribution, policy):
        row, spec, profile = self._job(db, contribution['job_id'], policy)
        manifest = _load(ContributionManifest, contribution['manifest'])
        identities = self._members(db, spec.job_id, profile, spec, self._budget())
        if (row['state'] != 'committed' or record_digest(manifest) != contribution['manifest_sha256']
                or manifest.scope_sha256 != record_digest(spec)
                or manifest.contribution_id != spec.contribution_id or manifest.job_id != spec.job_id
                or manifest.profile_sha256 != profile.identity_digest()
                or manifest.attempt_fence != contribution['sealing_attempt_fence']
                or manifest.job_attempt_id != contribution['sealing_job_attempt_id']
                or manifest.member_count != len(identities) or manifest.ordered_members_sha256 != members_digest(identities)):
            raise ValueError('knowledge_manifest_corrupt')
        return manifest, spec, profile

    @contextmanager
    def read_committed(self, contribution_id, *, policy):
        """Hold the source snapshot through a bounded recovery operation."""
        with self.database.transaction() as db:
            contribution = db.execute('SELECT * FROM knowledge_contributions WHERE contribution_id=?', (contribution_id,)).fetchone()
            if contribution is None:
                raise ValueError('knowledge_contribution_unavailable')
            manifest, spec, profile = self._verified(db, contribution, policy)
            yield db, manifest, spec, profile
            self._verified(db, contribution, policy)

    def get_committed(self, contribution_id, *, policy):
        with self.read_committed(contribution_id, policy=policy) as (_, manifest, _, _):
            return manifest

    def origin_is_live(self, store_id, contribution_id, manifest_sha256, account_ref, record_sha256):
        """A fresh storage fact, not an authorization decision or cached lease.

        Immutable sealed membership makes this an indexed member lookup; it
        does not rehash every result in a contribution for each derived row.
        """
        if self.source_guard is None:
            return None
        with self.database.read() as db:
            db.execute('PRAGMA busy_timeout=250')
            db.execute('BEGIN')
            identity = db.execute('SELECT store_id FROM knowledge_store_identity WHERE singleton=1').fetchone()
            if identity is None or identity[0] != store_id:
                return None
            row = db.execute('SELECT c.manifest,c.manifest_sha256,c.source_valid_until,j.spec,j.profile,j.state FROM knowledge_contributions c '
                             'JOIN knowledge_jobs j USING(job_id) WHERE c.contribution_id=?', (contribution_id,)).fetchone()
            if row is None:
                return 0
            if row['state'] != 'committed' or row['manifest_sha256'] != manifest_sha256 or _sha(bytes(row['manifest'])) != manifest_sha256:
                return 0
            manifest, spec = _load(ContributionManifest, row['manifest']), _load(JobSpec, row['spec'])
            if (manifest.scope_sha256 != record_digest(spec) or manifest.contribution_id != contribution_id
                    or manifest.source_valid_until != row['source_valid_until']
                    or (manifest.source_valid_until is not None and manifest.source_valid_until <= self.clock())):
                return 0
            member = db.execute("SELECT m.document,m.identity,s.document AS source FROM knowledge_members m INDEXED BY knowledge_member_digest "
                "JOIN knowledge_sources s USING(job_id,ordinal) WHERE m.job_id=? AND json_extract(m.identity,'$.record_sha256')=?",
                (spec.job_id, record_sha256)).fetchone()
            if member is None:
                return 0
            source = _load(SourceMemberSpec, member['source'])
            if source.source.account_scope != account_ref or _sha(bytes(member['document'])) != record_sha256:
                return 0
            try:
                self._source(db, spec, source, self.source_guard.origin_budget())
            except ValueError as error:
                if str(error) in ('knowledge_source_changed', 'knowledge_source_expired'):
                    return 0
                raise
            return int(manifest.source_valid_until is None or manifest.source_valid_until > self.clock())

    def origin_epoch(self, store_id, account_ref):
        with self.database.read() as db:
            identity = db.execute('SELECT store_id FROM knowledge_store_identity WHERE singleton=1').fetchone()
            if identity is None or identity[0] != store_id:
                return None
            row = db.execute('SELECT epoch FROM knowledge_retirement_epochs WHERE account_ref=?', (account_ref,)).fetchone()
            return row[0] if row else 0

    def export_document(self, contribution_id, *, policy):
        """Return exact result bytes and producer lineage under a verified manifest."""
        with self.read_committed(contribution_id, policy=policy) as (db, manifest, spec, profile):
            return {'manifest': manifest.model_dump(mode='json'), 'job': spec.model_dump(mode='json'),
                    'profile': profile.model_dump(mode='json'), 'members': [
                        {'identity': _load(MemberIdentity, r['identity']).model_dump(mode='json'),
                         'source': _load(SourceMemberSpec, r['source']).model_dump(mode='json'),
                         'finding_ids': finding_ids(spec.contribution_id, bytes(r['document'])),
                         'record_utf8': bytes(r['document']).decode('utf8')}
                        for r in db.execute('SELECT m.*,s.document AS source FROM knowledge_members m '
                          'JOIN knowledge_sources s USING(job_id,ordinal) WHERE m.job_id=? ORDER BY ordinal', (spec.job_id,))]}


def retire_scope(connection, account_id, scope_kind, scope_key):
    """Erase content in the source-deletion transaction, including stale locators."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_jobs'").fetchone():
        return
    if scope_kind == 'account':
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='text_analysis_enrollments'").fetchone():
            connection.execute("UPDATE text_analysis_enrollments SET state='retired' "
                               "WHERE creator_account_id=? AND state!='retired'", (account_id,))
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='text_analysis_account_current'").fetchone():
            connection.execute('UPDATE text_analysis_account_current SET enrollment_id=NULL, '
                'mode_version=CASE WHEN mode_version<9007199254740991 THEN mode_version+1 ELSE mode_version END '
                'WHERE creator_account_id=? AND enrollment_id IS NOT NULL', (account_id,))
        connection.execute('DELETE FROM knowledge_jobs WHERE creator_account_id=?', (account_id,))
        return
    column = {'message': 'message_id', 'conversation': 'conversation_id', 'participant': 'participant_id'}.get(scope_kind)
    if column is None:
        raise ValueError('knowledge_retirement_scope_invalid')
    connection.execute(f'DELETE FROM knowledge_jobs WHERE job_id IN (SELECT job_id FROM knowledge_sources '
                       f'WHERE creator_account_id=? AND {column}=?)', (account_id, scope_key))


def storage_high_water(connection):
    """Bind exact canonical result, staging and attempt bytes in backup metadata."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_jobs'").fetchone():
        return None
    digest = hashlib.sha256(b'knowledge-storage.v1\0')
    count = 0
    tables = ['knowledge_store_identity', 'knowledge_retirement_epochs',
                  'knowledge_jobs', 'knowledge_attempts', 'knowledge_sources', 'knowledge_members',
                  'knowledge_contributions', 'knowledge_retired_intents']
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='text_analysis_intents'").fetchone():
        tables += ['text_analysis_enrollments', 'text_analysis_intents',
                   'text_analysis_preview_admissions', 'text_analysis_preview_outputs']
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='text_analysis_enrollment_capsules'").fetchone():
        tables += ['text_analysis_enrollment_capsules', 'text_analysis_account_current',
                   'text_analysis_enrollment_commands']
    for table in tables:
        for row in connection.execute(f'SELECT * FROM {table} ORDER BY 1,2'):
            value = [v.hex() if type(v) is bytes else v for v in row]
            raw = json.dumps([table, value], ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
            digest.update(len(raw).to_bytes(8, 'big'))
            digest.update(raw)
            count += 1
    return {'rows': count, 'sha256': digest.hexdigest()}


def retire_expired(connection, now, account_id=None):
    """Conservatively erase whole contributions if any member expires."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_jobs'").fetchone():
        return
    from app.persistence.retention import PARTICIPANT_ANALYTICS_MAX_DAYS
    cutoff = (now - timedelta(days=PARTICIPANT_ANALYTICS_MAX_DAYS)).isoformat()
    connection.execute('DELETE FROM knowledge_jobs WHERE job_id IN ('
        'SELECT s.job_id FROM knowledge_sources s JOIN account_messages m '
        'ON m.creator_account_id=s.creator_account_id AND m.message_id=s.message_id '
        'WHERE julianday(m.sent_at)<=julianday(?) AND (? IS NULL OR m.creator_account_id=?))', (cutoff, account_id, account_id))


def reconcile_restored(connection, now):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_jobs'").fetchone():
        return
    retire_expired(connection, now)
    connection.execute("UPDATE knowledge_jobs SET state='pending',job_attempt_id=NULL,lease_expires_at=NULL WHERE state='running'")
