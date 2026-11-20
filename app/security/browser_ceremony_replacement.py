"""Exact ordinary ceremony replacement transport. The durable owner controls admission."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from functools import lru_cache

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from app.core.resource_paths import resource_path
from app.security.browser_ceremony import BrowserCeremonyClient, BrowserCeremonyRefused, binding_digest
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import _require_proof_budget, timestamp
from app.security.installation_continuation import _decode, _document, canonical_request, signing_reference
from app.security.installation_key import verify_installation_proof
from contracts.loader import verify_snapshot_integrity

PROFILE = "urn:bridge-clean:browser-ceremony-replacement:v1"
PROOF_PROFILE = "urn:bridge-clean:browser-ceremony-replacement-proof:v1"
PURPOSE = "browser-ceremony-replace"
CHALLENGE_PATH = "/v1/onboarding/browser-ceremony-replacement-proof-challenges"
PATH = "/v1/onboarding/browser-ceremony-replacements:replace"
LIMIT = 8192
RESPONSE_LIMIT = 16384
ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
REFUSAL_STATUS = {
    "invalid": {400, 413}, "proof_invalid": {401}, "authorization_denied": {403},
    "operation_conflict": {409}, "not_ready": {409}, "expired": {410},
    "rate_limited": {429}, "temporarily_unavailable": {503},
}


class _Contract:
    def __init__(self):
        root = resource_path("contracts")
        verify_snapshot_integrity(root)
        schemas = [json.loads(path.read_text(encoding="utf-8")) for path in
                   (root / "schemas").rglob("*.schema.json")]
        registry = Registry().with_resources((schema["$id"], Resource.from_contents(schema)) for schema in schemas)
        self.validators = {
            schema["$id"].rsplit("/", 1)[-1].removesuffix(".schema.json"):
            Draft202012Validator(schema, registry=registry) for schema in schemas
            if "/schemas/browser-ceremony-replacement/v1/" in schema["$id"]
        }
        self.proof = json.loads((root / "profiles/browser-ceremony-replacement-proof/v1/profile.json").read_text("utf-8"))

    def validate(self, name, value):
        try:
            self.validators[name].validate(value)
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise HostedGrantUnavailable("Invalid browser ceremony replacement document") from None
        return value


@lru_cache(maxsize=1)
def _contract():
    return _Contract()


class BrowserCeremonyReplacementClient:
    """One send per explicit call; never decides that a predecessor is replaceable."""

    def __init__(self, ceremony: BrowserCeremonyClient, *, monotonic=time.monotonic,
                 clock=lambda: datetime.now(timezone.utc)):
        self.ceremony = ceremony
        self.transport, self.key = ceremony.transport, ceremony.key
        self.monotonic, self.clock = monotonic, clock
        self.contract = _contract()

    def _request(self, path, body, schema, *, before_send):
        if len(canonical_request(body)) > LIMIT:
            raise HostedGrantUnavailable("Browser ceremony replacement request exceeds its limit")
        if before_send is not None:
            before_send()
        try:
            response = self.transport.request("POST", path, json_body=body)
        except Exception:
            raise HostedGrantUnavailable("Browser ceremony replacement outcome is unknown") from None
        if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise HostedGrantUnavailable("Invalid browser ceremony replacement response type")
        value = _document(response.body, limit=RESPONSE_LIMIT)
        if response.status_code != 200:
            self.contract.validate("error", value)
            reason = value["reason"]
            if response.status_code not in REFUSAL_STATUS[reason] or reason in {
                "rate_limited", "temporarily_unavailable"
            }:
                raise HostedGrantUnavailable("Browser ceremony replacement outcome is unknown")
            raise BrowserCeremonyRefused(reason)
        return self.contract.validate(schema, value)

    def _validate_predecessor(self, request, predecessor):
        self.contract.validate("proof-challenge-request", {"profile": PROOF_PROFILE,
            "target": {"operation": PURPOSE, "request": request}})
        # The supplied predecessor is the exact durable ordinary ceremony binding.
        # Only the successor correlation identifier may differ.
        if (request["predecessor_binding_digest"] != binding_digest(predecessor)
                or request["predecessor_operation_id"] == request["successor_operation_id"]
                or request["successor_attempt_id"] == predecessor["attempt_id"]
                or timestamp(predecessor["source_expires_at"]) <= self.clock()):
            raise HostedGrantUnavailable("Browser ceremony replacement scope changed")

    def envelope(self, request, predecessor, *, before_send=None):
        request = _document(canonical_request(request), limit=LIMIT)
        predecessor = _document(canonical_request(predecessor), limit=LIMIT)
        self._validate_predecessor(request, predecessor)
        if before_send is not None:
            before_send()
        key = signing_reference(self.key)
        if (key.installation_key_id, key.installation_key_jkt) != (
                predecessor["installation_key_id"], predecessor["installation_key_jkt"]):
            raise HostedGrantUnavailable("Browser ceremony replacement key changed")
        started = self.monotonic()
        challenge = self._request(CHALLENGE_PATH, {"profile": PROOF_PROFILE,
            "target": {"operation": PURPOSE, "request": request}}, "proof-challenge-response",
            before_send=before_send)
        _require_proof_budget(started, self.monotonic())
        timestamp(challenge["expires_at"])
        digest = hashlib.sha256(canonical_request(request)).digest()
        if challenge["request_digest"] != digest.hex():
            raise HostedGrantUnavailable("Browser ceremony replacement challenge changed")
        route = self.contract.proof["purposes"][PURPOSE]
        fields = (_decode(challenge["challenge"], 32), route["method"].encode(), route["path"].encode(),
                  digest, PURPOSE.encode(), _decode(key.installation_key_jkt, 32),
                  self.contract.proof["audience"].encode())
        message = self.contract.proof["domain"].encode() + b"".join(
            len(field).to_bytes(4, "big") + field for field in fields)
        if before_send is not None:
            before_send()
        proof = self.key.sign_challenge(message)
        _require_proof_budget(started, self.monotonic())
        if (proof.installation_key_id != key.installation_key_id or proof.algorithm != "ES256"
                or len(proof.signature) != 64
                or not 0 < int.from_bytes(proof.signature[:32], "big") < ORDER
                or not 0 < int.from_bytes(proof.signature[32:], "big") <= ORDER // 2
                or not verify_installation_proof(key.public_key_jwk, message, proof)):
            raise HostedGrantUnavailable("Browser ceremony replacement key proof is unavailable")
        result = {"request": request, "proof": {"challenge": challenge["challenge"],
                  "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode()}}
        return self.contract.validate("replace-request", result)

    def validate_result(self, request, predecessor, result):
        self._validate_predecessor(request, predecessor)
        self.contract.validate("replace-response", result)
        binding = {**predecessor, "attempt_id": request["successor_attempt_id"]}
        if (result["predecessor_operation_id"] != request["predecessor_operation_id"]
                or result["successor_binding"] != binding
                or result["successor"]["ceremony_id"] == request["predecessor_ceremony_id"]):
            raise HostedGrantUnavailable("Browser ceremony replacement result changed")
        # Reuse the ordinary owner validation for binding hash, deadline,
        # completion destination and exact successor operation.
        self.ceremony.validate_result("prepare", {
            "profile": "urn:bridge-clean:browser-ceremony:v1",
            "operation_id": request["successor_operation_id"], "binding": binding,
        }, result["successor"])

    def replace(self, request, predecessor, *, before_send=None):
        request = _document(canonical_request(request), limit=LIMIT)
        predecessor = _document(canonical_request(predecessor), limit=LIMIT)
        envelope = self.envelope(request, predecessor, before_send=before_send)
        result = self._request(PATH, envelope, "replace-response", before_send=before_send)
        self.validate_result(request, predecessor, result)
        return result
