"""Compose the registered package's workers with the local authority stores."""
from dataclasses import dataclass

from app.analytics.nlp_contribution_sources import ContributionSourceGuard
from app.core.optional_execution import installed_execution_release
from app.core.startup_maintenance import runtime_root_lease
from app.nlp.execution_release import ExecutionRelease
from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.authoritative_pair import AuthoritativePairOwner
from app.persistence.contributions import ContributionRepository
from app.persistence.text_analysis_authority import TextAnalysisAuthorityStore
from app.persistence.text_analysis_enrollment import TextAnalysisEnrollmentStore
from app.persistence.text_analysis_jobs import TextAnalysisJobStore
from app.persistence.text_analysis_preview import TextAnalysisPreviewStore
from app.security.text_analysis_contract import TextAnalysisSchemas
from app.services.installed_text_analysis import InstalledTextAnalysisAuthorities, _need
from app.services.installed_text_analysis_selection import InstalledLicenseSelection, InstalledPreviewSelection


@dataclass(frozen=True, slots=True)
class InstalledExecutionConfiguration:
    authentication: SQLiteAuthenticationStore
    release: ExecutionRelease

    def installation_id(self):
        auth = self.authentication
        with auth.database.read() as db:
            key = auth.installation_key_reference(connection=db)
            if key is None:
                return None
            rows = db.execute(
                "SELECT DISTINCT installation_id FROM verified_grant_references "
                "WHERE grant_type='installation_grant' AND revoked_at IS NULL AND compact_jws IS NOT NULL "
                "AND installation_key_id=? AND installation_key_jkt=? LIMIT 2",
                (key.installation_key_id, key.installation_key_jkt),
            ).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def bind(self, source, dependencies):
        from app.services.text_analysis_execution import TextAnalysisExecutionService
        installation = self.installation_id()
        _need(installation is not None and self.release == installed_execution_release()
              and dependencies.preparation_admission.execution == self.release)
        database = source.history.database
        repository = ContributionRepository(database, source_guard=ContributionSourceGuard())
        pair = AuthoritativePairOwner(self.authentication, database, installation_id=installation,
                                      root_lease=runtime_root_lease())
        owner = InstalledTextAnalysisAuthorities(self.release, TextAnalysisAuthorityStore(self.authentication),
                                                 pair, TextAnalysisSchemas())
        selection = InstalledLicenseSelection(owner, TextAnalysisEnrollmentStore(TextAnalysisJobStore(repository)))
        return TextAnalysisExecutionService(self.authentication, selection, source, dependencies)

    def acquisition(self, authorities):
        from app.core.customer_release import CUSTOMER_RELEASE_PATH, load_customer_release_config
        from app.provisioning.claim_submission import installation_proof_authority
        from app.security.text_analysis_contract import SUPPORTED
        from app.services.text_analysis_native_acquisition import compose_installed_text_analysis_acquisition
        release = load_customer_release_config(CUSTOMER_RELEASE_PATH, require_hosted=True)

        def eligible(policy, kind, binding):
            if self.release != installed_execution_release() or kind not in ('paid', 'preview'):
                return False
            with authorities._session(policy) as (_, _, identity):
                return identity.role == 'creator' and binding == {k: getattr(identity, k) for k in (
                    'organization_id', 'installation_id', 'installation_key_id', 'installation_key_jkt')}

        return compose_installed_text_analysis_acquisition(
            self.authentication, installation_proof_authority(self.authentication), authorities,
            origin=release.hosted_api_origin, payment_mode='test', eligible=eligible,
            supported_profiles={'preview': SUPPORTED, 'paid': SUPPORTED},
        )


@dataclass(frozen=True, slots=True)
class InstalledPreviewConfiguration:
    authentication: SQLiteAuthenticationStore

    def bind(self, source, dependencies):
        from app.services.text_analysis_preview import TextAnalysisPreviewService
        execution = dependencies.execution_owner
        _need(execution is not None and type(execution.selection) is InstalledLicenseSelection
              and execution.authentication is self.authentication and execution.source is source)
        selection = InstalledPreviewSelection(execution.selection.installed_authorities,
                                             TextAnalysisPreviewStore(execution.repository))
        return TextAnalysisPreviewService(self.authentication, selection, source, dependencies)


def installed_findings_configuration(authentication, dependencies, release):
    from app.analytics.nlp_runtime import FindingsConfiguration
    _need(type(release) is ExecutionRelease and release == installed_execution_release())

    def require_enrollment(_account):
        raise ValueError('text_analysis_enrollment_required')

    return FindingsConfiguration(dependencies.factory, require_enrollment,
                                 execution=InstalledExecutionConfiguration(authentication, release),
                                 preview=InstalledPreviewConfiguration(authentication))
