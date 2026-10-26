"""Immutable preview requests, capsules and exact shown-output verification."""
from contextlib import contextmanager
import hashlib
from uuid import uuid4

from app.models.knowledge_contributions import record_digest
from app.models.text_analysis_jobs import PreviewConfirmation
from app.models.text_analysis_preview_execution import (
    MAX_PREVIEW_CAPSULE_BYTES, MAX_PREVIEW_REQUEST_BYTES, PreviewExecutionCapsule,
    PreviewOutcome, PreviewPrepareRequest, PreviewLocator, PreviewDisclosure, PreviewReceipt,
)
from app.nlp.preview_parameters import require_preview_parameters
from app.nlp.profiles import canonical_bytes
from app.persistence.contributions import _load
from app.persistence.text_analysis_jobs import TextAnalysisJobStore
from app.security.text_analysis_policy import decode_run_context


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class PreviewSourceUnavailable(ValueError):
    """A live source guard refused a previously committed preview member."""


@contextmanager
def _atomic(db):
    name = 'preview_' + uuid4().hex
    db.execute('SAVEPOINT ' + name)
    try:
        yield
    except BaseException:
        db.execute('ROLLBACK TO ' + name)
        raise
    finally:
        db.execute('RELEASE ' + name)


class TextAnalysisPreviewStore:
    def __init__(self, repository):
        self.repository = repository
        self.jobs = TextAnalysisJobStore(repository)

    def _request_in(self, db, job_id):
        self.repository._owned_connection(db)
        row = db.execute('SELECT request,request_sha256 FROM text_analysis_preview_requests WHERE job_id=?',
                         (job_id,)).fetchone()
        if row is None:
            raise ValueError('preview_request_unavailable')
        raw = bytes(row['request'])
        if not 0 < len(raw) <= MAX_PREVIEW_REQUEST_BYTES or _sha(raw) != row['request_sha256']:
            raise ValueError('preview_request_corrupt')
        request = _load(PreviewPrepareRequest, raw)
        if canonical_bytes(request) != raw:
            raise ValueError('preview_request_corrupt')
        return request

    def read_key_in(self, db, external_key, *, policy):
        receipt = self.jobs.read_key_in(db, external_key, policy=policy)
        if receipt is None:
            return None
        return self.read_in(db, receipt.job_id, policy=policy)

    def read_in(self, db, job_id, *, policy):
        receipt = self.jobs.read_in(db, job_id, policy=policy)
        public_receipt = PreviewReceipt(job_id=receipt.job_id, contribution_id=receipt.contribution_id,
                                       state=receipt.state, terminal_reason=receipt.terminal_reason)
        request = self._request_in(db, job_id)
        if receipt.state == 'result_deleted':
            return PreviewOutcome(request=request, receipt=public_receipt, scope=None, confirmation=None,
                                  detailed_scope_available=False, disclosure=None)
        capsule, _, profile, _ = self.read_capsule_in(db, job_id, policy=policy)
        confirmation = _load(PreviewConfirmation, receipt.confirmation) if receipt.confirmation is not None else None
        return PreviewOutcome(
            request=request,
            receipt=public_receipt,
            scope=capsule.scope,
            confirmation=confirmation,
            detailed_scope_available=True,
            disclosure=PreviewDisclosure(
                profile_sha256=profile.identity_digest(),
                languages=tuple(
                    sorted({component.language for component in profile.components})
                ),
                tasks=tuple(
                    sorted(
                        (
                            descriptor
                            for component in profile.components
                            for descriptor in component.tasks
                        ),
                        key=lambda task: ('message_topics', 'concept_mentions').index(
                            task.task
                        ),
                    )
                ),
                presentation=profile.presentation,
                selection_policy=capsule.selection_policy,
            ),
        )

    def _capsule_in(self, db, job_id):
        self.repository._owned_connection(db)
        row = db.execute('SELECT document,document_sha256 FROM text_analysis_preview_capsules WHERE job_id=?',
                         (job_id,)).fetchone()
        if row is None:
            raise ValueError('preview_capsule_unavailable')
        raw = bytes(row['document'])
        if not 0 < len(raw) <= MAX_PREVIEW_CAPSULE_BYTES or _sha(raw) != row['document_sha256']:
            raise ValueError('preview_capsule_corrupt')
        capsule = _load(PreviewExecutionCapsule, raw)
        if capsule.job_id != job_id or canonical_bytes(capsule) != raw:
            raise ValueError('preview_capsule_corrupt')
        return capsule, row['document_sha256']

    def read_locator_identity_in(self, db, locator):
        capsule, digest = self._capsule_in(db, locator.job_id)
        row = db.execute('SELECT i.*,j.state AS job_state FROM text_analysis_intents i '
            'JOIN knowledge_jobs j USING(job_id) WHERE i.job_id=?', (locator.job_id,)).fetchone()
        if (row is None or row['authority_kind'] != 'preview' or row['job_state'] not in ('pending', 'running')
                or row['state'] != row['job_state'] or digest != locator.capsule_sha256
                or (row['installation_id'], row['principal_id'], row['creator_account_id'], row['contribution_id']) !=
                   (locator.installation_id, locator.principal_id, locator.creator_account_id, locator.contribution_id)
                or capsule.request != self._request_in(db, locator.job_id)):
            raise ValueError('preview_locator_stale')
        identity = capsule.identity
        if (identity.installation_id, identity.principal_id, identity.creator_account_id) != (
                locator.installation_id, locator.principal_id, locator.creator_account_id):
            raise ValueError('preview_capsule_corrupt')
        return identity

    def read_capsule_in(self, db, job_id, *, policy):
        self.jobs.read_in(db, job_id, policy=policy)
        return self._bound_capsule_in(db, job_id, policy=policy)

    def _bound_capsule_in(self, db, job_id, *, policy):
        """Verify stored provenance under ordinary saved-account scope."""
        capsule, _ = self._capsule_in(db, job_id)
        _, spec, profile = self.repository._job(db, job_id, policy)
        sources = tuple(self.repository._sources(db, job_id))
        intent = self.repository._module_intent(db, job_id)
        if intent is None or intent['authority_kind'] != 'preview':
            raise ValueError('preview_intent_invalid')
        raw_profile = bytes(db.execute('SELECT profile FROM knowledge_jobs WHERE job_id=?', (job_id,)).fetchone()[0])
        context = decode_run_context(bytes(intent['context']))
        request = self._request_in(db, job_id)
        parameters = capsule.parameters
        if (capsule.request != request or request.external_key != intent['external_key']
                or request.expected_profile_sha256 != profile.identity_digest()
                or capsule.contribution_id != spec.contribution_id or capsule.spec_sha256 != record_digest(spec)
                or intent['spec_sha256'] != capsule.spec_sha256
                or capsule.profile_bytes_sha256 != _sha(raw_profile)
                or capsule.context_sha256 != _sha(bytes(intent['context']))
                or capsule.confirmation_sha256 != _sha(bytes(intent['confirmation'] or b''))
                or capsule.scope.inventory_sha256 != spec.input.inventory_sha256
                or record_digest(capsule.scope) != intent['scope_sha256']
                or capsule.scope.source_count != spec.input.source_count
                or any(getattr(parameters, key) != getattr(spec, key) for key in type(parameters).model_fields)
                or capsule.selection_policy != _load(type(capsule.selection_policy), bytes(intent['preview_selection_policy']))
                or (capsule.identity.principal_id, capsule.identity.creator_account_id,
                    capsule.identity.organization_id, capsule.identity.installation_id,
                    capsule.identity.installation_key_id, capsule.identity.installation_key_jkt) !=
                   (context.principal_id, context.creator_account_id, context.organization_id, context.installation_id,
                    context.installation_key_id, context.installation_key_jkt)):
            raise ValueError('preview_capsule_corrupt')
        # The complete retained inventory remains independently bound to this job.
        from app.models.knowledge_contributions import inventory_digest
        if (len(sources) != spec.input.source_count or inventory_digest(sources) != spec.input.inventory_sha256
                or [m.ordinal for m in sources] != list(range(len(sources)))):
            raise ValueError('preview_inventory_corrupt')
        require_preview_parameters(profile, parameters, capsule.selection_policy)
        return capsule, spec, profile, sources

    def prepare_in(self, db, request, identity, scope, intent, plan, sources, *, profile_bytes, policy):
        self.repository._transaction(db)
        with _atomic(db):
            receipt = self.jobs.prepare_in(db, intent, plan, sources, profile_bytes=profile_bytes, policy=policy)
            if db.execute('SELECT 1 FROM text_analysis_preview_requests WHERE job_id=?', (receipt.job_id,)).fetchone():
                if self._request_in(db, receipt.job_id) != request:
                    raise ValueError('preview_request_conflict')
                return self.read_in(db, receipt.job_id, policy=policy)
            spec, profile, _ = self.jobs.read_plan_in(db, receipt.job_id, policy=policy)
            capsule = PreviewExecutionCapsule(job_id=receipt.job_id, contribution_id=receipt.contribution_id,
                request=request, identity=identity, scope=scope, parameters=plan.model_dump(exclude={'input'}),
                selection_policy=intent.preview_selection_policy, spec_sha256=record_digest(spec),
                profile_bytes_sha256=_sha(canonical_bytes(profile)), context_sha256=_sha(intent.run_context),
                confirmation_sha256=_sha(receipt.confirmation))
            request_raw, raw = canonical_bytes(request), canonical_bytes(capsule)
            if len(request_raw) > MAX_PREVIEW_REQUEST_BYTES or len(raw) > MAX_PREVIEW_CAPSULE_BYTES:
                raise ValueError('preview_capsule_limit')
            db.execute('INSERT INTO text_analysis_preview_requests VALUES(?,?,?)',
                       (receipt.job_id, request_raw, _sha(request_raw)))
            db.execute('INSERT INTO text_analysis_preview_capsules VALUES(?,?,?)',
                       (receipt.job_id, raw, _sha(raw)))
            if self.repository._storage(db) > self.repository.max_storage_bytes:
                raise ValueError('knowledge_storage_full')
            return self.read_in(db, receipt.job_id, policy=policy)

    def pending_in(self, db, installation_id, *, after_job='', limit=64):
        self.repository._owned_connection(db)
        if type(limit) is not int or not 1 <= limit <= 64 or type(after_job) is not str:
            raise ValueError('preview_page_invalid')
        rows = db.execute('SELECT i.*,c.document_sha256 FROM text_analysis_intents i '
            'JOIN knowledge_jobs j USING(job_id) JOIN text_analysis_preview_capsules c USING(job_id) '
            "WHERE i.installation_id=? AND i.authority_kind='preview' AND i.job_id>? "
            "AND (j.state='pending' OR (j.state='running' AND j.lease_expires_at<=?)) ORDER BY i.job_id LIMIT ?",
            (installation_id, after_job, self.repository.clock(), limit)).fetchall()
        return tuple(PreviewLocator(installation_id=r['installation_id'], principal_id=r['principal_id'],
            creator_account_id=r['creator_account_id'], job_id=r['job_id'], contribution_id=r['contribution_id'],
            capsule_sha256=r['document_sha256']) for r in rows)

    def read_shown_in(self, db, contribution_id, *, policy, validation_wall_clock_ms=30000):
        self.repository._transaction(db)
        row = db.execute('SELECT o.*,j.state AS job_state FROM text_analysis_preview_outputs o '
            'JOIN knowledge_jobs j USING(job_id) WHERE o.contribution_id=?', (contribution_id,)).fetchone()
        if row is None:
            raise ValueError('preview_output_unavailable')
        capsule, spec, profile, sources = self._bound_capsule_in(db, row['job_id'], policy=policy)
        intent = self.repository._module_intent(db, spec.job_id)
        frame = bytes(row['document'])
        if (row['job_state'] != 'committed' or intent is None or intent['authority_kind'] != 'preview'
                or intent['state'] != 'committed' or intent['contribution_id'] != contribution_id
                or spec.contribution_id != contribution_id or intent['spec_sha256'] != record_digest(spec)
                or _sha(frame) != row['document_sha256']
                or intent['confirmed_sha256'] != _sha(bytes(intent['confirmation'] or b''))):
            raise ValueError('preview_output_corrupt')
        confirmation = _load(PreviewConfirmation, bytes(intent['confirmation']))
        context = decode_run_context(bytes(intent['context']))
        if (confirmation.job_id != spec.job_id or confirmation.contribution_id != contribution_id
                or confirmation.external_key != intent['external_key']
                or confirmation.spec_sha256 != record_digest(spec)
                or confirmation.context_sha256 != _sha(bytes(intent['context']))
                or confirmation.reference_id != intent['reference_id']
                or confirmation.compact_jws_sha256 != intent['compact_jws_sha256']
                or context.reference_id != intent['reference_id'] or context.compact_jws_sha256 != intent['compact_jws_sha256']
                or confirmation.input_inventory_sha256 != spec.input.inventory_sha256
                or confirmation.scope_sha256 != intent['scope_sha256']
                or confirmation.parameter_sha256 != spec.parameter_sha256):
            raise ValueError('preview_output_corrupt')
        from app.security.text_analysis_references import PROFILE_SHA256
        if (spec.capability.object_reference_id != intent['reference_id']
                or spec.capability.object_sha256 != intent['compact_jws_sha256']
                or spec.capability.capability_id != 'text-analysis'
                or spec.capability.profile_sha256 != PROFILE_SHA256['preview']
                or spec.capability.artifact_family != context.artifact_family
                or spec.capability.artifact_major != context.selected_major_version
                or (spec.installation_id, spec.organization_id, spec.creator_account_id,
                    spec.algorithm_profile_sha256, spec.algorithm_artifacts_sha256, spec.policy_identity_sha256) !=
                   (context.installation_id, context.organization_id, context.creator_account_id,
                    context.algorithm_profile_sha256, context.algorithm_artifacts_sha256, context.policy_identity_sha256)):
            raise ValueError('preview_output_corrupt')
        self.jobs._admission(db, intent, spec.job_id, allow_spent=True)
        admission = db.execute('SELECT * FROM text_analysis_preview_admissions WHERE reference_id=?',
                               (intent['reference_id'],)).fetchone()
        if admission['state'] != 'spent' or not admission['permit_id'] or not admission['issuance_id']:
            raise ValueError('preview_output_corrupt')
        attempt = db.execute('SELECT 1 FROM knowledge_attempts WHERE job_id=? AND attempt_fence=? AND job_attempt_id=?',
            (spec.job_id, row['sealing_attempt_fence'], row['sealing_job_attempt_id'])).fetchone()
        job = db.execute('SELECT attempt_fence,job_attempt_id FROM knowledge_jobs WHERE job_id=?', (spec.job_id,)).fetchone()
        if (attempt is None or tuple(job) != (row['sealing_attempt_fence'], row['sealing_job_attempt_id'])
                or db.execute('SELECT 1 FROM knowledge_members WHERE job_id=?', (spec.job_id,)).fetchone()
                or db.execute('SELECT 1 FROM knowledge_contributions WHERE job_id=?', (spec.job_id,)).fetchone()):
            raise ValueError('preview_output_corrupt')
        output = self.repository.source_guard.preview_output(spec, profile, sources, frame)
        if (output.selection_policy != confirmation.preview_selection_policy
                or canonical_bytes(output.selection_policy) != bytes(intent['preview_selection_policy'])):
            raise ValueError('preview_output_corrupt')
        budget = self.repository._budget(wall_clock_ms=validation_wall_clock_ms)
        for member in sources:
            try:
                source = self.repository._source(db, spec, member, budget)
            except ValueError as error:
                if str(error) not in ('knowledge_source_changed', 'knowledge_source_expired'):
                    raise
                raise PreviewSourceUnavailable('preview_source_unavailable') from error
            self.repository.source_guard.preview_source(output, profile, member, source)
        budget.check()
        return output

    def saved_view_in(self, db, output, *, policy):
        from app.models.text_analysis_preview_execution import (
            PreviewSavedEnvelope,
            SavedPreviewDisclosure,
        )

        capsule, _, profile, _ = self._bound_capsule_in(
            db, output.job_id, policy=policy
        )
        return PreviewSavedEnvelope(
            scope=capsule.scope,
            output=output,
            disclosure=SavedPreviewDisclosure(
                profile_sha256=profile.identity_digest(),
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
                selection_policy=capsule.selection_policy,
            ),
        )

    def saved_list_in(self, db, *, policy, after_contribution='', limit=64):
        from app.models.text_analysis_preview_execution import (
            PreviewSavedPage,
            PreviewSavedId,
        )
        from app.models.text_analysis_jobs import ExecutionIntent
        from app.security.runtime_policy import authorized_account

        self.repository._owned_connection(db)
        if (
            type(limit) is not int
            or not 1 <= limit <= 64
            or type(after_contribution) is not str
        ):
            raise ValueError('preview_page_invalid')
        if after_contribution:
            ExecutionIntent.uuid4(after_contribution)
        account = authorized_account(policy, policy.identity.creator_account_id)
        authority = policy.identity_authority
        if authority is None:
            raise ValueError('knowledge_scope_invalid')
        rows = db.execute(
            "SELECT o.contribution_id,o.job_id FROM text_analysis_preview_outputs o "
            "JOIN knowledge_jobs j USING(job_id) WHERE j.state='committed' AND j.installation_id=? "
            "AND j.creator_account_id=? AND json_extract(j.spec,'$.organization_id')=? AND o.contribution_id>? "
            "ORDER BY o.contribution_id LIMIT ?",
            (
                authority.installation_id,
                account,
                authority.organization_id,
                after_contribution,
                limit + 1,
            ),
        ).fetchall()
        for row in rows:
            self.repository._job(db, row['job_id'], policy)
        items = tuple(
            PreviewSavedId(contribution_id=row['contribution_id'])
            for row in rows[:limit]
        )
        return PreviewSavedPage(
            items=items,
            next_after=items[-1].contribution_id if len(rows) > limit else None,
        )
