"""Proof-bound, non-creating read of one original complimentary activation."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from functools import lru_cache

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from app.core.resource_paths import resource_path
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import _require_proof_budget, timestamp
from app.security.installation_continuation import (
    PROFILE as SOURCE_PROFILE, _decode, _document, canonical_request, signing_reference, _P256_ORDER,
)
from app.security.installation_key import verify_installation_proof
from contracts.loader import verify_snapshot_integrity

NAME = "complimentary-activation-reconciliation"
PROFILE = "urn:bridge-clean:" + NAME + ":v1"
PURPOSE = "complimentary-activation-reconcile"
LIMIT = 8192
RESPONSE_LIMIT = 16384


@lru_cache(maxsize=1)
def _contract():
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
        _contract()[0][schema].validate(value)
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise HostedGrantUnavailable("Invalid activation reconciliation document") from None
    return value


def exact_request(prepare, *, continuation_id, activation_operation_id):
    if not isinstance(prepare, dict) or set(prepare) != {"profile", "operation_id", "scope"} or prepare["profile"] != SOURCE_PROFILE:
        raise HostedGrantUnavailable("Invalid original activation source")
    request = {"profile": PROFILE, "source_operation_id": prepare["operation_id"],
        "source_continuation_id": continuation_id, "source_prepare_digest": hashlib.sha256(canonical_request(prepare)).hexdigest(),
        "scope": prepare["scope"], "activation_operation_id": activation_operation_id}
    request = _document(canonical_request(request), limit=LIMIT)
    challenge_target(request)
    return request


def challenge_target(request):
    proof = _contract()[2]
    target = validate("proof-challenge-request", {"profile": proof["profile"],
        "target": {"operation": PURPOSE, "request": request}})
    original = {"profile": SOURCE_PROFILE, "operation_id": request["source_operation_id"], "scope": request["scope"]}
    if request["source_prepare_digest"] != hashlib.sha256(canonical_request(original)).hexdigest():
        raise HostedGrantUnavailable("Original activation source changed")
    return target


def validate_result(request, result):
    challenge_target(request)
    validate("read-response", result)
    if any(request[name] != result[name] for name in ("source_operation_id", "activation_operation_id")):
        raise HostedGrantUnavailable("Original activation result changed")
    return result


class ActivationReconciliationRefused(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Activation reconciliation refused")


class ActivationReconciliationClient:
    """No activation, offer, session or grant mutation is available here."""
    def __init__(self, transport, key, *, monotonic=time.monotonic):
        self.transport, self.key, self.monotonic = transport, key, monotonic
        _, self.profile, self.proof = _contract()
        self.route = self.proof["purposes"][PURPOSE]
        self.challenge_path = f"/v1/onboarding/{NAME}-proof-challenges"

    def _request(self, path, value, schema, guard):
        if len(canonical_request(value)) > LIMIT:
            raise HostedGrantUnavailable("Activation reconciliation request exceeds limit")
        guard()
        try:
            response = self.transport.request("POST", path, json_body=value)
        except Exception:
            raise HostedGrantUnavailable("Activation reconciliation unavailable") from None
        if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise HostedGrantUnavailable("Invalid activation reconciliation response type")
        result = _document(response.body, limit=RESPONSE_LIMIT)
        if response.status_code != 200:
            validate("error", result)
            reason = result["reason"]
            if response.status_code != self.profile["refusal_status"][reason] or reason in {"rate_limited", "temporarily_unavailable"}:
                raise HostedGrantUnavailable("Activation reconciliation unavailable")
            raise ActivationReconciliationRefused(reason)
        return validate(schema, result)

    def read(self, request, *, before_send):
        request = _document(canonical_request(request), limit=LIMIT)
        target = challenge_target(request)
        before_send()
        key = signing_reference(self.key)
        if key.installation_key_id != request["scope"]["installation_key_id"]:
            raise HostedGrantUnavailable("Original activation key changed")
        start = self.monotonic()
        challenge = self._request(self.challenge_path, target, "proof-challenge-response", before_send)
        _require_proof_budget(start, self.monotonic())
        timestamp(challenge["expires_at"])
        digest = hashlib.sha256(canonical_request(request)).digest()
        if challenge["request_digest"] != digest.hex():
            raise HostedGrantUnavailable("Activation reconciliation challenge changed")
        fields = (_decode(challenge["challenge"], 32), self.route["method"].encode(), self.route["path"].encode(),
            digest, PURPOSE.encode(), _decode(key.installation_key_jkt, 32), self.proof["audience"].encode())
        message = self.proof["domain"].encode() + b"".join(len(field).to_bytes(4, "big") + field for field in fields)
        before_send()
        signature = self.key.sign_challenge(message)
        _require_proof_budget(start, self.monotonic())
        if (signature.installation_key_id != key.installation_key_id or signature.algorithm != "ES256"
                or len(signature.signature) != 64 or not 0 < int.from_bytes(signature.signature[:32], "big") < _P256_ORDER
                or not 0 < int.from_bytes(signature.signature[32:], "big") <= _P256_ORDER // 2
                or not verify_installation_proof(key.public_key_jwk, message, signature)):
            raise HostedGrantUnavailable("Activation reconciliation proof unavailable")
        envelope = validate("read-request", {"request": request, "proof": {"challenge": challenge["challenge"],
            "signature": base64.urlsafe_b64encode(signature.signature).rstrip(b"=").decode()}})
        result = self._request(self.route["path"], envelope, "read-response", before_send)
        return validate_result(request, result)
