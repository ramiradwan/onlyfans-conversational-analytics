"""Read-only durable first-run progress for customer resume."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from app.persistence.auth import AuthenticationStore, SQLiteAuthenticationStore

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
