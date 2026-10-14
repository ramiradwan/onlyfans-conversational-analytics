"""Creator-scoped read projection, independent of command admission."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.security.analysis_authorization import AnalysisReadiness
from app.security.runtime_policy import RuntimeAuthorizationDenied, require_identity, require_role


@dataclass(frozen=True)
class DesktopAnalysisReadiness:
    availability: Literal["current", "unknown", "unauthorized"] = "unauthorized"
    creator_account_id: str | None = None
    value: AnalysisReadiness | None = None

    def projection(self):
        return {"availability": self.availability, "creator_account_id": self.creator_account_id,
            "value": None if self.value is None else {"schema": "ofca-analysis-readiness/v1",
                "commercial_authority": self.value.commercial_authority,
                "analysis_admission": self.value.analysis_admission}}


def creator_readiness(policy, identity, value):
    """Retain the former readiness endpoint's identity, role and runtime gates."""
    try:
        require_identity(policy)
        require_role(policy, "creator")
    except RuntimeAuthorizationDenied:
        return DesktopAnalysisReadiness()
    if identity is None or (identity.principal_id, identity.creator_account_id) != (
            policy.identity.principal_id, policy.identity.creator_account_id):
        return DesktopAnalysisReadiness()
    from app.security.activation_gate import runtime_is_activated
    if not runtime_is_activated() or value is None:
        return DesktopAnalysisReadiness("unknown", identity.creator_account_id)
    return DesktopAnalysisReadiness("current", identity.creator_account_id, value)
