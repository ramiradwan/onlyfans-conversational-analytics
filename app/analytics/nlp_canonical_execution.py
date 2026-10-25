"""Canonical licensed execution and exact origin-bound findings replay."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time

from app.analytics.identity import source_identity
from app.analytics.nlp_contribution_replay import replay_contribution
from app.analytics.nlp_findings_store import source_binding, require_current_snapshot, MAX_LOCATORS, check_deadline
from app.models.knowledge_contributions import AttemptToken, InputIdentity, SourceMemberSpec, inventory_digest, record_digest
from app.models.text_analysis_jobs import JobPlan
from app.nlp.profiles import canonical_bytes
from app.persistence.contributions import ContributionRepository
from app.persistence.text_analysis_authority import FixtureTextAnalysisLifecycle, TextAnalysisAuthorityStore
from app.persistence.text_analysis_jobs import TextAnalysisJobStore
from app.security.text_analysis_policy import decode_run_context


@dataclass(frozen=True, slots=True)
class CanonicalWork:
    job_id: str
    contribution_id: str
    attempt: AttemptToken | None
    staged_record: bytes | None
    sealed: bool
    terminal_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _RetainedOrigin:
    record_sha256: str
    contribution_id: str
    manifest_sha256: str


class CanonicalFindingsExecution:
    """One exact licensed enrollment; production providers remain unavailable."""

    def __init__(self, repository, authority_store, *, enrollment_id, identity_policy,
                 current_evidence=None, lifecycle=None, fixture_only=False):
        if type(repository) is not ContributionRepository or type(authority_store) is not TextAnalysisAuthorityStore:
            raise ValueError('nlp_canonical_owner_invalid')
        self.repository = repository
        self.authority_store = authority_store
        self.jobs = TextAnalysisJobStore(repository)
        self.enrollment_id = enrollment_id
        self.policy = identity_policy
        self.evidence = current_evidence
        self.lifecycle = lifecycle
        self.fixture_only = fixture_only
        with repository.database.read() as db:
            _, enrollment = self.jobs.read_enrollment_in(db, enrollment_id, policy=identity_policy)
        self.context = decode_run_context(enrollment.run_context)
        self.parameters = enrollment.parameters
        if lifecycle is not None and (type(lifecycle) is not FixtureTextAnalysisLifecycle
                or lifecycle.canonical_database is not repository.database):
            raise ValueError('nlp_canonical_owner_invalid')

    def _fence(self, phase):
        return self.authority_store.transition_fence(self.policy, self.context, kind='license', phase=phase,
            evidence=self.evidence, lifecycle=self.lifecycle, fixture_only=self.fixture_only)

    def begin(self, source, profile, original, *, lease_seconds):
        if (original.account_id != self.context.creator_account_id
                or profile.identity_digest() != self.context.algorithm_profile_sha256):
            raise ValueError('nlp_canonical_selection_mismatch')
        lookup_member = SourceMemberSpec(ordinal=0, location=original.location.model_dump(), source=source_binding(original),
            source_revision=original.source_revision, text_sha256=hashlib.sha256(original.text.encode('utf-8')).hexdigest())
        with self.repository.database.transaction() as db:
            existing = self.jobs.find_child_in(db, self.enrollment_id, lookup_member, policy=self.policy)
            if existing is not None:
                if existing.state == 'result_deleted':
                    return None
                if existing.state in ('committed', 'failed'):
                    retained = self.repository.read_execution_member_in(db, existing.job_id, policy=self.policy)
                    if existing.state == 'committed':
                        return CanonicalWork(existing.job_id, existing.contribution_id, None, retained.staged_record, True)
                    if existing.terminal_reason not in ('unsupported_input', 'record_limit'):
                        raise ValueError('nlp_canonical_terminal_invalid')
                    return CanonicalWork(existing.job_id, existing.contribution_id, None, None, False, existing.terminal_reason)
        canonical = source_identity(source, original.account_id)
        if canonical is None:
            raise ValueError('knowledge_source_changed')
        member = SourceMemberSpec(ordinal=0, location=original.location.model_dump(), source=source_binding(original),
            source_revision=canonical.revision, text_sha256=hashlib.sha256(original.text.encode('utf-8')).hexdigest())
        plan = JobPlan(**self.parameters.model_dump(mode='python'), input=InputIdentity(
            canonical_revision=canonical.revision, canonical_content_sha256=canonical.content_digest.removeprefix('sha256:'),
            inventory_sha256=inventory_digest((member,)), source_count=1))
        with self._fence('prepare') as policy, self.repository.database.transaction() as db:
            receipt = self.jobs.prepare_child_in(db, self.enrollment_id, plan, (member,),
                profile_bytes=canonical_bytes(profile), policy=policy)
            if receipt.state == 'failed':
                if receipt.terminal_reason not in ('unsupported_input', 'record_limit'):
                    raise ValueError('nlp_canonical_terminal_invalid')
                return CanonicalWork(receipt.job_id, receipt.contribution_id, None, None, False, receipt.terminal_reason)
            if receipt.state == 'result_deleted':
                return None
            retained = self.repository.read_execution_member_in(db, receipt.job_id, policy=policy)
        if retained.state == 'committed':
            return CanonicalWork(receipt.job_id, receipt.contribution_id, None, retained.staged_record, True)
        with self._fence('start') as policy, self.repository.database.transaction() as db:
            self.jobs.activate_in(db, receipt.job_id, policy=policy)
            retained = self.repository.read_execution_member_in(db, receipt.job_id, policy=policy)
            if retained.state == 'committed':
                return CanonicalWork(receipt.job_id, receipt.contribution_id, None, retained.staged_record, True)
            if retained.lease_expires_at is not None and retained.lease_expires_at > self.repository.clock():
                return None
            token = self.repository.claim_attempt_in(db, receipt.job_id, expected_fence=retained.attempt_fence,
                lease_seconds=lease_seconds, policy=policy)
            return CanonicalWork(receipt.job_id, receipt.contribution_id, token, retained.staged_record, False)

    def check(self, work):
        if work.sealed or work.attempt is None:
            raise ValueError('nlp_canonical_attempt_missing')
        with self._fence('resume') as policy, self.repository.database.transaction() as db:
            return self.repository.read_execution_member_in(db, work.job_id, policy=policy, attempt=work.attempt)

    def seal(self, work, frame=None):
        if work.sealed:
            return
        if work.attempt is None:
            raise ValueError('nlp_canonical_attempt_missing')
        with self._fence('commit') as policy, self.repository.database.transaction() as db:
            if frame is not None:
                self.repository.stage_member_in(db, work.attempt, 0, frame, policy=policy)
            self.repository.commit_in(db, work.attempt, policy=policy)

    def replay(self, work, findings):
        with self.authority_store.database.dependency_authority_gate:
            return replay_contribution(self.repository, findings, work.contribution_id, policy=self.policy, limit=1)

    def retained_anchor(self, findings, frozen):
        """Check live membership of the exact frozen inventory in this enrollment."""
        with self.authority_store.database.dependency_authority_gate:
            return self._retained_anchor(findings, frozen)

    def _retained_anchor(self, findings, frozen):
        deadline=time.monotonic()+30
        with self.repository.database.read() as db:
            store_id=db.execute('SELECT store_id FROM knowledge_store_identity WHERE singleton=1').fetchone()[0]
        candidates=[]
        with findings.database.read() as db:
            value=require_current_snapshot(db,frozen,deadline=deadline)
            if len(value['records']) > MAX_LOCATORS:
                raise ValueError('nlp_recovery_record_limit')
            for key in value['records']:
                check_deadline(deadline)
                cursor=db.execute('SELECT contribution_id,manifest_sha256 FROM nlp_record_origins '
                    'WHERE account_ref=? AND record_digest=? AND canonical_store_id=?',
                    (frozen.account_ref,key,store_id))
                while page:=cursor.fetchmany(64):
                    for row in page:
                        candidates.append(_RetainedOrigin(key,row['contribution_id'],row['manifest_sha256']))
                        if len(candidates) > MAX_LOCATORS:
                            raise ValueError('nlp_recovery_record_limit')
        anchored=False
        for candidate in candidates:
            check_deadline(deadline)
            with self.repository.read_committed(candidate.contribution_id,policy=self.policy) as (db,manifest,spec,_):
                current=db.execute('SELECT c.enrollment_id,e.state FROM text_analysis_account_current c '
                    'JOIN text_analysis_enrollments e ON e.enrollment_id=c.enrollment_id '
                    'WHERE c.installation_id=? AND c.creator_account_id=?',
                    (self.context.installation_id,self.context.creator_account_id)).fetchone()
                intent=db.execute('SELECT enrollment_id FROM text_analysis_intents WHERE job_id=? AND contribution_id=?',
                                  (spec.job_id,candidate.contribution_id)).fetchone()
                if (current is None or current['enrollment_id'] != self.enrollment_id or current['state'] not in ('enabled','paused') or intent is None
                        or intent['enrollment_id'] != self.enrollment_id
                        or spec.creator_account_id != self.context.creator_account_id
                        or spec.algorithm_profile_sha256 != self.context.algorithm_profile_sha256
                        or record_digest(manifest) != candidate.manifest_sha256):
                    continue
                member=self.repository.read_execution_member_in(db,spec.job_id,policy=self.policy)
                if member.state == 'committed' and member.staged_record is not None and hashlib.sha256(member.staged_record).hexdigest() == candidate.record_sha256:
                    anchored=True
                    break
        with findings.database.read() as db:
            require_current_snapshot(db,frozen,deadline=deadline,verify_records=False)
        return anchored

    def finish_without_output(self, work, reason):
        if work.attempt is None:
            raise ValueError('nlp_canonical_attempt_missing')
        with self._fence('commit') as policy, self.repository.database.transaction() as db:
            return self.jobs.finish_licensed_without_output_in(db, work.attempt, reason=reason, policy=policy)
