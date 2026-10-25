"""Dormant canonical enrichment storage and authenticated recovery reads."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models.knowledge_contributions import (
    AttemptToken, ContributionManifest, JobSpec, MemberIdentity, SourceMemberSpec,
    inventory_digest, members_digest, record_digest,
)
from app.core.config import settings
from app.nlp import v2
from app.nlp.profiles import canonical_bytes, read_profile
from app.security.runtime_policy import RuntimePolicy, authorized_account

MAX_MEMBERS = 4096
PAGE_SIZE = 64


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
    if settings.environment.lower() != 'test':
        raise ValueError('findings_execution_unavailable')


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
        return sum(db.execute(f'SELECT COALESCE(sum(length({column})),0) FROM {table}').fetchone()[0]
                   for table, column in [('knowledge_jobs', 'spec'), ('knowledge_jobs', 'profile'),
                                         ('knowledge_sources', 'document'), ('knowledge_members', 'document')])

    def _job(self, db, job_id, policy):
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

    @staticmethod
    def _sources(db, job_id):
        for row in db.execute('SELECT document FROM knowledge_sources WHERE job_id=? ORDER BY ordinal', (job_id,)):
            yield _load(SourceMemberSpec, row[0])

    def _source(self, db, spec, member, budget):
        self._budget()
        return self.source_guard.source(db, spec, member, budget, self.clock())

    def _current_input(self, db, spec, budget):
        self._budget()
        self.source_guard.current_input(db, spec, budget)

    def create_job(self, spec, sources, *, profile_bytes, policy):
        """The owning caller persists all request UUIDs before this call."""
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
        with self.database.transaction() as db:
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
                return dict(old)
            self._current_input(db, spec, budget)
            if (db.execute('SELECT count(*) FROM knowledge_jobs').fetchone()[0] >= 4096
                    or self._storage(db) + len(raw) + len(profile_raw) + sum(len(canonical_bytes(m)) for m in members) > self.max_storage_bytes):
                raise ValueError('knowledge_storage_full')
            checked = [(m, self._source(db, spec, m, budget)) for m in members]
            db.execute('INSERT INTO knowledge_jobs(job_id,installation_id,request_id,contribution_id,creator_account_id,'
                       'spec,spec_sha256,profile,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (spec.job_id, spec.installation_id, spec.request_id, spec.contribution_id,
                        spec.creator_account_id, raw, _sha(raw), profile_raw, 'pending', self.clock()))
            for member, source in checked:
                db.execute('INSERT INTO knowledge_sources VALUES(?,?,?,?,?,?,?,?)',
                           (spec.job_id, member.ordinal, spec.creator_account_id,
                            member.location.conversation_id, member.location.message_id, source.sender_id,
                            member.source.source_ref, canonical_bytes(member)))
            return dict(db.execute('SELECT * FROM knowledge_jobs WHERE job_id=?', (spec.job_id,)).fetchone())

    def claim_attempt(self, job_id, *, expected_fence, lease_seconds, policy):
        _require_dormant_write()
        if (type(expected_fence) is not int or expected_fence < 0 or type(lease_seconds) is not int
                or not 1 <= lease_seconds <= 120):
            raise ValueError('knowledge_lease_invalid')
        with self.database.transaction() as db:
            row, spec, _ = self._job(db, job_id, policy)
            if (row['state'] == 'committed' or row['attempt_fence'] != expected_fence
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

    def stage_member(self, token, ordinal, frame, *, policy):
        _require_dormant_write()
        token = _typed(AttemptToken, token)
        if type(ordinal) is not int or not 0 <= ordinal < MAX_MEMBERS:
            raise ValueError('knowledge_member_invalid')
        with self.database.transaction() as db:
            row, spec, profile = self._job(db, token.job_id, policy)
            self._attempt(row, token)
            member_row = db.execute('SELECT document FROM knowledge_sources WHERE job_id=? AND ordinal=?',
                                    (token.job_id, ordinal)).fetchone()
            if member_row is None:
                raise ValueError('knowledge_member_missing')
            member = _load(SourceMemberSpec, member_row[0])
            source = self._source(db, spec, member, self._budget())
            self.source_guard.record(profile, source, frame)
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
        _require_dormant_write()
        token = _typed(AttemptToken, token)
        with self.database.transaction() as db:
            row, spec, profile = self._job(db, token.job_id, policy)
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
            from app.persistence.retention import PARTICIPANT_ANALYTICS_MAX_DAYS
            valid_until = min((datetime.fromisoformat(r[0]).timestamp() + PARTICIPANT_ANALYTICS_MAX_DAYS * 86400
                for r in db.execute('SELECT m.sent_at FROM knowledge_sources s JOIN account_messages m '
                    'ON m.creator_account_id=s.creator_account_id AND m.message_id=s.message_id WHERE s.job_id=?',
                    (spec.job_id,))), default=None)
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
    for table in ('knowledge_store_identity', 'knowledge_retirement_epochs',
                  'knowledge_jobs', 'knowledge_attempts', 'knowledge_sources', 'knowledge_members',
                  'knowledge_contributions', 'knowledge_retired_intents'):
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
