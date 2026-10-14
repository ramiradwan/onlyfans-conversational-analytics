"""Explicit desktop consent over Provider-held admission; no local license authority."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Callable

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from app.core.resource_paths import resource_path
from app.security.hosted_grants import HostedGrantUnavailable, HostedTransport
from app.security.initial_handoff import _require_proof_budget, timestamp
from app.security.installation_continuation import (
    ExistingInstallationKey, _decode, _document, canonical_request,
)
from app.security.installation_key import verify_installation_proof
from contracts.loader import verify_snapshot_integrity

PROFILE = "urn:bridge-clean:desktop-decision:v1"
PROOF_PROFILE = "urn:bridge-clean:desktop-decision-proof:v1"
CHALLENGE_PATH = "/v1/onboarding/desktop-decision-proof-challenges"
PATH_PREFIX = "/v1/onboarding/desktop-decisions:"
MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 16384
_P256_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
_REFUSAL_STATUS = {
    "invalid": {400, 413}, "proof_invalid": {401}, "authentication_required": {401},
    "operation_conflict": {409}, "decision_conflict": {409}, "authorization_denied": {403},
    "expired": {410}, "not_ready": {409}, "rate_limited": {429},
    "temporarily_unavailable": {503},
}


class DesktopDecisionRefused(ValueError):
    """A closed Provider refusal; no rollback or local grant revocation is implied."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("Desktop decision was refused")


class _Contract:
    def __init__(self) -> None:
        root = resource_path("contracts")
        verify_snapshot_integrity(root)
        documents = [json.loads(path.read_text(encoding="utf-8"))
                     for path in (root / "schemas").rglob("*.schema.json")]
        registry = Registry().with_resources(
            (value["$id"], Resource.from_contents(value)) for value in documents)
        self.validators = {
            value["$id"].rsplit("/", 1)[-1].removesuffix(".schema.json"):
            Draft202012Validator(value, registry=registry) for value in documents
            if "/schemas/desktop-decision/v1/" in value["$id"]}
        self.proof = json.loads(
            (root / "profiles/desktop-decision-proof/v1/profile.json").read_text("utf-8"))

    def validate(self, name: str, value: dict) -> dict:
        try:
            self.validators[name].validate(value)
        except (ValidationError, TypeError, ValueError, RecursionError):
            raise HostedGrantUnavailable("Invalid desktop decision document") from None
        return value


@lru_cache(maxsize=1)
def _contract() -> _Contract:
    return _Contract()


def binding_digest(binding: dict) -> str:
    return hashlib.sha256(canonical_request(binding)).hexdigest()


def decision_digest(decision: dict) -> str:
    return hashlib.sha256(canonical_request(decision)).hexdigest()


class DesktopDecisionClient:
    """Each invocation sends once; uncertain consent is reconciled by an explicit read."""

    def __init__(self, transport: HostedTransport, key: ExistingInstallationKey, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.transport, self.key, self.monotonic, self.clock = transport, key, monotonic, clock
        self.contract = _contract()

    def envelope(self, operation: str, request: dict, *, before_send=None) -> dict:
        if operation not in {"read", "submit"}:
            raise ValueError("Invalid desktop decision operation")
        purpose = "desktop-decision-" + operation
        request = _document(canonical_request(request), limit=MAX_REQUEST_BYTES)
        challenge_request = {"profile": PROOF_PROFILE,
                             "target": {"operation": purpose, "request": request}}
        self.contract.validate("proof-challenge-request", challenge_request)
        if before_send is not None:
            before_send()
        key = self.key.reopen_existing()
        binding = request["binding"]
        if (binding["installation_key_id"], binding["installation_key_jkt"]) != (
                key.installation_key_id, key.installation_key_jkt):
            raise HostedGrantUnavailable("Desktop decision key changed")
        started = self.monotonic()
        challenge = self._request(CHALLENGE_PATH, challenge_request,
                                  schema="proof-challenge-response", before_send=before_send)
        _require_proof_budget(started, self.monotonic())
        digest = hashlib.sha256(canonical_request(request)).digest()
        if challenge["request_digest"] != digest.hex():
            raise HostedGrantUnavailable("Desktop decision challenge changed")
        timestamp(challenge["expires_at"])
        route = self.contract.proof["purposes"][purpose]
        fields = (_decode(challenge["challenge"], 32), route["method"].encode(),
                  route["path"].encode(), digest, purpose.encode(),
                  _decode(key.installation_key_jkt, 32), self.contract.proof["audience"].encode())
        message = self.contract.proof["domain"].encode() + b"".join(
            len(field).to_bytes(4, "big") + field for field in fields)
        if before_send is not None:
            before_send()
        proof = self.key.sign_challenge(message)
        _require_proof_budget(started, self.monotonic())
        if (proof.installation_key_id != key.installation_key_id or proof.algorithm != "ES256"
                or len(proof.signature) != 64
                or not 0 < int.from_bytes(proof.signature[:32], "big") < _P256_ORDER
                or not 0 < int.from_bytes(proof.signature[32:], "big") <= _P256_ORDER // 2
                or not verify_installation_proof(key.public_key_jwk, message, proof)):
            raise HostedGrantUnavailable("Desktop decision key proof is unavailable")
        result = {"request": request, "proof": {"challenge": challenge["challenge"],
                  "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode()}}
        self.contract.validate(operation + "-request", result)
        return result

    def read(self, request: dict, *, before_send=None, expires_at: str | None = None) -> dict:
        return self.execute("read", request, before_send=before_send, expires_at=expires_at)

    def submit(self, request: dict, *, before_send=None, expires_at: str | None = None) -> dict:
        return self.execute("submit", request, before_send=before_send, expires_at=expires_at)

    def execute(self, operation: str, request: dict, *, before_send=None,
                expires_at: str | None = None) -> dict:
        started = self.monotonic()

        def guarded_send():
            if before_send is not None:
                before_send()
            _require_proof_budget(started, self.monotonic())

        envelope = self.envelope(operation, request, before_send=guarded_send)
        result = self._request(PATH_PREFIX + operation, envelope,
                               schema=operation + "-response", before_send=guarded_send)
        self.validate_result(operation, envelope["request"], result, expires_at=expires_at)
        return result

    def validate_result(self, operation: str, request: dict, result: dict, *,
                        expires_at: str | None = None) -> None:
        self.contract.validate(operation + "-response", result)
        binding = request["binding"]
        if (result["operation_id"] != request["operation_id"]
                or result["ceremony_id"] != request["ceremony_id"]
                or result["binding_digest"] != binding_digest(binding)):
            raise HostedGrantUnavailable("Desktop decision scope changed")
        if result["state"] != "confirmation-required":
            return
        decision = result["decision"]
        _decode(result["decision_reference"], 32)
        if (result["decision_digest"] != decision_digest(decision)
                or decision["action"] != binding["purpose"]):
            raise HostedGrantUnavailable("Desktop decision display changed")
        expiry = timestamp(decision["expires_at"])
        if (expiry <= self.clock() or expiry > timestamp(binding["source_expires_at"])
                or (expires_at is not None and expiry > timestamp(expires_at))):
            raise HostedGrantUnavailable("Desktop decision deadline changed")
        if decision["action"] == "connect-account":
            if decision["creator_id"] != binding["intended_creator_id"]:
                raise HostedGrantUnavailable("Desktop decision account changed")
        else:
            fallback = decision["offer"]["fallback_major_versions"]
            if fallback != sorted(fallback):
                raise HostedGrantUnavailable("Desktop decision terms are not canonical")

    def _request(self, path, body, *, schema, before_send):
        if len(canonical_request(body)) > MAX_REQUEST_BYTES:
            raise HostedGrantUnavailable("Desktop decision request exceeds its limit")
        if before_send is not None:
            before_send()
        try:
            response = self.transport.request("POST", path, json_body=body)
        except Exception:
            raise HostedGrantUnavailable("Desktop decision result is unavailable") from None
        if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise HostedGrantUnavailable("Desktop decision response type is invalid")
        value = _document(response.body, limit=MAX_RESPONSE_BYTES)
        if response.status_code != 200:
            self.contract.validate("error", value)
            reason = value["reason"]
            if response.status_code not in _REFUSAL_STATUS[reason]:
                raise HostedGrantUnavailable("Desktop decision refusal is unconfirmed")
            if reason in {"rate_limited", "temporarily_unavailable"}:
                raise HostedGrantUnavailable("Desktop decision result is unavailable")
            raise DesktopDecisionRefused(reason)
        return self.contract.validate(schema, value)
