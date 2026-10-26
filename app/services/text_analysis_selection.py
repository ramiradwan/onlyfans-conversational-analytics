"""Explicit fixture-only module selection and current store evidence."""
from dataclasses import dataclass
import hashlib

from app.core.optional_execution import require_optional_execution
from app.nlp.execution_parameters import execution_parameters
from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.authoritative_pair import AuthoritativePairOwner
from app.persistence.text_analysis_authority import (
    FixtureTextAnalysisLifecycle, TextAnalysisAuthorityStore, TextAnalysisCurrentEvidence, trust_digest,
)
from app.persistence.text_analysis_enrollment import TextAnalysisEnrollmentStore
from app.security.text_analysis_authority import TextAnalysisTrust
from app.security.text_analysis_contract import TextAnalysisSchemas
from app.security.text_analysis_policy import TextAnalysisRunContext, encode_run_context


@dataclass(frozen=True, slots=True)
class FixtureTextAnalysisSelection:
    profile_sha256: str
    context: TextAnalysisRunContext


@dataclass(frozen=True, slots=True)
class FixtureTextAnalysisSelectionOwner:
    authority: TextAnalysisAuthorityStore
    enrollments: TextAnalysisEnrollmentStore
    trust: TextAnalysisTrust
    schemas: TextAnalysisSchemas
    installation_id: str
    selections: tuple[FixtureTextAnalysisSelection, ...]
    pair_owner: AuthoritativePairOwner

    def __post_init__(self):
        require_optional_execution()
        if (type(self.authority) is not TextAnalysisAuthorityStore
                or type(self.enrollments) is not TextAnalysisEnrollmentStore
                or type(self.trust) is not TextAnalysisTrust or not self.trust.fixture_only
                or type(self.schemas) is not TextAnalysisSchemas
                or type(self.pair_owner) is not AuthoritativePairOwner
                or self.pair_owner.authentication is not self.authority.authentication
                or self.pair_owner.canonical_database is not self.enrollments.repository.database
                or self.pair_owner.installation_id != self.installation_id
                or type(self.selections) is not tuple or not 1 <= len(self.selections) <= 64
                or any(type(s) is not FixtureTextAnalysisSelection or type(s.context) is not TextAnalysisRunContext
                    or s.context.installation_id != self.installation_id
                    or s.profile_sha256 != s.context.algorithm_profile_sha256 for s in self.selections)
                or len({(s.profile_sha256, s.context.principal_id, s.context.creator_account_id)
                        for s in self.selections}) != len(self.selections)):
            raise ValueError('text_analysis_fixture_selection_invalid')

    @property
    def lifecycle(self):
        return FixtureTextAnalysisLifecycle(self.authority.database, self.enrollments.repository.database,
                                           pair_owner=self.pair_owner)

    def choose(self, digest, identity, dependencies):
        require_optional_execution()
        selected = [s for s in self.selections if s.profile_sha256 == digest
                    and (s.context.principal_id, s.context.creator_account_id) ==
                    (identity.principal_id, identity.creator_account_id)]
        profiles = [(p, closure) for p, closure, state in dependencies.persistence.profiles()
                    if p.identity_digest() == digest and state == 'available']
        if len(selected) != 1 or len(profiles) != 1:
            raise ValueError('text_analysis_selection_unavailable')
        profile, closure = profiles[0]
        context = selected[0].context
        params = execution_parameters(profile, closure)
        if (context.algorithm_artifacts_sha256 != closure or context.policy_identity_sha256 != params.policy_identity_sha256
                or any(getattr(context, key) != getattr(identity, key) for key in
                    ('organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt'))):
            raise ValueError('text_analysis_selection_scope_mismatch')
        # Preparation is a separate bounded worker observation, lost on restart.
        dependencies._admit(profile)
        return profile, params, context

    def evidence(self, policy, context):
        require_optional_execution()
        with self.authority.database.dependency_authority_gate, self.authority.database.read() as db:
            from app.persistence.nlp_dependencies import DependencyPersistence
            state = DependencyPersistence._authority(db)
            row = db.execute('SELECT version FROM text_analysis_authority_references WHERE reference_id=?',
                             (context.reference_id,)).fetchone()
            if row is None:
                raise ValueError('text_analysis_reference_unavailable')
            return TextAnalysisCurrentEvidence(self.trust, trust_digest(self.trust), policy.authorization_epoch.value,
                row['version'], state['epoch'], state['trust_sha256'], state['catalog_sha256'],
                hashlib.sha256(encode_run_context(context)).hexdigest(), context.activation_lineage_sha256,
                context.seat_id, context.installation_key_id, context.installation_key_jkt, True)


@dataclass(frozen=True, slots=True)
class FixtureTextAnalysisExecutionConfiguration:
    authentication: SQLiteAuthenticationStore
    selection: FixtureTextAnalysisSelectionOwner

    def bind(self, source, dependencies):
        require_optional_execution()
        from app.services.text_analysis_execution import TextAnalysisExecutionService
        return TextAnalysisExecutionService(self.authentication, self.selection, source, dependencies)
