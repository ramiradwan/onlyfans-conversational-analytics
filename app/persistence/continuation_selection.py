"""Select saved enrollment intent without creating keys or hosted authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.persistence import sqlite_api as sqlite3
from app.persistence.auth import (
    ClaimSubmission,
    InstallationKeyReference,
    ProvisioningCandidate,
    SQLiteAuthenticationStore,
    _claim_submission,
    _installation_key_reference,
    _provisioning_candidate,
)


SelectionReason = Literal[
    "registration_required", "enrollment_unresolved", "enrollment_ambiguous",
    "installation_key_unavailable", "target_unavailable", "lineage_conflict",
    "account_reassignment", "selection_required", "selection_changed",
]


class ContinuationSelectionUnavailable(ValueError):
    def __init__(self, reason: SelectionReason) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ContinuationTarget:
    """Saved proposal only; current hosted approval is checked independently."""

    claim: ClaimSubmission
    key: InstallationKeyReference
    candidate: ProvisioningCandidate
    finalized: bool
    binding_revoked_at: str | None


class ContinuationSelectionStore:
    def __init__(self, authentication: SQLiteAuthenticationStore) -> None:
        self.authentication = authentication

    def select(self, *, association_request_id: str | None = None) -> ContinuationTarget:
        """Use only an exact owner-held intent or a unique saved target.

        The optional reference comes from an admitted local context, never raw
        browser signing coordinates. Call ``require_current`` in the transaction
        that fixes a new context before doing external work.
        """
        with self.authentication.database.transaction(immediate=False) as connection:
            return self.select_in_transaction(connection, association_request_id=association_request_id)

    def require_current(
        self, connection: sqlite3.Connection, target: ContinuationTarget,
    ) -> None:
        current = self.select_in_transaction(
            connection, association_request_id=target.candidate.association_request_id,
        )
        if current != target:
            raise ContinuationSelectionUnavailable("selection_changed")

    def select_in_transaction(
        self, connection: sqlite3.Connection, *, association_request_id: str | None = None,
    ) -> ContinuationTarget:
        if connection.execute(
            "SELECT 1 FROM provisioning_claim_submissions WHERE state = 'submitted' LIMIT 1"
        ).fetchone() is not None:
            raise ContinuationSelectionUnavailable("enrollment_unresolved")
        claims = connection.execute(
            "SELECT * FROM provisioning_claim_submissions WHERE state = 'consumed' LIMIT 2"
        ).fetchall()
        if not claims:
            raise ContinuationSelectionUnavailable("registration_required")
        if len(claims) != 1:
            raise ContinuationSelectionUnavailable("enrollment_ambiguous")
        claim = _claim_submission(claims[0])
        key_row = connection.execute(
            "SELECT * FROM installation_key_reference WHERE singleton = 1 AND activated_at IS NOT NULL"
        ).fetchone()
        if key_row is None:
            raise ContinuationSelectionUnavailable("installation_key_unavailable")
        key = _installation_key_reference(key_row)
        rows = connection.execute(
            "SELECT * FROM provisioning_candidates WHERE installation_id = ? "
            "AND state IN ('pending', 'approved')",
            (claim.installation_id,),
        ).fetchall()
        if any(
            row["organization_id"] != claim.organization_id
            or row["onboarding_transaction_id"] != claim.onboarding_transaction_id
            for row in rows
        ):
            raise ContinuationSelectionUnavailable("lineage_conflict")
        # Competing histories for one account are not a meaningful user choice.
        if len({row["creator_account_id"] for row in rows}) != len(rows):
            raise ContinuationSelectionUnavailable("lineage_conflict")
        bindings = connection.execute("SELECT * FROM authorized_account_bindings LIMIT 2").fetchall()
        if len(bindings) > 1:
            raise ContinuationSelectionUnavailable("lineage_conflict")
        binding = bindings[0] if bindings else None
        if binding is not None:
            if binding["installation_id"] != claim.installation_id:
                raise ContinuationSelectionUnavailable("lineage_conflict")
            if association_request_id is not None and association_request_id != binding["association_request_id"]:
                raise ContinuationSelectionUnavailable("account_reassignment")
            matches = [row for row in rows if row["association_request_id"] == binding["association_request_id"]]
            if (len(matches) != 1 or matches[0]["creator_account_id"] != binding["creator_account_id"]
                    or matches[0]["state"] != "approved"):
                raise ContinuationSelectionUnavailable("lineage_conflict")
            selected = matches[0]
        elif association_request_id is not None:
            matches = [row for row in rows if row["association_request_id"] == association_request_id]
            if len(matches) != 1:
                raise ContinuationSelectionUnavailable("target_unavailable")
            selected = matches[0]
        else:
            pending = [row for row in rows if row["state"] == "pending"]
            if len(pending) > 1:
                raise ContinuationSelectionUnavailable("lineage_conflict")
            if pending:
                selected = pending[0]
            elif len(rows) == 1:
                selected = rows[0]
            elif not rows:
                raise ContinuationSelectionUnavailable("target_unavailable")
            else:
                # No qualified readable names exist for distinguishing these rows.
                raise ContinuationSelectionUnavailable("selection_required")
        return ContinuationTarget(
            claim=claim, key=key, candidate=_provisioning_candidate(selected),
            finalized=binding is not None,
            binding_revoked_at=None if binding is None else binding["revoked_at"],
        )
