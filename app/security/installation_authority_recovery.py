"""Exact protected installation recovery transport; never admits returned grants.

Preparation reserves an operation, not authority. Delivery only retrieves the
original committed result. Neither operation can create a missing local key.
The owning recovery journal must guard every effect and independently verify
all signed grants before adopting them.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from app.core.resource_paths import resource_path
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import _require_proof_budget, timestamp
from app.security.installation_continuation import (
    _P256_ORDER, _decode, _document, canonical_request, signing_reference,
)
from app.security.installation_key import verify_installation_proof
from contracts.loader import verify_snapshot_integrity

NAME = "installation-authority-recovery"
PROFILE = "urn:bridge-clean:" + NAME + ":v1"
REQUEST_LIMIT = 8192
RESPONSE_LIMIT = 65536
WITHDRAWALS = frozenset({"installation_revoked", "membership_removed", "approval_revoked", "grant_withdrawn"})


@lru_cache(maxsize=1)
def contract():
    root = resource_path("contracts")
    verify_snapshot_integrity(root)
    documents = [json.loads(path.read_text("utf-8")) for path in (root / "schemas").rglob("*.schema.json")]
    registry = Registry().with_resources((doc["$id"], Resource.from_contents(doc)) for doc in documents)
    validators = {doc["$id"].rsplit("/", 1)[-1].removesuffix(".schema.json"):
        Draft202012Validator(doc, registry=registry) for doc in documents if f"/schemas/{NAME}/v1/" in doc["$id"]}
    return (validators, json.loads((root / f"profiles/{NAME}/v1/profile.json").read_text("utf-8")),
        json.loads((root / f"profiles/{NAME}-proof/v1/profile.json").read_text("utf-8")))


def validate(schema, value):
    try:
        contract()[0][schema].validate(value)
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise HostedGrantUnavailable("Invalid authority recovery document") from None
    return value


def scope_digest(scope):
    return hashlib.sha256(canonical_request(scope)).hexdigest()


def exact_request(recovery_id, scope):
    request = _document(canonical_request({"profile": PROFILE, "recovery_id": recovery_id, "scope": scope}), limit=REQUEST_LIMIT)
    challenge_target("prepare", request)
    return request


def challenge_target(verb, request):
    if verb not in {"prepare", "deliver"}:
        raise ValueError("Unsupported authority recovery operation")
    target = validate("proof-challenge-request", {"profile": contract()[2]["profile"],
        "target": {"operation": "authority-recovery-" + verb, "request": request}})
    scope = request["scope"]
    _decode(scope["installation_key_jkt"], 32)
    evidence = scope["evidence"]
    if evidence["kind"] == "previous-grants":
        ids = [evidence["installation_jti"], evidence["membership_jti"]]
        if evidence["creator_scope"]["kind"] == "approved":
            ids.append(evidence["creator_scope"]["binding_jti"])
        if len(ids) != len(set(ids)):
            raise HostedGrantUnavailable("Authority recovery evidence is ambiguous")
    return target


def validate_result(verb, request, result, *, now):
    challenge_target(verb, request)
    validate(verb + "-response", result)
    if result["recovery_id"] != request["recovery_id"] or result["scope_digest"] != scope_digest(request["scope"]):
        raise HostedGrantUnavailable("Authority recovery result changed")
    if result["status"] == "prepared":
        _decode(result["source_reference"], 32)
        if not now < timestamp(result["expires_at"]) <= now + timedelta(seconds=1800):
            raise HostedGrantUnavailable("Authority recovery deadline is invalid")
        return result
    committed = timestamp(result["committed_at"])
    if committed > now + timedelta(seconds=60):
        raise HostedGrantUnavailable("Authority recovery commitment time is invalid")
    if verb == "deliver":
        scope = request["scope"]
        if any(result[name] != scope[name] for name in ("organization_id", "installation_id",
                "installation_key_id", "installation_key_jkt", "enrollment_transaction_id")):
            raise HostedGrantUnavailable("Authority recovery delivery scope changed")
        expected = {name: value for name, value in scope["evidence"]["creator_scope"].items() if name != "binding_jti"}
        if result["creator_scope"] != expected or timestamp(result["enrolled_at"]) > committed:
            raise HostedGrantUnavailable("Authority recovery delivery evidence changed")
    return result


class AuthorityRecoveryRefused(ValueError):
    """Closed response; only a named withdrawal is withdrawal evidence."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Installation authority recovery refused")

    @property
    def withdrawal(self):
        return self.reason in WITHDRAWALS


class InstallationAuthorityRecoveryClient:
    def __init__(self, transport, key, *, clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        self.transport, self.key = transport, key
        self.clock, self.monotonic = clock, monotonic
        _, self.profile, self.proof = contract()

    def _request(self, path, value, schema, guard, *, challenge=False):
        if len(canonical_request(value)) > REQUEST_LIMIT:
            raise HostedGrantUnavailable("Authority recovery request exceeds limit")
        guard()
        try:
            response = self.transport.request("POST", path, json_body=value)
        except Exception:
            raise HostedGrantUnavailable("Authority recovery outcome is unknown") from None
        if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise HostedGrantUnavailable("Invalid authority recovery response type")
        result = _document(response.body, limit=RESPONSE_LIMIT)
        if response.status_code != 200:
            validate("error", result)
            reason = result["reason"]
            # Challenge disclosure is nonauthorizing. Never treat an error there
            # as an authenticated operation refusal or proof of withdrawal.
            if challenge or response.status_code != self.profile["refusal_status"][reason] or reason in {
                    "rate_limited", "temporarily_unavailable"}:
                raise HostedGrantUnavailable("Authority recovery outcome is unknown")
            raise AuthorityRecoveryRefused(reason)
        return validate(schema, result)

    def _exchange(self, verb, request, *, before_send):
        request = _document(canonical_request(request), limit=REQUEST_LIMIT)
        target = challenge_target(verb, request)
        scope = request["scope"]
        before_send()
        key = signing_reference(self.key)
        if (key.installation_key_id, key.installation_key_jkt) != (scope["installation_key_id"], scope["installation_key_jkt"]):
            raise HostedGrantUnavailable("Authority recovery key changed")
        purpose = "authority-recovery-" + verb
        route = self.proof["purposes"][purpose]
        path = route["path"].format(installation_id=scope["installation_id"])
        started = self.monotonic()
        challenge = self._request(f"/v1/installations/{scope['installation_id']}/authority-recovery-proof-challenges",
            target, "proof-challenge-response", before_send, challenge=True)
        _require_proof_budget(started, self.monotonic())
        timestamp(challenge["expires_at"])
        digest = hashlib.sha256(canonical_request(request)).digest()
        if challenge["request_digest"] != digest.hex():
            raise HostedGrantUnavailable("Authority recovery challenge changed")
        fields = (_decode(challenge["challenge"], 32), route["method"].encode(), path.encode(),
            digest, purpose.encode(), _decode(key.installation_key_jkt, 32), self.proof["audience"].encode())
        message = self.proof["domain"].encode() + b"".join(len(field).to_bytes(4, "big") + field for field in fields)
        before_send()
        signature = self.key.sign_challenge(message)
        _require_proof_budget(started, self.monotonic())
        if (signature.installation_key_id != key.installation_key_id or signature.algorithm != "ES256"
                or len(signature.signature) != 64 or not 0 < int.from_bytes(signature.signature[:32], "big") < _P256_ORDER
                or not 0 < int.from_bytes(signature.signature[32:], "big") <= _P256_ORDER // 2
                or not verify_installation_proof(key.public_key_jwk, message, signature)):
            raise HostedGrantUnavailable("Authority recovery proof unavailable")
        envelope = validate(verb + "-request", {"request": request, "proof": {"challenge": challenge["challenge"],
            "key_id": key.installation_key_id, "signature": base64.urlsafe_b64encode(signature.signature).rstrip(b"=").decode()}})
        def guard():
            before_send()
            _require_proof_budget(started, self.monotonic())
        result = self._request(path, envelope, verb + "-response", guard)
        return validate_result(verb, request, result, now=self.clock())

    def prepare(self, request, *, before_send):
        return self._exchange("prepare", request, before_send=before_send)

    def deliver(self, request, *, before_send):
        return self._exchange("deliver", request, before_send=before_send)

def verify_delivery_bundle(verifier, request, result, *, evidence, now):
    """Verify the whole signed bundle against retained, independent actor evidence.

    The ordinary bootstrap helper derives its actor from the returned token.
    That is not sufficient for protected recovery: a different signed actor,
    grant scope, previous JTI or an expired delivery must never be accepted.
    """
    from app.security.hosted_grants import _untrusted_payload, _required_int, GrantVerificationRefused
    result = validate_result("deliver", request, result, now=now)
    if request["scope"] != evidence.scope or timestamp(result["enrolled_at"]) != evidence.enrolled_at:
        raise HostedGrantUnavailable("Original authority recovery enrollment changed")
    prior = {grant.grant_type: grant for grant in evidence.grants}
    membership = evidence.membership
    if set(result["grants"]) != set(prior):
        raise HostedGrantUnavailable("Authority recovery grant set changed")
    expected_accounts = (() if result["creator_scope"]["kind"] == "none"
        else (result["creator_scope"]["creator_account_id"],))
    references = []
    seen = {grant.grant_identifier for grant in evidence.grants}
    committed_at = int(timestamp(result["committed_at"]).timestamp())
    try:
        for kind, token in result["grants"].items():
            payload = _untrusted_payload(token)
            reference = verifier._verified_reference(token, payload, grant_type=kind,
                organization_id=evidence.scope["organization_id"], installation_id=evidence.scope["installation_id"],
                key=evidence.key, external_issuer=membership.issuer, external_subject=membership.subject,
                requested_account_ids=())
            if (reference.grant_identifier in seen or _required_int(payload, "iat") != committed_at
                    or not _required_int(payload, "nbf") <= int(now.timestamp()) < _required_int(payload, "exp")
                    or committed_at > int(now.timestamp())):
                raise HostedGrantUnavailable("Authority recovery grant commitment changed")
            seen.add(reference.grant_identifier)
            if kind == "membership_snapshot" and (reference.membership_id != membership.membership_id
                    or reference.allowed_creator_account_ids != expected_accounts):
                raise HostedGrantUnavailable("Authority recovery membership changed")
            if kind == "creator_account_binding" and (reference.creator_account_id, reference.approval_id,
                    reference.approval_revision) != (prior[kind].creator_account_id, prior[kind].approval_id,
                    prior[kind].approval_revision):
                raise HostedGrantUnavailable("Authority recovery approval changed")
            references.append(reference)
    except GrantVerificationRefused:
        raise HostedGrantUnavailable("Authority recovery signed grant could not be verified") from None
    return tuple(references)

def verify_lost_enrollment_bundle(verifier, request, result, *, evidence, now):
    """Verify the original authenticated result when no prior actor/JTIs exist.

    The consumed receipt binds the enrollment, not the human actor. The
    release-selected Provider resolves and freshly authenticates that actor
    against its original enrollment evidence. Only the signed membership in
    its exact proof-bound committed delivery can establish the local actor.
    This branch cannot be used to replace any previously known authority.
    """
    from app.security.hosted_grants import _untrusted_payload, _required_int, _required_string, GrantVerificationRefused
    result = validate_result("deliver", request, result, now=now)
    if (request["scope"] != evidence.scope
            or evidence.scope["evidence"] != {"kind": "lost-enrollment", "claim_id": evidence.claim_id,
                "creator_scope": {"kind": "none"}}
            or timestamp(result["enrolled_at"]) != evidence.enrolled_at
            or result["creator_scope"] != {"kind": "none"}
            or set(result["grants"]) != {"installation_grant", "membership_snapshot"}):
        raise HostedGrantUnavailable("Lost enrollment delivery evidence changed")
    instant = int(now.timestamp())
    committed = int(timestamp(result["committed_at"]).timestamp())
    references = []
    seen = set()
    try:
        # Derive only the expected signed identity; the unverified payload
        # is never persisted or used as authority before signature checking.
        membership = _untrusted_payload(result["grants"]["membership_snapshot"])
        issuer = _required_string(membership, "ciam_issuer")
        subject = _required_string(membership, "ciam_subject")
        for kind in ("membership_snapshot", "installation_grant"):
            token = result["grants"][kind]
            payload = _untrusted_payload(token)
            reference = verifier._verified_reference(token, payload, grant_type=kind,
                organization_id=evidence.scope["organization_id"], installation_id=evidence.scope["installation_id"],
                key=evidence.key, external_issuer=issuer, external_subject=subject, requested_account_ids=())
            if (reference.grant_identifier in seen or _required_int(payload, "iat") != committed
                    or not _required_int(payload, "nbf") <= instant < _required_int(payload, "exp")
                    or committed > instant or reference.creator_account_id is not None):
                raise HostedGrantUnavailable("Lost enrollment grant commitment changed")
            if kind == "membership_snapshot" and (not reference.membership_id
                    or reference.allowed_creator_account_ids != ()):
                raise HostedGrantUnavailable("Lost enrollment cannot admit creator authority")
            seen.add(reference.grant_identifier)
            references.append(reference)
    except GrantVerificationRefused:
        raise HostedGrantUnavailable("Lost enrollment signed grant could not be verified") from None
    return tuple(references)
