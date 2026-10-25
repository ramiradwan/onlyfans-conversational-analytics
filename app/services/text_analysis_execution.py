"""Authenticated account commands and retained licensed execution owners."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import weakref

from app.analytics.nlp_canonical_execution import CanonicalFindingsExecution
from app.core.optional_execution import require_optional_execution
from app.models.text_analysis_enrollment import EnrollmentCapsule, EnrollmentCommand, EnrollmentLocator, CurrentEnrollment
from app.models.text_analysis_jobs import EnrollmentIntent
from app.nlp.execution_parameters import require_original_parameters
from app.nlp.profiles import AnalysisProfile, canonical_bytes, read_profile
from app.persistence.auth import AuthenticationStateError, RevocationKey, RevocationScopeType, SQLiteAuthenticationStore
from app.security.runtime_policy import RuntimeAuthorizationDenied, RuntimePolicy, authorized_account
from app.security.text_analysis_identity import TextAnalysisIdentityOwner
from app.security.text_analysis_policy import decode_run_context, encode_run_context
from app.services.text_analysis_selection import FixtureTextAnalysisSelectionOwner


@dataclass(frozen=True, slots=True, weakref_slot=True)
class LicensedFindingsBinding:
    locator: EnrollmentLocator
    current: CurrentEnrollment
    capsule: EnrollmentCapsule
    profile: AnalysisProfile
    identity_policy: RuntimePolicy
    execution: CanonicalFindingsExecution


class TextAnalysisExecutionService:
    def __init__(self, authentication, selection, source, dependencies):
        require_optional_execution()
        if (type(authentication) is not SQLiteAuthenticationStore
                or type(selection) is not FixtureTextAnalysisSelectionOwner
                or selection.authority.authentication is not authentication
                or dependencies.persistence.authentication is not authentication
                or dependencies.execution_owner is not None
                or source.history.database is not selection.enrollments.repository.database
                or dependencies.source is not source):
            raise ValueError('text_analysis_composition_owner_invalid')
        self.authentication, self.selection = authentication, selection
        self.source, self.dependencies = source, dependencies
        self.enrollments, self.repository = selection.enrollments, selection.enrollments.repository
        self.identity = TextAnalysisIdentityOwner(authentication)
        self._bindings = {}
        dependencies.execution_owner = self

    def _http_identity(self, policy):
        identity = self.identity.capture(policy)
        if identity.installation_id != self.selection.installation_id:
            raise ValueError('text_analysis_installation_mismatch')
        return identity, self.identity.rebuild(identity)

    def _session_in(self, db, original):
        """Recheck the original session while an auth writer reservation is held."""
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

    def command(self, http_policy, command):
        require_optional_execution()
        command = EnrollmentCommand.model_validate_json(canonical_bytes(command))
        with self.dependencies.gate:
            identity, baseline = self._http_identity(http_policy)
            with self.repository.database.read() as db:
                prior = self.enrollments.read_command_in(db, command.command_key, policy=baseline)
            if prior is not None or command.action in ('pause', 'retire'):
                with self.authentication.database.transaction() as auth_db, self.repository.database.transaction() as db:
                    self._session_in(auth_db, http_policy)
                    result = self.enrollments.apply_command_in(db, command, policy=baseline)
                    self._session_in(auth_db, http_policy)
            else:
                payload = {}
                if command.action in ('enable', 'replace'):
                    profile, parameters, context = self.selection.choose(command.expected_profile_sha256, identity, self.dependencies)
                    payload = dict(identity=identity, profile_bytes=canonical_bytes(profile),
                        intent=EnrollmentIntent(external_key=command.command_key,
                            run_context=encode_run_context(context), parameters=parameters))
                else:
                    with self.repository.database.read() as db:
                        capsule = self.enrollments.read_capsule_in(db, command.expected_enrollment_id, policy=baseline)
                    baseline = self.identity.rebuild(capsule.identity)
                    profile = read_profile(capsule.profile_bytes)
                    require_original_parameters(profile, capsule.intent.parameters)
                    context = decode_run_context(capsule.intent.run_context)
                evidence = self.selection.evidence(baseline, context)
                with self.selection.authority.transition_fence(baseline, context, kind='license', phase='start',
                        evidence=evidence, lifecycle=self.selection.lifecycle, fixture_only=True) as held:
                    with self.repository.database.transaction() as db:
                        with self.authentication.database.read() as auth_db:
                            self._session_in(auth_db, http_policy)
                        result = self.enrollments.apply_command_in(db, command, policy=held, **payload)
                        with self.authentication.database.read() as auth_db:
                            self._session_in(auth_db, http_policy)
            # A committed command remains recoverable if disposable reconciliation fails.
            self.reconcile_current(baseline)
            return result

    def read_current(self, http_policy):
        with self.dependencies.gate:
            _, baseline = self._http_identity(http_policy)
            with self.repository.database.read() as db:
                return self.enrollments.read_current_in(db, policy=baseline)

    def read_command(self, http_policy, command_key):
        with self.dependencies.gate:
            _, baseline = self._http_identity(http_policy)
            with self.repository.database.read() as db:
                return self.enrollments.read_command_in(db, command_key, policy=baseline)

    def current_locators(self, *, after_account='', limit=64):
        with self.dependencies.gate, self.repository.database.read() as db:
            return self.enrollments.list_current_locators_in(db, installation_id=self.selection.installation_id,
                                                           after_account=after_account, limit=limit)

    def reconcile_current(self, policy):
        with self.dependencies.gate, self.repository.database.transaction() as db:
            if not self.authentication.runtime_policy_is_current(policy):
                raise RuntimeAuthorizationDenied('text_analysis_identity_unavailable')
            current = self.enrollments.read_current_in(db, policy=policy)
            profile = None
            if current.enrollment_id is not None:
                capsule = self.enrollments.read_capsule_in(db, current.enrollment_id, policy=policy)
                profile = read_profile(capsule.profile_bytes)
            store_id = db.execute('SELECT store_id FROM knowledge_store_identity WHERE singleton=1').fetchone()[0]
            self.dependencies.findings.reconcile_enrollment(current.creator_account_id,
                canonical_store_id=store_id, current=current, profile=profile)
            return current

    def bind_current(self, locator):
        require_optional_execution()
        with self.dependencies.gate:
            if type(locator) is not EnrollmentLocator or locator.installation_id != self.selection.installation_id:
                raise ValueError('text_analysis_locator_invalid')
            with self.repository.database.read() as db:
                identity = self.enrollments.read_locator_identity_in(db, locator)
            baseline = self.identity.rebuild(identity)
            with self.repository.database.read() as db:
                current = self.enrollments.read_current_in(db, policy=baseline)
                capsule = self.enrollments.read_capsule_in(db, locator.enrollment_id, policy=baseline)
                store_id = db.execute('SELECT store_id FROM knowledge_store_identity WHERE singleton=1').fetchone()[0]
            self._same_current(locator, current)
            profile = read_profile(capsule.profile_bytes)
            require_original_parameters(profile, capsule.intent.parameters)
            context = decode_run_context(capsule.intent.run_context)
            try:
                evidence = self.selection.evidence(baseline, context)
            except (ValueError, RuntimeAuthorizationDenied):
                evidence = None
            execution = CanonicalFindingsExecution(self.repository, self.selection.authority,
                enrollment_id=locator.enrollment_id, identity_policy=baseline, current_evidence=evidence,
                lifecycle=self.selection.lifecycle, fixture_only=True)
            self.reconcile_current(baseline)
            binding = LicensedFindingsBinding(locator, current, capsule, profile, baseline, execution)
            self._register_binding(binding, store_id)
            return binding

    def _register_binding(self, binding, store_id):
        key, bindings = id(binding), self._bindings
        def release(reference):
            current = bindings.get(key)
            if current is not None and current[0] is reference:
                del bindings[key]
        fields = (binding.locator, binding.current, binding.capsule, binding.profile,
                  binding.identity_policy, binding.execution)
        hashes = (hashlib.sha256(canonical_bytes(binding.capsule.intent)).hexdigest(),
                  hashlib.sha256(canonical_bytes(binding.capsule.identity)).hexdigest(),
                  binding.capsule.profile_bytes_sha256, binding.capsule.capsule_sha256)
        bindings[key] = (weakref.ref(binding, release), fields, hashes, store_id)

    @staticmethod
    def _same_current(locator, current):
        if (current.installation_id, current.creator_account_id, current.principal_id,
                current.enrollment_id, current.mode_version) != (locator.installation_id, locator.creator_account_id,
                locator.principal_id, locator.enrollment_id, locator.mode_version):
            raise ValueError('text_analysis_locator_stale')

    def authorize_binding(self, binding, account):
        if type(binding) is not LicensedFindingsBinding or account != binding.locator.creator_account_id:
            raise ValueError('text_analysis_binding_invalid')
        with self.dependencies.gate:
            retained = self._bindings.get(id(binding))
            fields = (binding.locator, binding.current, binding.capsule, binding.profile,
                      binding.identity_policy, binding.execution)
            if (retained is None or retained[0]() is not binding
                    or any(actual is not expected for actual, expected in zip(fields, retained[1]))):
                raise ValueError('text_analysis_binding_invalid')
            baseline, original = binding.identity_policy, binding.capsule.identity
            authorized_account(baseline, account)
            with self.authentication.database.read() as auth_db:
                if (not self.authentication.runtime_policy_is_current_in(auth_db, baseline, now=self.authentication._clock())
                        or any(self.authentication._scope_is_revoked(auth_db, RevocationKey(kind, value)) for kind, value in (
                            (RevocationScopeType.PRINCIPAL, original.principal_id),
                            (RevocationScopeType.CREATOR_ACCOUNT, original.creator_account_id),
                            (RevocationScopeType.INSTALLATION, original.installation_id)))):
                    raise RuntimeAuthorizationDenied('text_analysis_identity_unavailable')
            locator = binding.locator
            with self.repository.database.read() as db:
                self.repository._owned_connection(db)
                # Bodies are immutable and were validated when this binding was created.
                row = db.execute('SELECT c.principal_id,c.enrollment_id,c.mode_version,'
                    'e.installation_id,e.creator_account_id,e.principal_id AS enrollment_principal,e.state,'
                    'e.meaning_sha256,p.identity_sha256,p.profile_bytes_sha256,p.capsule_sha256,s.store_id '
                    'FROM text_analysis_account_current c '
                    'JOIN text_analysis_enrollments e ON e.enrollment_id=c.enrollment_id '
                    'JOIN text_analysis_enrollment_capsules p ON p.enrollment_id=e.enrollment_id '
                    'JOIN knowledge_store_identity s ON s.singleton=1 '
                    'WHERE c.installation_id=? AND c.creator_account_id=?',
                    (locator.installation_id, locator.creator_account_id)).fetchone()
            if row is None or (row['principal_id'], row['enrollment_id'], row['mode_version']) != (
                    locator.principal_id, locator.enrollment_id, locator.mode_version):
                raise ValueError('text_analysis_locator_stale')
            if ((row['installation_id'], row['creator_account_id'], row['enrollment_principal'], row['state']) !=
                    (locator.installation_id, locator.creator_account_id, locator.principal_id, binding.current.state)
                    or row['state'] not in ('enabled', 'paused')
                    or row['store_id'] != retained[3]
                    or tuple(row[name] for name in ('meaning_sha256', 'identity_sha256',
                                                   'profile_bytes_sha256', 'capsule_sha256')) != retained[2]):
                raise ValueError('text_analysis_binding_changed')

    @contextmanager
    def retained_visibility(self, binding):
        with self.dependencies.gate:
            self.authorize_binding(binding, binding.locator.creator_account_id)
            with self.dependencies.persistence.recovery_visibility(binding.profile.identity_digest()) as barrier:
                self.authorize_binding(binding, binding.locator.creator_account_id)
                yield barrier
                self.authorize_binding(binding, binding.locator.creator_account_id)


_service = None


def execution_service():
    return _service


def configure_execution_service(service):
    global _service
    if service is not None and type(service) is not TextAnalysisExecutionService:
        raise ValueError('text_analysis_service_invalid')
    _service = service
