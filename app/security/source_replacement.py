"""Proof-bound replacement of an exact retained onboarding source.

This transport does not decide eligibility, persist an operation, or retry a
mutation. Its callers must retain the predecessor and chosen successor before
sending; a prepared result is not account approval or installation authority.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timezone, timedelta
from functools import lru_cache

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from app.core.resource_paths import resource_path
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import _require_proof_budget, timestamp
from app.security.installation_continuation import (
    _decode, _document, canonical_request, signing_reference, _P256_ORDER,
)
from app.security.installation_key import verify_installation_proof
from contracts.loader import verify_snapshot_integrity

INITIAL = "initial-handoff"
REGISTERED = "registered-continuation"
LIMIT = 8192
RESPONSE_LIMIT = 16384


class SourceReplacementRefused(ValueError):
    """A closed refusal, not proof that the original operation rolled back."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Onboarding source replacement was refused")


class _Contract:
    def __init__(self, kind):
        root = resource_path("contracts")
        verify_snapshot_integrity(root)
        documents = [json.loads(path.read_text("utf-8")) for path in (root / "schemas").rglob("*.schema.json")]
        registry = Registry().with_resources((item["$id"], Resource.from_contents(item)) for item in documents)
        self.validators = {
            item["$id"].rsplit("/", 1)[-1].removesuffix(".schema.json"): Draft202012Validator(item, registry=registry)
            for item in documents if f"/schemas/{kind}-replacement/v1/" in item["$id"]
        }
        self.profile = json.loads((root / f"profiles/{kind}-replacement/v1/profile.json").read_text("utf-8"))
        self.proof = json.loads((root / f"profiles/{kind}-replacement-proof/v1/profile.json").read_text("utf-8"))

    def validate(self, name, value):
        try:
            self.validators[name].validate(value)
        except (ValidationError, TypeError, ValueError, RecursionError):
            raise HostedGrantUnavailable("Invalid source replacement document") from None
        return value


@lru_cache(maxsize=2)
def _contract(kind):
    if kind not in (INITIAL, REGISTERED):
        raise ValueError("Unsupported source replacement")
    return _Contract(kind)


def _digest(value):
    return hashlib.sha256(canonical_request(value)).hexdigest()


def original_prepare(kind, request, *, successor=False):
    """Reconstruct only the published v2 prepare bytes, never a new scope."""
    contract = _contract(kind)
    field = "destination" if kind == INITIAL else "scope"
    return {"profile": contract.profile["predecessor_profile"],
            "operation_id": request["successor_operation_id" if successor else "predecessor_operation_id"],
            field: request[field]}


def replacement_request(kind, predecessor, successor_operation_id, *, continuation_id=None):
    """Build the request exclusively from the retained ordinary prepare."""
    contract = _contract(kind)
    field = "destination" if kind == INITIAL else "scope"
    if not isinstance(predecessor, dict) or set(predecessor) != {"profile", "operation_id", field}:
        raise HostedGrantUnavailable("Invalid retained source prepare")
    if predecessor["profile"] != contract.profile["predecessor_profile"]:
        raise HostedGrantUnavailable("Unsupported retained source prepare")
    value = {"profile": contract.profile["profile"],
             "predecessor_operation_id": predecessor["operation_id"],
             "predecessor_prepare_digest": _digest(predecessor),
             field: predecessor[field], "successor_operation_id": successor_operation_id}
    if kind == REGISTERED:
        value["predecessor_continuation_id"] = continuation_id
    elif continuation_id is not None:
        raise HostedGrantUnavailable("Initial source has no continuation")
    value = _document(canonical_request(value), limit=LIMIT)
    _validate_request(kind, value)
    return value


def _validate_request(kind, request):
    contract = _contract(kind)
    target = {"profile": contract.proof["profile"],
              "target": {"operation": kind + "-replace", "request": request}}
    contract.validate("proof-challenge-request", target)
    if (request["predecessor_operation_id"] == request["successor_operation_id"]
            or request["predecessor_prepare_digest"] != _digest(original_prepare(kind, request))):
        raise HostedGrantUnavailable("Source replacement predecessor changed")
    return target


class SourceReplacementClient:
    """Closed initial or registered source transport using the existing key."""
    def __init__(self, kind, transport, key, *, clock=lambda: datetime.now(timezone.utc),
                 monotonic=time.monotonic):
        self.kind, self.transport, self.key = kind, transport, key
        self.clock, self.monotonic = clock, monotonic
        self.contract = _contract(kind)
        self.purpose = kind + "-replace"
        self.challenge_path = f"/v1/onboarding/{kind}-replacement-proof-challenges"
        self.route = self.contract.proof["purposes"][self.purpose]

    def _request(self, path, body, schema, *, before_send):
        if len(canonical_request(body)) > LIMIT:
            raise HostedGrantUnavailable("Source replacement request exceeds its limit")
        before_send()
        try:
            response = self.transport.request("POST", path, json_body=body)
        except Exception:
            raise HostedGrantUnavailable("Source replacement outcome is unknown") from None
        if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise HostedGrantUnavailable("Invalid source replacement response type")
        value = _document(response.body, limit=RESPONSE_LIMIT)
        if response.status_code != 200:
            self.contract.validate("error", value)
            reason = value["reason"]
            if (response.status_code != self.contract.profile["refusal_status"][reason]
                    or reason in {"rate_limited", "temporarily_unavailable"}):
                raise HostedGrantUnavailable("Source replacement outcome is unknown")
            raise SourceReplacementRefused(reason)
        return self.contract.validate(schema, value)

    def envelope(self, request, *, before_send):
        request = _document(canonical_request(request), limit=LIMIT)
        target = _validate_request(self.kind, request)
        before_send()
        # Replacement must never create a missing key, even for initial enrollment.
        key = signing_reference(self.key)
        if self.kind == INITIAL:
            expected = request["destination"]["installation_key"]
            public = json.loads(key.public_key_jwk)
            if (expected["kid"] != key.installation_key_id
                    or expected["jwk"] != {name: public[name] for name in ("crv", "kty", "x", "y")}):
                raise HostedGrantUnavailable("Initial source replacement key changed")
        elif request["scope"]["installation_key_id"] != key.installation_key_id:
            raise HostedGrantUnavailable("Registered source replacement key changed")
        started = self.monotonic()
        challenge = self._request(self.challenge_path, target, "proof-challenge-response", before_send=before_send)
        _require_proof_budget(started, self.monotonic())
        timestamp(challenge["expires_at"])
        digest = hashlib.sha256(canonical_request(request)).digest()
        if challenge["request_digest"] != digest.hex():
            raise HostedGrantUnavailable("Source replacement challenge changed")
        fields = (_decode(challenge["challenge"], 32), self.route["method"].encode(), self.route["path"].encode(),
                  digest, self.purpose.encode(), _decode(key.installation_key_jkt, 32),
                  self.contract.proof["audience"].encode())
        message = self.contract.proof["domain"].encode() + b"".join(len(field).to_bytes(4, "big") + field for field in fields)
        before_send()
        proof = self.key.sign_challenge(message)
        _require_proof_budget(started, self.monotonic())
        if (proof.installation_key_id != key.installation_key_id or proof.algorithm != "ES256"
                or len(proof.signature) != 64
                or not 0 < int.from_bytes(proof.signature[:32], "big") < _P256_ORDER
                or not 0 < int.from_bytes(proof.signature[32:], "big") <= _P256_ORDER // 2
                or not verify_installation_proof(key.public_key_jwk, message, proof)):
            raise HostedGrantUnavailable("Source replacement key proof is unavailable")
        wire_proof = {"challenge": challenge["challenge"],
                      "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode()}
        if self.kind == INITIAL:
            wire_proof["key_id"] = key.installation_key_id
        return self.contract.validate("replace-request", {"request": request, "proof": wire_proof})

    def validate_result(self, request, result, *, intended_creator_id=None):
        _validate_request(self.kind, request)
        self.contract.validate("replace-response", result)
        if result["predecessor_operation_id"] != request["predecessor_operation_id"]:
            raise HostedGrantUnavailable("Source replacement result changed")
        if result["status"] != "prepared":
            # Accepted/committed IDs name original domain work, not the reserved
            # successor. The owner must reconcile them, not adopt a new source.
            return result
        successor = result["successor"]
        if (successor["operation_id"] != request["successor_operation_id"]
                or result["successor_prepare_digest"] != _digest(original_prepare(self.kind, request, successor=True))):
            raise HostedGrantUnavailable("Source replacement successor changed")
        if self.kind == INITIAL:
            if intended_creator_id is None or result["intended_creator_id"] != intended_creator_id:
                raise HostedGrantUnavailable("Initial source replacement creator changed")
        elif successor["continuation_id"] == request["predecessor_continuation_id"]:
            raise HostedGrantUnavailable("Registered source replacement reused predecessor")
        _decode(successor["reference"], 32)
        # The ordinary v2 lifetime starts at creation, not at delivery. An
        # exact retry can return a shorter remaining lifetime, never a longer one.
        expiry = timestamp(successor["expires_at"])
        now = self.clock()
        ttl = timedelta(seconds=600 if self.kind == INITIAL else 1800)
        if not now < expiry <= now + ttl:
            raise HostedGrantUnavailable("Source replacement successor deadline is invalid")
        return result

    def replace(self, request, *, before_send, intended_creator_id=None):
        """One deliberate send; caller owns durable identity and recovery."""
        if self.kind == INITIAL and not intended_creator_id:
            raise HostedGrantUnavailable("Initial source replacement creator is unavailable")
        request = _document(canonical_request(request), limit=LIMIT)
        envelope = self.envelope(request, before_send=before_send)
        result = self._request(self.route["path"], envelope, "replace-response", before_send=before_send)
        return self.validate_result(request, result, intended_creator_id=intended_creator_id)
