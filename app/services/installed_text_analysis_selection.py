"""Select installed workers from the prepared profile and verified account authority."""
from dataclasses import dataclass, replace
from typing import ClassVar

from app.core.optional_execution import installed_execution_release, require_optional_execution
from app.nlp.execution_parameters import execution_parameters
from app.nlp.preview_parameters import preview_parameters
from app.persistence.text_analysis_installed import InstalledTextAnalysisRecords
from app.persistence.text_analysis_enrollment import TextAnalysisEnrollmentStore
from app.persistence.text_analysis_preview import TextAnalysisPreviewStore
from app.security.text_analysis_identity import TextAnalysisIdentityOwner
from app.security.text_analysis_policy import TextAnalysisRunContext, encode_run_context
from app.services.installed_text_analysis import InstalledTextAnalysisAuthorities, _need


@dataclass(frozen=True, slots=True)
class _Selection:
    installed_authorities: InstalledTextAnalysisAuthorities

    @property
    def pair_owner(self):
        return self.installed_authorities.pair_owner

    @property
    def installation_id(self):
        return self.pair_owner.installation_id

    @property
    def authority(self):
        return self.installed_authorities.authority

    @property
    def lifecycle(self):
        owner = self.installed_authorities
        _need(type(owner) is InstalledTextAnalysisAuthorities)
        return InstalledTextAnalysisRecords(owner.release, owner.authority, owner.pair_owner, owner.schemas)

    def evidence(self, policy, context):
        return self.authority.installed_current_evidence(policy, context, lifecycle=self.lifecycle, kind=self.kind)

    def choose(self, digest, identity, dependencies, *, selection_policy=None):
        selection, _ = self._choose(digest, identity, dependencies, selection_policy)
        return selection

    def choose_for_session(self, digest, identity, dependencies, session, *, selection_policy=None):
        auth = self.authority.authentication
        session_owner = TextAnalysisIdentityOwner(auth)
        with auth.database.dependency_authority_gate:
            _need(session_owner.capture(session) == identity)
            selection, activation = self._choose(digest, identity, dependencies, selection_policy)
            if activation is not None and activation.activated:
                epoch = activation.policy.authorization_epoch
                _need(epoch.value == session.authorization_epoch.value + 1)
                session = replace(session, authorization_epoch=epoch)
            _need(session_owner.capture(session) == identity)
            return (*selection, session, session_owner.rebuild(identity))

    def _choose(self, digest, identity, dependencies, selection_policy):
        require_execution_selection(self)
        owner = self.installed_authorities
        _need(digest == owner.release.profile_sha256 and dependencies.preparation_admission
              and dependencies.preparation_admission.execution == owner.release)
        candidates = [(p, c) for p,c,state in dependencies.persistence.profiles()
                      if p.identity_digest() == digest and state == 'available']
        _need(len(candidates) == 1)
        profile,closure = candidates[0]
        dependencies._admit(profile)
        _need(dependencies.ready(profile))
        if self.kind == 'preview':
            from app.nlp.preview_parameters import RECURRENCE_SELECTION_POLICY
            _need(selection_policy == RECURRENCE_SELECTION_POLICY)
            params = preview_parameters(profile, closure, selection_policy)
        else:
            _need(selection_policy is None)
            params = execution_parameters(profile,closure)
        policy = TextAnalysisIdentityOwner(self.authority.authentication).rebuild(identity)
        lifecycle = self.lifecycle
        with self.authority.database.transaction() as db:
            ref = owner.current_in(db, identity, self.kind, for_execution=True)
            _need(ref is not None)
            state = self.authority.reference_state_in(db, ref.reference_id)
        context = TextAnalysisRunContext(reference_id=ref.reference_id, compact_jws_sha256=ref.compact_jws_sha256,
            principal_id=identity.principal_id, creator_account_id=identity.creator_account_id,
            organization_id=identity.organization_id, installation_id=identity.installation_id,
            installation_key_id=identity.installation_key_id, installation_key_jkt=identity.installation_key_jkt,
            seat_id=ref.claims['seat_id'], seat_scope=ref.claims['seat_scope'],
            licensed_major_version=1,selected_major_version=1,
            artifact_family='text-analysis-artifact',algorithm_profile_sha256=digest,algorithm_artifacts_sha256=closure,
            policy_identity_sha256=params.policy_identity_sha256)
        activation = None
        if state == 'staged':
            evidence = self.evidence(policy,context)
            if self.kind == 'preview':
                with self.authority.transition_fence(policy,context,kind='preview',phase='import',evidence=evidence,
                        lifecycle=lifecycle) as held, lifecycle.canonical_database.transaction() as db:
                    self.store.jobs.import_preview_in(db,encode_run_context(context),policy=held)
            activation = self.authority.activate_reference(policy,context,kind=self.kind,evidence=evidence,lifecycle=lifecycle)
        return (profile,params,context), activation


@dataclass(frozen=True, slots=True)
class InstalledLicenseSelection(_Selection):
    enrollments: TextAnalysisEnrollmentStore
    kind: ClassVar[str] = 'license'


@dataclass(frozen=True, slots=True)
class InstalledPreviewSelection(_Selection):
    store: TextAnalysisPreviewStore
    kind: ClassVar[str] = 'preview'


def require_execution_selection(selection):
    if type(selection) in (InstalledLicenseSelection, InstalledPreviewSelection):
        _need(selection.installed_authorities.release == installed_execution_release())
    else:
        require_optional_execution()
