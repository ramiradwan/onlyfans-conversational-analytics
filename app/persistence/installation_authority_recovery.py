"""Exact previous-grant recovery evidence and durable operation journal.

Only a trusted owner may reserve work. Evidence is derived from the encrypted
installation record; rendering, browser messages and caller-selected IDs never
supply it. No grant, sign-in or account approval is admitted by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.persistence.auth import (
    RevocationKey, RevocationScopeType, _new_uuid7, _time_text, _verified_grant_reference, refresh_window_open,
)
from app.persistence.catchup_events import capture_authority_change
from app.persistence.lost_enrollment_evidence import LostEnrollmentEvidence
from app.security.capability_license_redemption_journal import provider_origin
from app.security.grant_types import ACCOUNT_AUTHORITY_GRANT_TYPES
from app.security.hosted_grants import CLAIM_PROFILE_V2, HostedGrantUnavailable, grant_offline_grace_seconds
from app.security.initial_handoff import timestamp
from app.security.installation_authority_recovery import (
    AuthorityRecoveryRefused, contract, exact_request, scope_digest, validate_result, verify_delivery_bundle,
    verify_lost_enrollment_bundle, WITHDRAWALS,
)
from app.security.installation_continuation import canonical_request

PREPARE_LIMIT = 2
DELIVERY_LIMIT = 3
BACKOFF = (15, 30, 60)


@dataclass(frozen=True)
class PriorAuthority:
    scope: dict
    claim_id: str
    enrolled_at: datetime
    key: object
    grants: tuple

    @property
    def membership(self):
        return next(grant for grant in self.grants if grant.grant_type == "membership_snapshot")


def previous_authority(store, connection):
    """Resolve one exact previously verified set after ordinary renewal ends.

    Missing grants do not prove lost enrollment. A separate owner must establish
    the original enrollment actor before using that contract branch.
    """
    key = store.installation_key_reference(connection=connection)
    now = store._now()
    if key is None:
        raise HostedGrantUnavailable("Installation authority recovery is unavailable")
    rows = connection.execute("SELECT * FROM verified_grant_references WHERE grant_type IN (?,?,?)",
        ACCOUNT_AUTHORITY_GRANT_TYPES).fetchall()
    active = [_verified_grant_reference(row) for row in rows if row["revoked_at"] is None]
    if any((grant.installation_key_id, grant.installation_key_jkt) != (key.installation_key_id, key.installation_key_jkt)
            or store._grant_is_withdrawn(connection, grant) for grant in active):
        raise HostedGrantUnavailable("Prior installation authority changed")
    selected = {}
    for grant in active:
        if grant.grant_type in selected:
            raise HostedGrantUnavailable("Prior installation authority is ambiguous")
        selected[grant.grant_type] = grant
    installation = selected.get("installation_grant")
    membership = selected.get("membership_snapshot")
    binding = selected.get("creator_account_binding")
    if installation is None or membership is None or not membership.membership_id:
        raise HostedGrantUnavailable("Prior installation authority is incomplete")
    coordinates = (installation.organization_id, installation.installation_id, installation.issuer, installation.subject)
    if (not all(coordinates) or any((grant.organization_id, grant.installation_id, grant.issuer, grant.subject) != coordinates
            or grant.valid_from > now for grant in active)
            or store._scope_is_revoked(connection, RevocationKey(RevocationScopeType.PRINCIPAL, membership.subject))):
        raise HostedGrantUnavailable("Prior installation authority changed")
    if refresh_window_open(installation, active, now):
        raise HostedGrantUnavailable("Ordinary grant renewal remains available")
    claims = connection.execute("SELECT * FROM provisioning_claim_submissions WHERE state='consumed'").fetchall()
    if len(claims) != 1:
        raise HostedGrantUnavailable("Original enrollment is ambiguous")
    claim = claims[0]
    if (claim["organization_id"], claim["installation_id"], claim["claim_profile"]) != (
            installation.organization_id, installation.installation_id, CLAIM_PROFILE_V2) or claim["enrolled_at"] is None:
        raise HostedGrantUnavailable("Original enrollment is unavailable")
    enrolled = datetime.fromisoformat(claim["enrolled_at"])
    if enrolled > now or any(int(grant.valid_from.timestamp()) < int(enrolled.timestamp()) for grant in active):
        raise HostedGrantUnavailable("Original enrollment time changed")
    accounts = connection.execute("SELECT * FROM authorized_account_bindings").fetchall()
    creator_scope = {"kind": "none"}
    if binding is not None:
        if (len(accounts) != 1 or accounts[0]["revoked_at"] is not None or
                (accounts[0]["creator_account_id"], accounts[0]["installation_id"]) !=
                (binding.creator_account_id, installation.installation_id)
                or binding.approval_revision != 1 or not binding.approval_id
                or binding.creator_account_id not in (membership.allowed_creator_account_ids or ())):
            raise HostedGrantUnavailable("Prior creator approval is unavailable")
        account = accounts[0]
        candidate = connection.execute("SELECT * FROM provisioning_candidates WHERE association_request_id=?",
            (account["association_request_id"],)).fetchone()
        linked = {row["grant_reference_id"] for row in connection.execute(
            "SELECT grant_reference_id FROM authorized_account_binding_grants WHERE creator_account_id=?", (binding.creator_account_id,))}
        if (candidate is None or candidate["state"] != "approved"
                or (candidate["organization_id"], candidate["installation_id"], candidate["onboarding_transaction_id"], candidate["creator_account_id"]) !=
                (installation.organization_id, installation.installation_id, claim["onboarding_transaction_id"], binding.creator_account_id)
                or not {grant.reference_id for grant in active} <= linked):
            raise HostedGrantUnavailable("Prior creator approval changed")
        creator_scope = {"kind": "approved", "creator_account_id": binding.creator_account_id,
            "approval_id": binding.approval_id, "approval_revision": binding.approval_revision, "binding_jti": binding.grant_identifier}
    elif accounts or any(row["grant_type"] == "creator_account_binding" for row in rows):
        raise HostedGrantUnavailable("Prior creator approval cannot be discarded")
    scope = {"organization_id": installation.organization_id, "installation_id": installation.installation_id,
        "installation_key_id": key.installation_key_id, "installation_key_jkt": key.installation_key_jkt,
        "enrollment_transaction_id": claim["onboarding_transaction_id"], "evidence": {"kind": "previous-grants",
            "installation_jti": installation.grant_identifier, "membership_jti": membership.grant_identifier,
            "creator_scope": creator_scope}}
    return PriorAuthority(scope, claim["claim_id"], enrolled, key, tuple(active))


class InstallationAuthorityRecoveries:
    def __init__(self, store, *, origin):
        self.store = self.authentication = store
        self.origin = provider_origin(origin)
        self.lost_enrollment = LostEnrollmentEvidence(store, self.origin)

    def row(self, connection, recovery_id):
        row = connection.execute("SELECT * FROM installation_authority_recoveries WHERE recovery_id=?", (recovery_id,)).fetchone()
        return None if row is None else dict(row)

    def eligible(self, connection):
        return self._check_lineage(connection, previous_authority(self.store, connection))

    def eligible_lost(self, connection):
        """Separate owner-only admission; never fall back from prior grants."""
        return self._check_lineage(connection, self.lost_enrollment.eligible(connection))

    def _check_lineage(self, connection, evidence):
        rows = connection.execute("SELECT * FROM installation_authority_recoveries WHERE installation_id=? ORDER BY created_at DESC",
            (evidence.scope["installation_id"],)).fetchall()
        for row in rows:
            # No new ID can cure an unresolved operation or withdrawal. An
            # independent recovery after explicit expiry needs new admission.
            if row["state"] == "refused" and row["refusal_reason"] in WITHDRAWALS:
                raise HostedGrantUnavailable("Installation recovery authority was withdrawn")
            if row["state"] not in {"admitted", "refused"}:
                raise HostedGrantUnavailable("Original authority recovery is unresolved")
            if row["state"] == "refused" and (row["refusal_reason"] != "expired" or row["committed_at"] is not None):
                raise HostedGrantUnavailable("Original authority recovery was refused")
            if row["state"] == "refused" and self.store._now().timestamp() < row["created_at"] + 1860:
                raise HostedGrantUnavailable("Original recovery deadline remains live")
        if len(rows) >= 128:
            raise HostedGrantUnavailable("Authority recovery retention exceeded")
        return evidence

    def reserve(self, connection, *, authorize):
        authorize(connection=connection)
        return self._reserve(connection, self.eligible(connection), authorize=authorize)

    def reserve_lost(self, connection, *, authorize):
        """Only the owner of an explicitly admitted lost-enrollment recovery."""
        authorize(connection=connection)
        return self._reserve(connection, self.eligible_lost(connection), authorize=authorize)

    def _reserve(self, connection, evidence, *, authorize):
        now = self.store._now()
        request = exact_request(_new_uuid7(now), evidence.scope)
        connection.execute("INSERT INTO installation_authority_recoveries "
            "(recovery_id,installation_id,scope_digest,request_json,provider_origin,enrollment_claim_id,created_at,retry_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (request["recovery_id"], evidence.scope["installation_id"], scope_digest(evidence.scope),
                canonical_request(request).decode(), self.origin, evidence.claim_id, now.timestamp(), now.timestamp()))
        authorize(connection=connection)
        return request

    def exact(self, connection, request, *, authorize):
        authorize(connection=connection)
        row = self.row(connection, request["recovery_id"])
        if row is None or row["request_json"] != canonical_request(request).decode():
            raise HostedGrantUnavailable("Original authority recovery changed")
        # The persisted, immutable operation chooses the evidence branch.
        # The caller cannot turn an ordinary operation into lost enrollment.
        if request["scope"]["evidence"]["kind"] == "lost-enrollment":
            evidence = self.lost_enrollment.eligible(connection)
        else:
            evidence = previous_authority(self.store, connection)
        if (row["provider_origin"] != self.origin or row["enrollment_claim_id"] != evidence.claim_id
                or row["scope_digest"] != scope_digest(evidence.scope)
                or row["request_json"] != canonical_request(exact_request(row["recovery_id"], evidence.scope)).decode()
                or row["request_json"] != canonical_request(request).decode()):
            raise HostedGrantUnavailable("Original authority recovery changed")
        return row, evidence

    def claim(self, connection, request, verb, *, authorize):
        row, _ = self.exact(connection, request, authorize=authorize)
        now = self.store._now().timestamp()
        if now < row["retry_at"]:
            return None
        if verb == "prepare":
            if (row["state"] not in {"reserved", "prepare-claimed"} or row["prepare_attempts"] >= PREPARE_LIMIT
                    or now >= row["created_at"] + 1800):
                return None
            attempt = row["prepare_attempts"] + 1
            connection.execute("UPDATE installation_authority_recoveries SET state='prepare-claimed',prepare_attempts=?,retry_at=? WHERE recovery_id=?",
                (attempt, now + BACKOFF[attempt-1], row["recovery_id"]))
        elif verb == "deliver":
            # A prepared operation might already have committed, even after its
            # presentation expires. Exact delivery cannot create new authority.
            if row["state"] not in {"prepared", "committed"} or row["delivery_attempts"] >= DELIVERY_LIMIT:
                return None
            attempt = row["delivery_attempts"] + 1
            connection.execute("UPDATE installation_authority_recoveries SET delivery_attempts=?,retry_at=? WHERE recovery_id=?",
                (attempt, now + BACKOFF[attempt-1], row["recovery_id"]))
        else:
            raise ValueError("Unsupported authority recovery operation")
        authorize(connection=connection)
        return attempt

    def guard(self, connection, request, verb, attempt, *, authorize):
        row, evidence = self.exact(connection, request, authorize=authorize)
        if (verb == "prepare" and row["state"] == "prepare-claimed" and row["prepare_attempts"] == attempt
                and self.store._now().timestamp() < row["created_at"] + 1800
                or verb == "deliver" and row["state"] in {"prepared", "committed"} and row["delivery_attempts"] == attempt):
            if attempt > 0:
                return row, evidence
        raise HostedGrantUnavailable("Authority recovery send was not admitted")

    def record_preparation(self, connection, request, attempt, result, *, authorize):
        row, _ = self.guard(connection, request, "prepare", attempt, authorize=authorize)
        result = validate_result("prepare", request, result, now=self.store._now())
        if result["status"] == "prepared":
            if timestamp(result["expires_at"]).timestamp() > row["created_at"] + 1860:
                raise HostedGrantUnavailable("Authority recovery deadline changed")
            connection.execute("UPDATE installation_authority_recoveries SET state='prepared',source_reference=?,source_expires_at=? WHERE recovery_id=?",
                (result["source_reference"], result["expires_at"], row["recovery_id"]))
        else:
            connection.execute("UPDATE installation_authority_recoveries SET state='committed',committed_at=? WHERE recovery_id=?",
                (result["committed_at"], row["recovery_id"]))
        authorize(connection=connection)

    def record_refusal(self, connection, request, verb, attempt, refusal, *, authorize):
        row, _ = self.guard(connection, request, verb, attempt, authorize=authorize)
        if not isinstance(refusal, AuthorityRecoveryRefused) or refusal.reason not in contract()[1]["refusal_status"]:
            raise HostedGrantUnavailable("Authority recovery refusal is unavailable")
        if refusal.reason in {"not_ready", "rate_limited", "temporarily_unavailable"}:
            return
        connection.execute("UPDATE installation_authority_recoveries SET state='refused',source_reference=NULL,refusal_reason=? WHERE recovery_id=?",
            (refusal.reason, row["recovery_id"]))
        authorize(connection=connection)

    def browser_source(self, connection, request, *, authorize):
        """A protected ceremony may use only the exact still-live source."""
        row, _ = self.exact(connection, request, authorize=authorize)
        if (row["state"] != "prepared" or self.store._now() >= timestamp(row["source_expires_at"])
                or self.store._now().timestamp() >= row["created_at"] + 1800):
            raise HostedGrantUnavailable("Authority recovery browser source expired")
        return {"reference": row["source_reference"], "expires_at": row["source_expires_at"]}

    @capture_authority_change
    def adopt_delivery(self, request, attempt, result, *, verifier, authorize):
        """Admit only the verified original complete bundle in one owner transaction.

        No old session or approval is recreated. The existing local account link
        follows its exact replacement grant references, using the same withdrawal
        and session invalidation primitive as ordinary refresh.
        """
        with self.store.database.transaction() as connection:
            row, evidence = self.guard(connection, request, "deliver", attempt, authorize=authorize)
            now = self.store._now()
            if row["committed_at"] is not None and row["committed_at"] != result.get("committed_at"):
                raise HostedGrantUnavailable("Authority recovery commitment changed")
            lost = request["scope"]["evidence"]["kind"] == "lost-enrollment"
            grants = (verify_lost_enrollment_bundle if lost else verify_delivery_bundle)(
                verifier, request, result, evidence=evidence, now=now)
            prior = {} if lost else {grant.grant_type: grant for grant in evidence.grants}
            if timestamp(result["committed_at"]).timestamp() < row["created_at"] - 60:
                raise HostedGrantUnavailable("Authority recovery commitment predates admission")
            for grant in grants:
                self.store._validate_verified_grant(grant)
                if (self.store._grant_is_withdrawn(connection, grant)
                        or connection.execute("SELECT 1 FROM verified_grant_references WHERE reference_id=? OR grant_identifier=?",
                            (grant.reference_id, grant.grant_identifier)).fetchone()):
                    raise HostedGrantUnavailable("Authority recovery grant is not a fresh exact reference")
            self.guard(connection, request, "deliver", attempt, authorize=authorize)
            for grant in grants:
                self.store._insert_verified_grant(connection, grant)
                if not lost:
                    previous = prior[grant.grant_type]
                    connection.execute("UPDATE authorized_account_binding_grants SET grant_reference_id=? WHERE grant_reference_id=?",
                        (grant.reference_id, previous.reference_id))
                    self.store._revoke_in_transaction(connection,
                        RevocationKey(RevocationScopeType.VERIFIED_GRANT, previous.reference_id), reason="replaced")
            if lost:
                self._complete_lost_claim(connection, evidence, now)
            receipt = canonical_request({grant.grant_type: {"reference_id": grant.reference_id,
                "jti": grant.grant_identifier, "digest": grant.grant_digest} for grant in grants}).decode()
            connection.execute("UPDATE installation_authority_recoveries SET state='admitted',source_reference=NULL,"
                "committed_at=?,admitted_grants_json=? WHERE recovery_id=?", (result["committed_at"], receipt, row["recovery_id"]))
            self.store._increment_authorization_epoch(connection)
            now = self.store._now()
            if any(not grant.valid_from <= now < grant.expires_at - timedelta(
                    seconds=grant_offline_grace_seconds(grant.grant_type)) for grant in grants):
                raise HostedGrantUnavailable("Authority recovery delivery expired before admission")
            # The caller's admitted operation/installation guard is distinct
            # from the now-replaced previous-grant evidence.
            authorize(connection=connection)
            return tuple(grant.reference_id for grant in grants)

    def _complete_lost_claim(self, connection, evidence, now):
        """Admit the original enrollment, never a creator binding or new claim."""
        updated = connection.execute("UPDATE provisioning_claim_submissions SET state='consumed',outcome=NULL,"
            "resolved_at=?,enrolled_at=? WHERE claim_id=? AND state='submitted' AND enrolled_at IS NULL",
            (_time_text(now), _time_text(evidence.enrolled_at), evidence.claim_id))
        if updated.rowcount != 1:
            raise HostedGrantUnavailable("Original enrollment claim changed")
        self.store._enqueue_onboarding_progress_in_transaction(connection, "enrolled",
            event_id=_new_uuid7(now), correlation_id=_new_uuid7(now), occurred_at=now)
        if not connection.execute("SELECT 1 FROM provisioning_candidates WHERE association_request_id=?",
                (evidence.association_request_id,)).fetchone():
            connection.execute("INSERT INTO provisioning_candidates (association_request_id,installation_id,"
                "onboarding_transaction_id,organization_id,creator_account_id,state,requested_at,resolved_at) "
                "VALUES (?,?,?,?,?,'pending',?,NULL)", (evidence.association_request_id,
                evidence.scope["installation_id"], evidence.scope["enrollment_transaction_id"],
                evidence.scope["organization_id"], evidence.intended_creator_id, _time_text(now)))
