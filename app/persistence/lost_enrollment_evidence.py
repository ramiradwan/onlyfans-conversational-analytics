"""Nonauthorizing original receipt evidence for proved lost enrollment.

Only the initial owner's exact proof-read receipt with retained authorization
scope may establish this evidence. Provider independently resolves the actor.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json

from app.persistence.auth import ClaimSubmission, RevocationKey, RevocationScopeType
from app.security.hosted_grants import CLAIM_PROFILE_V2, HostedGrantUnavailable, require_hosted_origin
from app.security.initial_handoff import timestamp, validate_receipt, validate_scope
from app.security.installation_continuation import canonical_request

BOOTSTRAP_WINDOW = timedelta(days=37)
CLOSED_REFUSALS = frozenset({"bootstrap_authentication_required", "bootstrap_authority_withdrawn",
    "installation_binding_mismatch"})


@dataclass(frozen=True)
class LostEnrollment:
    scope: dict
    claim_id: str
    enrolled_at: datetime
    key: object
    operation_id: str
    intended_creator_id: str
    association_request_id: str


class LostEnrollmentEvidence:
    def __init__(self, store, origin):
        self.store = store
        self.origin = require_hosted_origin(origin)

    def _key(self, connection, receipt):
        key = self.store.installation_key_reference(connection=connection)
        if (key is None or (key.installation_key_id, key.installation_key_jkt) !=
                (receipt["installation_key_id"], receipt["installation_key_jkt"])):
            raise HostedGrantUnavailable("Original enrollment key changed")
        if self.store._scope_is_revoked(connection, RevocationKey(
                RevocationScopeType.INSTALLATION, receipt["installation_id"])):
            raise HostedGrantUnavailable("Original installation was revoked")
        return key

    def _claim(self, connection, receipt, *, missing=False):
        rows = connection.execute("SELECT * FROM provisioning_claim_submissions LIMIT 2").fetchall()
        if not rows and missing:
            return None
        if len(rows)!=1:
            raise HostedGrantUnavailable("Original enrollment claim is ambiguous")
        claim=rows[0]
        if (claim["state"]=="refused" or claim["claim_profile"]!=CLAIM_PROFILE_V2
                or any(claim[name]!=receipt[name] for name in
                    ("claim_id","installation_id","organization_id","onboarding_transaction_id"))
                or claim["enrolled_at"] is not None and
                    datetime.fromisoformat(claim["enrolled_at"])!=timestamp(receipt["consumed_at"])):
            raise HostedGrantUnavailable("Original enrollment claim changed")
        return claim

    def retain(self, connection, receipt, *, scope, operation_id, authorize):
        """Persist the exact proof-read receipt and the claim in one transaction."""
        authorize(connection=connection)
        validate_receipt(receipt)
        validate_scope(scope)
        self._key(connection, receipt)
        if (receipt["operation_id"]!=operation_id
                or any(scope[name]!=receipt[name] for name in ("installation_id","organization_id",
                    "onboarding_transaction_id","installation_key_jkt"))
                or timestamp(receipt["consumed_at"])>self.store._now()):
            raise HostedGrantUnavailable("Original enrollment receipt changed")
        claim=self._claim(connection,receipt,missing=True)
        raw=canonical_request(receipt).decode()
        digest=hashlib.sha256(canonical_request(scope)).hexdigest()
        existing=connection.execute("SELECT * FROM lost_enrollment_evidence").fetchall()
        identity=(receipt["claim_id"],receipt["installation_id"],operation_id,self.origin,raw,digest,
            scope["intended_creator_id"],scope["association_request_id"])
        if existing:
            if len(existing)!=1 or tuple(existing[0][name] for name in ("claim_id","installation_id",
                    "operation_id","provider_origin","receipt_json","scope_digest","intended_creator_id",
                    "association_request_id"))!=identity:
                raise HostedGrantUnavailable("Original enrollment receipt conflicts with retained evidence")
        else:
            if claim is None:
                self.store._record_claim_submission_in_transaction(connection, ClaimSubmission(
                    receipt["claim_id"],receipt["onboarding_transaction_id"],receipt["organization_id"],
                    receipt["installation_id"],self.store._now(),claim_profile=CLAIM_PROFILE_V2))
            connection.execute("INSERT INTO lost_enrollment_evidence VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                (*identity,self.store._now().timestamp()))
        authorize(connection=connection)

    def exact_receipt(self, connection, *, operation_id=None):
        rows=connection.execute("SELECT * FROM lost_enrollment_evidence LIMIT 2").fetchall()
        if len(rows)!=1:
            raise HostedGrantUnavailable("Original consumed enrollment receipt unavailable")
        row=rows[0]
        receipt=json.loads(row["receipt_json"])
        validate_receipt(receipt)
        if (row["provider_origin"]!=self.origin or row["operation_id"]!=receipt["operation_id"]
                or row["installation_id"]!=receipt["installation_id"] or row["claim_id"]!=receipt["claim_id"]
                or operation_id is not None and operation_id!=row["operation_id"]
                or canonical_request(receipt).decode()!=row["receipt_json"]):
            raise HostedGrantUnavailable("Original enrollment receipt changed")
        key=self._key(connection,receipt)
        claim=self._claim(connection,receipt)
        return row,receipt,key,claim

    def require_bootstrap(self, connection, *, operation_id, authorize):
        authorize(connection=connection)
        row,receipt,_,_=self.exact_receipt(connection,operation_id=operation_id)
        if (row["bootstrap_refusal"] is not None
                or self.store._now()>=timestamp(receipt["consumed_at"])+BOOTSTRAP_WINDOW):
            raise HostedGrantUnavailable("Original enrollment requires authenticated recovery")
        return receipt

    def record_refusal(self, connection, *, operation_id, reason, authorize):
        """Only a closed proof-bound v3 bootstrap refusal is retained."""
        authorize(connection=connection)
        if reason not in CLOSED_REFUSALS:
            return
        row,_,_,_=self.exact_receipt(connection,operation_id=operation_id)
        if row["bootstrap_refusal"] not in (None,reason):
            raise HostedGrantUnavailable("Original bootstrap refusal changed")
        connection.execute("UPDATE lost_enrollment_evidence SET bootstrap_refusal=? WHERE claim_id=?",
            (reason,row["claim_id"]))
        authorize(connection=connection)

    def eligible(self, connection):
        """Candidate only; never infer loss from the absence of grant references."""
        row,receipt,key,claim=self.exact_receipt(connection)
        enrolled=timestamp(receipt["consumed_at"])
        if (claim["state"]!="submitted" or claim["enrolled_at"] is not None
                or self.store._now()<enrolled+BOOTSTRAP_WINDOW
                or row["bootstrap_refusal"] not in (None,"bootstrap_authentication_required")):
            raise HostedGrantUnavailable("Lost enrollment recovery is not eligible")
        # Any prior local authority, even revoked, rules out this branch.
        for table in ("verified_grant_references","hosted_grant_tombstones",
                "authorized_account_bindings","webauthn_credentials","agent_pairings",
                "bridge_sessions","runtime_tickets"):
            if connection.execute("SELECT 1 FROM "+table+" LIMIT 1").fetchone():
                raise HostedGrantUnavailable("Prior local authority cannot be discarded")
        if connection.execute("SELECT 1 FROM auth_revocation_state WHERE revoked_at IS NOT NULL LIMIT 1").fetchone():
            raise HostedGrantUnavailable("Prior authority was withdrawn")
        candidates=connection.execute("SELECT * FROM provisioning_candidates LIMIT 2").fetchall()
        if candidates and (len(candidates)!=1 or candidates[0]["state"]!="pending"
                or candidates[0]["association_request_id"]!=row["association_request_id"]
                or candidates[0]["creator_account_id"]!=row["intended_creator_id"]
                or any(candidates[0][name]!=receipt[name] for name in
                    ("installation_id","organization_id","onboarding_transaction_id"))):
            raise HostedGrantUnavailable("Original creator selection changed")
        scope={"organization_id":receipt["organization_id"],"installation_id":receipt["installation_id"],
            "installation_key_id":key.installation_key_id,"installation_key_jkt":key.installation_key_jkt,
            "enrollment_transaction_id":receipt["onboarding_transaction_id"],
            "evidence":{"kind":"lost-enrollment","claim_id":receipt["claim_id"],"creator_scope":{"kind":"none"}}}
        return LostEnrollment(scope,receipt["claim_id"],enrolled,key,row["operation_id"],
            row["intended_creator_id"],row["association_request_id"])
