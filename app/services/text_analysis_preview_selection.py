"""Explicit fixture-only selection for one-shot preview execution."""
from dataclasses import dataclass

from app.analytics.nlp_preview import SELECTION_POLICY
from app.core.optional_execution import require_optional_execution
from app.nlp.preview_parameters import preview_parameters
from app.persistence.authoritative_pair import AuthoritativePairOwner
from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.text_analysis_authority import (
    FixtureTextAnalysisLifecycle, TextAnalysisAuthorityStore,
)
from app.persistence.text_analysis_preview import TextAnalysisPreviewStore
from app.security.text_analysis_authority import TextAnalysisTrust
from app.security.text_analysis_contract import TextAnalysisSchemas
from app.security.text_analysis_policy import TextAnalysisRunContext


@dataclass(frozen=True, slots=True)
class FixtureTextAnalysisPreviewSelection:
    profile_sha256: str
    context: TextAnalysisRunContext


@dataclass(frozen=True, slots=True)
class FixtureTextAnalysisPreviewSelectionOwner:
    authority: TextAnalysisAuthorityStore
    store: TextAnalysisPreviewStore
    trust: TextAnalysisTrust
    schemas: TextAnalysisSchemas
    installation_id: str
    selections: tuple[FixtureTextAnalysisPreviewSelection, ...]
    pair_owner: AuthoritativePairOwner

    def __post_init__(self):
        require_optional_execution()
        if (type(self.authority) is not TextAnalysisAuthorityStore or type(self.store) is not TextAnalysisPreviewStore
                or type(self.trust) is not TextAnalysisTrust or not self.trust.fixture_only
                or type(self.schemas) is not TextAnalysisSchemas or type(self.pair_owner) is not AuthoritativePairOwner
                or self.pair_owner.authentication is not self.authority.authentication
                or self.pair_owner.canonical_database is not self.store.repository.database
                or self.pair_owner.installation_id != self.installation_id
                or type(self.selections) is not tuple or not 1 <= len(self.selections) <= 64
                or any(type(s) is not FixtureTextAnalysisPreviewSelection
                    or type(s.context) is not TextAnalysisRunContext
                    or s.context.installation_id != self.installation_id
                    or s.profile_sha256 != s.context.algorithm_profile_sha256 for s in self.selections)
                or len({(s.profile_sha256, s.context.principal_id, s.context.creator_account_id)
                        for s in self.selections}) != len(self.selections)):
            raise ValueError('text_analysis_preview_selection_invalid')

    @property
    def lifecycle(self):
        return FixtureTextAnalysisLifecycle(self.authority.database, self.store.repository.database, pair_owner=self.pair_owner)

    def choose(self, digest, identity, dependencies):
        require_optional_execution()
        selected = [s for s in self.selections if s.profile_sha256 == digest
            and (s.context.principal_id, s.context.creator_account_id) == (identity.principal_id, identity.creator_account_id)]
        profiles = [(p, closure) for p, closure, state in dependencies.persistence.profiles()
                    if p.identity_digest() == digest and state == 'available']
        if len(selected) != 1 or len(profiles) != 1:
            raise ValueError('text_analysis_preview_selection_unavailable')
        profile, closure = profiles[0]
        params, context = preview_parameters(profile, closure, SELECTION_POLICY), selected[0].context
        if (context.algorithm_artifacts_sha256 != closure or context.policy_identity_sha256 != params.policy_identity_sha256
                or any(getattr(context, key) != getattr(identity, key) for key in
                    ('organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt'))):
            raise ValueError('text_analysis_preview_selection_scope_invalid')
        self.authority.require_active_preview(context.reference_id)
        dependencies._admit(profile)
        return profile, params, context

    def evidence(self, policy, context):
        require_optional_execution()
        return self.authority.fixture_current_evidence(policy, context, trust=self.trust)


@dataclass(frozen=True, slots=True)
class FixtureTextAnalysisPreviewConfiguration:
    authentication: SQLiteAuthenticationStore
    selection: FixtureTextAnalysisPreviewSelectionOwner

    def bind(self, source, dependencies):
        require_optional_execution()
        from app.services.text_analysis_preview import TextAnalysisPreviewService
        return TextAnalysisPreviewService(self.authentication, self.selection, source, dependencies)
