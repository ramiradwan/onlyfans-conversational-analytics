"""Session-bound one-shot previews and baseline-authorized shown-output reads."""
from contextlib import contextmanager
import weakref

from app.analytics.evidence_contracts import ResolvedEvidence
from app.analytics.nlp_preview import SELECTION_POLICY
from app.analytics.nlp_preview_scope import freeze_preview_scope
from app.core.optional_execution import require_optional_execution
from app.models.knowledge_contributions import AttemptToken, record_digest
from app.models.question_evidence import QuestionEvidence, SourceSpan
from app.models.text_analysis_jobs import ExecutionIntent, JobPlan, PreviewConfirmation
from app.models.text_analysis_preview import NoPreviewOutput
from app.models.text_analysis_preview_execution import (
    PreviewFindingsBinding, PreviewLocator, PreviewPrepareRequest,
)
from app.nlp.profiles import canonical_bytes
from app.persistence.auth import AuthenticationStateError, RevocationKey, RevocationScopeType, SQLiteAuthenticationStore
from app.persistence.authoritative_pair import AuthoritativePairOwner
from app.persistence.text_analysis_preview import PreviewSourceUnavailable
from app.security.runtime_policy import RuntimeAuthorizationDenied, authorized_account
from app.security.text_analysis_identity import TextAnalysisIdentityOwner
from app.security.text_analysis_policy import decode_run_context, encode_run_context, require_text_analysis_fence
from app.services.text_analysis_preview_selection import FixtureTextAnalysisPreviewSelectionOwner


class TextAnalysisPreviewService:
    def __init__(self, authentication, selection, source, dependencies):
        require_optional_execution()
        if (type(authentication) is not SQLiteAuthenticationStore
                or type(selection) is not FixtureTextAnalysisPreviewSelectionOwner
                or selection.authority.authentication is not authentication
                or dependencies.persistence.authentication is not authentication
                or source.history.database is not selection.store.repository.database
                or dependencies.source is not source or type(selection.pair_owner) is not AuthoritativePairOwner
                or selection.pair_owner.authentication is not authentication
                or selection.pair_owner.canonical_database is not source.history.database
                or selection.pair_owner.installation_id != selection.installation_id):
            raise ValueError('text_analysis_preview_owner_invalid')
        self.authentication, self.selection = authentication, selection
        self.source, self.dependencies = source, dependencies
        self.store, self.repository = selection.store, selection.store.repository
        self.identity = TextAnalysisIdentityOwner(authentication)
        self.pair_owner = selection.pair_owner
        self._bindings = {}

    def _http(self, policy):
        identity = self.identity.capture(policy)
        if identity.installation_id != self.selection.installation_id:
            raise ValueError('text_analysis_installation_mismatch')
        return identity, self.identity.rebuild(identity)

    def _session_in(self, db, original):
        auth = self.authentication
        try:
            row = auth._require_session_current(db, original.identity.session_id, auth._clock())
            if ((row['principal_id'], row['creator_account_id'], row['role']) !=
                    (original.identity.principal_id, original.identity.creator_account_id, original.identity.role)
                    or not auth.runtime_policy_is_current_in(db, original, now=auth._clock())
                    or set(auth._session_grants(db, original.identity.session_id)) != set(original.signed_object_reference_ids)):
                raise AuthenticationStateError('Session changed')
        except AuthenticationStateError:
            raise RuntimeAuthorizationDenied('text_analysis_session_unavailable') from None

    def _baseline_in(self, db, policy):
        authorized_account(policy, policy.identity.creator_account_id)
        authority = policy.identity_authority
        if (authority is None or authority.installation_id != self.selection.installation_id
                or not self.authentication.runtime_policy_is_current_in(db, policy, now=self.authentication._clock())
                or any(self.authentication._scope_is_revoked(db, RevocationKey(kind, value)) for kind, value in (
                    (RevocationScopeType.PRINCIPAL, policy.identity.principal_id),
                    (RevocationScopeType.CREATOR_ACCOUNT, policy.identity.creator_account_id),
                    (RevocationScopeType.INSTALLATION, authority.installation_id)))):
            raise RuntimeAuthorizationDenied('text_analysis_identity_unavailable')

    @contextmanager
    def _baseline_transaction(self, baseline, http_policy=None, *, execution=False):
        with self.dependencies.gate, self.authentication.database.transaction() as auth_db:
            self._baseline_in(auth_db, baseline)
            if http_policy is not None:
                self._session_in(auth_db, http_policy)
            self.pair_owner.validate_auth_in(auth_db)
            with self.repository.database.transaction() as db:
                if execution:
                    self.pair_owner.validate_in(auth_db, db)
                yield auth_db, db
                self._baseline_in(auth_db, baseline)
                if http_policy is not None:
                    self._session_in(auth_db, http_policy)
                if execution:
                    self.pair_owner.validate_in(auth_db, db)

    def read_outcome(self, http_policy, external_key):
        ExecutionIntent.uuid4(external_key)
        with self.dependencies.gate:
            _, baseline = self._http(http_policy)
            with self._baseline_transaction(baseline, http_policy) as (_, db):
                return self.store.read_key_in(db, external_key, policy=baseline)

    def prepare(self, http_policy, request):
        require_optional_execution()
        request = PreviewPrepareRequest.model_validate_json(canonical_bytes(request))
        with self.dependencies.gate:
            identity, baseline = self._http(http_policy)
            with self._baseline_transaction(baseline, http_policy) as (_, db):
                old = self.store.read_key_in(db, request.external_key, policy=baseline)
                if old is not None:
                    if old.request != request:
                        raise ValueError('preview_request_conflict')
                    return old
            profile, parameters, context = self.selection.choose(request.expected_profile_sha256, identity, self.dependencies)
            scope, input_identity, sources = freeze_preview_scope(self.repository, self.source,
                identity.creator_account_id, self.authentication._clock(), self.repository._budget())
            plan = JobPlan(**parameters.model_dump(mode='python'), input=input_identity)
            intent = ExecutionIntent(external_key=request.external_key, authority_kind='preview',
                run_context=encode_run_context(context), scope_sha256=record_digest(scope),
                preview_selection_policy=SELECTION_POLICY)
            evidence = self.selection.evidence(baseline, context)
            with self.selection.authority.transition_fence(baseline, context, kind='preview', phase='prepare',
                    evidence=evidence, lifecycle=self.selection.lifecycle, fixture_only=True) as held:
                auth_db = held.text_analysis_fence._connection
                with self.repository.database.transaction() as db:
                    self._session_in(auth_db, http_policy)
                    self.pair_owner.validate_in(auth_db, db)
                    result = self.store.prepare_in(db, request, identity, scope, intent, plan, sources,
                        profile_bytes=canonical_bytes(profile), policy=held)
                    self._session_in(auth_db, http_policy)
                    require_text_analysis_fence(held, context, kind='preview', phase='prepare',
                        canonical_database=self.repository.database, canonical_connection=db)
                return result

    def confirm(self, http_policy, external_key, confirmation):
        require_optional_execution()
        ExecutionIntent.uuid4(external_key)
        confirmation = PreviewConfirmation.model_validate_json(canonical_bytes(confirmation))
        with self.dependencies.gate:
            _, baseline = self._http(http_policy)
            with self._baseline_transaction(baseline, http_policy) as (_, db):
                old = self.store.read_key_in(db, external_key, policy=baseline)
                if old is None:
                    raise ValueError('preview_request_unavailable')
                if old.confirmation is not None and old.confirmation != confirmation:
                    raise ValueError('text_analysis_confirmation_mismatch')
                if old.receipt.state in ('committed', 'failed', 'result_deleted'):
                    return old
                capsule, _, _, _ = self.store.read_capsule_in(db, old.receipt.job_id, policy=baseline)
            baseline = self.identity.rebuild(capsule.identity)
            context = self._context(capsule.job_id)
            with self._fence(baseline, context, 'start') as held:
                auth_db = held.text_analysis_fence._connection
                with self.repository.database.transaction() as db:
                    self._session_in(auth_db, http_policy)
                    self.pair_owner.validate_in(auth_db, db)
                    self.store.jobs.activate_in(db, capsule.job_id, confirmation=canonical_bytes(confirmation), policy=held)
                    result = self.store.read_in(db, capsule.job_id, policy=baseline)
                    self._session_in(auth_db, http_policy)
                    require_text_analysis_fence(held, context, kind='preview', phase='start',
                        canonical_database=self.repository.database, canonical_connection=db)
                return result

    def _context(self, job_id):
        with self.repository.database.read() as db:
            row = self.repository._module_intent(db, job_id)
            if row is None:
                raise ValueError('preview_intent_unavailable')
            return decode_run_context(bytes(row['context']))

    def _fence(self, policy, context, phase):
        return self.selection.authority.transition_fence(policy, context, kind='preview', phase=phase,
            evidence=self.selection.evidence(policy, context), lifecycle=self.selection.lifecycle, fixture_only=True)

    def pending_locators(self, *, after_job='', limit=64):
        with self.dependencies.gate, self.repository.database.read() as db:
            return self.store.pending_in(db, self.selection.installation_id, after_job=after_job, limit=limit)

    def bind_pending(self, locator):
        require_optional_execution()
        if type(locator) is not PreviewLocator or locator.installation_id != self.selection.installation_id:
            raise ValueError('preview_locator_invalid')
        with self.dependencies.gate:
            with self.repository.database.read() as db:
                identity = self.store.read_locator_identity_in(db, locator)
            baseline = self.identity.rebuild(identity)
            with self._baseline_transaction(baseline, execution=True) as (_, db):
                capsule, spec, profile, sources = self.store.read_capsule_in(db, locator.job_id, policy=baseline)
                self.store.read_locator_identity_in(db, locator)
                receipt = self.store.jobs.read_in(db, locator.job_id, policy=baseline)
                store_id = db.execute('SELECT store_id FROM knowledge_store_identity WHERE singleton=1').fetchone()[0]
            binding = PreviewFindingsBinding(locator, receipt, capsule, spec, profile, sources,
                self._context(locator.job_id), baseline)
            key, bindings = id(binding), self._bindings
            fields = self._fields(binding)
            def release(reference):
                old = bindings.get(key)
                if old is not None and old[0] is reference:
                    del bindings[key]
            bindings[key] = (weakref.ref(binding, release), fields, store_id)
            return binding

    @staticmethod
    def _fields(binding):
        return (binding.locator, binding.receipt, binding.capsule, binding.spec, binding.profile,
                binding.sources, binding.context, binding.policy)

    def _registered_in(self, db, binding):
        if type(binding) is not PreviewFindingsBinding:
            raise ValueError('preview_binding_invalid')
        original = self._bindings.get(id(binding))
        if (original is None or original[0]() is not binding
                or any(a is not b for a, b in zip(self._fields(binding), original[1]))):
            raise ValueError('preview_binding_invalid')
        row = db.execute('SELECT c.document_sha256,s.store_id FROM text_analysis_preview_capsules c '
            'JOIN knowledge_store_identity s ON s.singleton=1 WHERE c.job_id=?', (binding.spec.job_id,)).fetchone()
        if row is None or tuple(row) != (binding.locator.capsule_sha256, original[2]):
            raise ValueError('preview_binding_stale')

    def claim(self, binding, *, lease_seconds=120):
        require_optional_execution()
        with self.dependencies.gate, self._fence(binding.policy, binding.context, 'resume') as held:
            auth_db = held.text_analysis_fence._connection
            with self.repository.database.transaction() as db:
                self._baseline_in(auth_db, binding.policy)
                self._registered_in(db, binding)
                self.pair_owner.validate_in(auth_db, db)
                row, _, _ = self.repository._job(db, binding.spec.job_id, held)
                token = self.repository.claim_attempt_in(db, binding.spec.job_id,
                    expected_fence=row['attempt_fence'], lease_seconds=lease_seconds, policy=held)
                self._baseline_in(auth_db, binding.policy)
                self.repository._write_scope(db, binding.spec, held, 'resume')
                return token

    def check(self, binding, token):
        require_optional_execution()
        with self.dependencies.gate, self._fence(binding.policy, binding.context, 'resume') as held:
            auth_db = held.text_analysis_fence._connection
            with self.repository.database.transaction() as db:
                self._baseline_in(auth_db, binding.policy)
                self._registered_in(db, binding)
                if type(token) is not AttemptToken or token.job_id != binding.spec.job_id:
                    raise ValueError('knowledge_attempt_stale')
                row, _, _ = self.repository._job(db, token.job_id, held)
                self.repository._attempt(row, token)
                self.repository._write_scope(db, binding.spec, held, 'resume')

    def commit(self, binding, token, outcome, *, validation_wall_clock_ms):
        require_optional_execution()
        if type(validation_wall_clock_ms) is not int or not 1 <= validation_wall_clock_ms <= 30000:
            raise ValueError('preview_validation_budget_invalid')
        if type(outcome) not in (bytes, NoPreviewOutput):
            raise ValueError('preview_outcome_invalid')
        with self.dependencies.gate, self._fence(binding.policy, binding.context, 'commit') as held:
            auth_db = held.text_analysis_fence._connection
            with self.repository.database.transaction() as db:
                self._baseline_in(auth_db, binding.policy)
                self._registered_in(db, binding)
                if type(token) is not AttemptToken or token.job_id != binding.spec.job_id:
                    raise ValueError('knowledge_attempt_stale')
                if type(outcome) is bytes:
                    self.store.jobs.commit_preview_in(db, token, outcome, policy=held,
                        validation_wall_clock_ms=validation_wall_clock_ms)
                else:
                    self.store.jobs.finish_without_output_in(db, token, outcome, policy=held,
                        validation_wall_clock_ms=validation_wall_clock_ms)
                self._baseline_in(auth_db, binding.policy)
                if self.repository.clock() >= token.lease_expires_at:
                    raise ValueError('knowledge_attempt_stale')
                require_text_analysis_fence(held, binding.context, kind='preview', phase='commit',
                    canonical_database=self.repository.database, canonical_connection=db)
                return self.store.jobs.read_in(db, token.job_id, policy=binding.policy)

    def read_shown(self, http_policy, contribution_id):
        return self._read_saved(http_policy, contribution_id, view=False)

    def read_saved_view(self, http_policy, contribution_id):
        return self._read_saved(http_policy, contribution_id, view=True)

    def read_saved_evidence(self, http_policy, contribution_id, reference):
        reference = QuestionEvidence.model_validate_json(canonical_bytes(reference))
        return self._read_saved(http_policy, contribution_id, view=False, reference=reference)

    def saved_list(self, http_policy, *, after_contribution='', limit=64):
        with self.dependencies.gate:
            _, baseline = self._http(http_policy)
            with self._baseline_transaction(baseline, http_policy) as (_, db):
                return self.store.saved_list_in(
                    db,
                    policy=baseline,
                    after_contribution=after_contribution,
                    limit=limit,
                )

    def _read_saved(self, http_policy, contribution_id, *, view, reference=None):
        ExecutionIntent.uuid4(contribution_id)
        with self.dependencies.gate:
            _, baseline = self._http(http_policy)
            with self._baseline_transaction(baseline, http_policy) as (auth_db, db):
                try:
                    output = self.store.read_shown_in(db, contribution_id, policy=baseline)
                    self._saved_reference_in(auth_db, db, output)
                    if reference is not None:
                        result = self._saved_evidence_in(db, baseline, output, reference)
                    else:
                        result = (
                            self.store.saved_view_in(db, output, policy=baseline)
                            if view
                            else output
                        )
                except PreviewSourceUnavailable:
                    # Retire only after a genuine live-source refusal. Preserve
                    # lifetime intent and spent admission through the cascade.
                    row = db.execute('SELECT job_id FROM text_analysis_preview_outputs WHERE contribution_id=?',
                                     (contribution_id,)).fetchone()
                    if row is None:
                        raise ValueError('preview_output_unavailable') from None
                    self.repository._job(db, row['job_id'], baseline)
                    db.execute('DELETE FROM knowledge_jobs WHERE job_id=?', (row['job_id'],))
                    output = None
                    result = None
                self._baseline_in(auth_db, baseline)
            # Refusal occurs after the authorized erasure transaction commits.
            if output is None:
                raise ValueError('preview_output_unavailable')
            return result

    def _saved_evidence_in(self, db, baseline, output, reference):
        matches = [entry for summary in output.summaries for entry in summary.evidence
                   if entry.reference == reference]
        if not matches:
            raise ValueError('preview_evidence_unavailable')
        _, spec, profile, sources = self.store._bound_capsule_in(db, output.job_id, policy=baseline)
        member = sources[matches[0].ordinal]
        budget = self.repository._budget()
        try:
            source = self.repository._source(db, spec, member, budget)
        except ValueError as error:
            if str(error) not in ('knowledge_source_changed', 'knowledge_source_expired'):
                raise
            raise PreviewSourceUnavailable('preview_source_unavailable') from error
        self.repository.source_guard.preview_source(output, profile, member, source)
        # The retained revision belongs to the frozen inventory; all other source fields stay current.
        current = source.reference(reference.span).model_copy(update={'source_revision': member.source_revision})
        if current != reference:
            raise ValueError('preview_evidence_unavailable')
        span = reference.span
        browser_span = None if span is None else SourceSpan(
            start=len(source.text[:span.start].encode('utf-16-le')) // 2,
            end=len(source.text[:span.end].encode('utf-16-le')) // 2,
        )
        budget.check()
        return ResolvedEvidence(reference=reference, location=member.location.model_dump(), text=source.text,
            direction=source.direction, checked_at=self.authentication._clock(), browser_span=browser_span)

    def _saved_reference_in(self, auth_db, db, output):
        intent = self.repository._module_intent(db, output.job_id)
        admission = db.execute('SELECT * FROM text_analysis_preview_admissions WHERE reference_id=?',
            (intent['reference_id'],)).fetchone()
        if admission is None:
            raise ValueError('preview_reference_corrupt')
        self.selection.authority.validate_saved_preview_reference_in(auth_db,
            reference_id=intent['reference_id'], compact_jws_sha256=intent['compact_jws_sha256'],
            permit_id=admission['permit_id'], issuance_id=admission['issuance_id'])


_service = None


def preview_service():
    return _service


def configure_preview_service(service):
    global _service
    if service is not None and type(service) is not TextAnalysisPreviewService:
        raise ValueError('text_analysis_preview_service_invalid')
    _service = service
