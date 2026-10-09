"""Read-only durable first-run progress for customer resume."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from app.persistence.auth import (
    AuthenticationStore, SQLiteAuthenticationStore, RevocationKey, RevocationScopeType,
)
from app.security.grant_types import CREATOR_ACCOUNT_BINDING, MEMBERSHIP_SNAPSHOT

ProvisioningStage = Literal[
    "registration_required",
    "creator_confirmation_required",
    "creator_approval_pending",
    "finalization_ready",
    "recovery_required",
]


@dataclass(frozen=True, slots=True)
class ProvisioningProgress:
    """Nonsecret browser-resume coordinates plus one customer progress state."""

    stage: ProvisioningStage
    association_request_id: str | None = None
    creator_account_id: str | None = None

    def public(self) -> dict[str, str | None]:
        return {
            "stage": self.stage,
            "association_request_id": self.association_request_id,
            "creator_account_id": self.creator_account_id,
        }


def durable_provisioning_progress(
    open_store: Callable[[], AuthenticationStore],
) -> Callable[[], dict[str, str | None]]:
    """Build a status reader from durable provisioning state only.

    No browser-supplied state participates. An unresolved claim or ambiguous
    durable state fails to the recovery state instead of asking the customer to
    spend a setup code again.
    """

    def read() -> dict[str, str | None]:
        store = open_store()
        if store.unresolved_claim_submissions():
            return ProvisioningProgress("recovery_required").public()
        consumed = store.consumed_claim_submissions()
        if not consumed:
            return ProvisioningProgress("registration_required").public()
        if len(consumed) != 1 or not isinstance(store, SQLiteAuthenticationStore):
            return ProvisioningProgress("recovery_required").public()
        installation_id = consumed[0].installation_id
        try:
            with store.database.read() as connection:
                rows = connection.execute(
                    """
                    SELECT association_request_id, creator_account_id, state
                    FROM provisioning_candidates
                    WHERE installation_id = ?
                      AND state IN ('pending', 'approved')
                    """,
                    (installation_id,),
                ).fetchall()
        except Exception:
            return ProvisioningProgress("recovery_required").public()
        if not rows:
            return ProvisioningProgress("creator_confirmation_required").public()
        if len(rows) != 1:
            return ProvisioningProgress("recovery_required").public()
        row = rows[0]
        association_request_id = str(row["association_request_id"])
        creator_account_id = str(row["creator_account_id"])
        if row["state"] == "pending":
            stage: ProvisioningStage = "creator_approval_pending"
        elif row["state"] == "approved":
            stage = "finalization_ready"
        else:
            return ProvisioningProgress("recovery_required").public()
        return ProvisioningProgress(
            stage,
            association_request_id=association_request_id,
            creator_account_id=creator_account_id,
        ).public()

    return read


def scoped_durable_provisioning_progress(
    open_store: Callable[[], AuthenticationStore],
) -> Callable[[str], dict[str, str | None]]:
    """Read only the target fixed by the authenticated current context."""
    initial = durable_provisioning_progress(open_store)

    def read(journey_id: str) -> dict[str, str | None]:
        from app.persistence.continuation_selection import ContinuationSelectionUnavailable
        from app.persistence.installation_continuation import InstallationContinuationStore
        from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable

        store = open_store()
        if not isinstance(store, SQLiteAuthenticationStore):
            return ProvisioningProgress("recovery_required").public()
        try:
            journey = OnboardingJourneyStore(store).require_current(journey_id)
            if journey["kind"] == "initial-enrollment":
                if journey["scope_json"] is not None:
                    import json
                    scope = json.loads(journey["scope_json"])
                    candidate = store.provisioning_candidate(scope["association_request_id"])
                    claims = store.consumed_claim_submissions()
                    if (store.unresolved_claim_submissions() or len(claims) != 1
                            or claims[0].installation_id != journey["installation_id"]
                            or claims[0].organization_id != scope["organization_id"]
                            or claims[0].onboarding_transaction_id != scope["onboarding_transaction_id"]):
                        return ProvisioningProgress("recovery_required").public()
                    if candidate is None:
                        return ProvisioningProgress("creator_confirmation_required").public()
                    if (candidate.installation_id != journey["installation_id"]
                            or candidate.organization_id != scope["organization_id"]
                            or candidate.onboarding_transaction_id != scope["onboarding_transaction_id"]
                            or candidate.creator_account_id != scope["intended_creator_id"]
                            or candidate.state.value not in {"pending", "approved"}):
                        return ProvisioningProgress("recovery_required").public()
                    return ProvisioningProgress(
                        "finalization_ready" if candidate.state.value == "approved" else "creator_approval_pending",
                        candidate.association_request_id, candidate.creator_account_id,
                    ).public()
                return initial()
            current = InstallationContinuationStore(store).require_current(journey_id)
            target = current["target"]
            if (target.binding_revoked_at is not None
                    or current["state"] in {"expired", "revoked"}
                    or current["provider_state"] in {"expired", "revoked"}
                    or (current["provider_expires_at"] is not None
                        and datetime.fromisoformat(current["provider_expires_at"].replace("Z", "+00:00")) <= store._now())):
                return ProvisioningProgress("recovery_required").public()
            stage = ("finalization_ready" if target.candidate.state.value == "approved"
                     and current["state"] == "completed" and current["provider_state"] == "approved"
                     and current["provider_expires_at"] is not None
                     else "creator_approval_pending")
            if stage == "finalization_ready":
                relevant = tuple(grant for grant in store.verified_grants()
                    if grant.organization_id == target.candidate.organization_id
                    and grant.installation_id == target.candidate.installation_id
                    and grant.installation_key_id == target.key.installation_key_id
                    and grant.installation_key_jkt == target.key.installation_key_jkt
                    and (grant.grant_type != CREATOR_ACCOUNT_BINDING
                         or grant.creator_account_id == target.candidate.creator_account_id))
                scopes = (
                    RevocationKey(RevocationScopeType.INSTALLATION, target.candidate.installation_id),
                    RevocationKey(RevocationScopeType.CREATOR_ACCOUNT, target.candidate.creator_account_id),
                    *(RevocationKey(RevocationScopeType.PRINCIPAL, grant.subject)
                      for grant in relevant if grant.grant_type == MEMBERSHIP_SNAPSHOT),
                    *(RevocationKey(RevocationScopeType.VERIFIED_GRANT, grant.reference_id)
                      for grant in relevant),
                )
                if (not any(grant.grant_type == CREATOR_ACCOUNT_BINDING for grant in relevant)
                        or any(store.scope_is_revoked(scope) for scope in scopes)):
                    return ProvisioningProgress("recovery_required").public()
            return ProvisioningProgress(stage, target.candidate.association_request_id,
                                        target.candidate.creator_account_id).public()
        except (ContinuationSelectionUnavailable, OnboardingJourneyUnavailable, KeyError, ValueError, TypeError):
            return ProvisioningProgress("recovery_required").public()

    return read
