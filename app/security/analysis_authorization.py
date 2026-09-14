"""Commercial admission for new local analytics processing."""

from __future__ import annotations

from app.security.runtime_policy import (
    AnalysisRunContext,
    RuntimePolicy,
    require_analysis_run,
)

_ANALYSIS_CAPABILITY = "analysis-run"
_ANALYSIS_ARTIFACT_FAMILY = "analysis-artifact"
_ANALYSIS_MAJOR_VERSION = 3


def require_current_analysis_run(policy: RuntimePolicy) -> None:
    """Require current identity/account and CapabilityLicense authority.

    Installation coordinates come from the independently verified identity
    plane. The selected capability, artifact family, and major are local runtime
    inputs, while the seat comes from the commercial authority being bound to
    those local inputs. Missing authority is represented by inert values so
    ``require_analysis_run`` remains the single fail-closed decision function.
    """

    identity = policy.identity_authority
    commercial = policy.commercial_authority
    context = AnalysisRunContext(
        organization_id=(
            identity.organization_id if identity is not None else "missing"
        ),
        installation_id=(
            identity.installation_id if identity is not None else "missing"
        ),
        installation_key_id=(
            identity.installation_key_id if identity is not None else "missing"
        ),
        installation_key_jkt=(
            identity.installation_key_jkt if identity is not None else "missing"
        ),
        seat_id=commercial.seat_id if commercial is not None else "missing",
        seat_scope=(
            commercial.seat_scope if commercial is not None else "missing"
        ),
        capability=_ANALYSIS_CAPABILITY,
        selected_major_version=_ANALYSIS_MAJOR_VERSION,
        artifact_family=_ANALYSIS_ARTIFACT_FAMILY,
    )
    require_analysis_run(policy, context)
