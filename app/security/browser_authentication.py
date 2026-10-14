"""Provider-selected authentication presentation over the original ceremony binding."""
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

PROFILE = "urn:bridge-clean:browser-authentication-selection:v1"
PROOF_PROFILE = "urn:bridge-clean:browser-authentication-selection-proof:v1"
CHALLENGE_PATH = "/v1/onboarding/browser-authentication-selection-proof-challenges"
PATH_PREFIX = "/v1/onboarding/browser-authentication-selections:"
MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 16384
_P256_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
_REFUSAL_STATUS = {
    "invalid": {400, 413}, "proof_invalid": {401}, "authentication_required": {401},
    "operation_conflict": {409}, "attempt_conflict": {409}, "provider_unavailable": {409},
    "attempt_limit": {409}, "authorization_denied": {403},
    "expired": {410}, "not_ready": {409}, "rate_limited": {429},
    "temporarily_unavailable": {503},
}


class BrowserAuthenticationRefused(ValueError):
    """A closed Provider refusal; no rollback or local grant revocation is implied."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("Authentication selection was refused")


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
            if "/schemas/browser-authentication-selection/v1/" in value["$id"]}
        self.proof = json.loads(
            (root / "profiles/browser-authentication-selection-proof/v1/profile.json").read_text("utf-8"))

    def validate(self, name: str, value: dict) -> dict:
        try:
            self.validators[name].validate(value)
        except (ValidationError, TypeError, ValueError, RecursionError):
            raise HostedGrantUnavailable("Invalid authentication selection document") from None
        return value


@lru_cache(maxsize=1)
def _contract() -> _Contract:
    return _Contract()


def binding_digest(binding: dict) -> str:
    return hashlib.sha256(canonical_request(binding)).hexdigest()



class BrowserAuthenticationClient:
    """Each request sends once; uncertain selection is reconciled against its exact child attempt."""

    def __init__(self, transport: HostedTransport, key: ExistingInstallationKey, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.transport, self.key, self.monotonic, self.clock = transport, key, monotonic, clock
        self.contract = _contract()

    def envelope(self, operation: str, request: dict, *, before_send=None) -> dict:
        if operation not in {"read", "select", "cancel", "switch"}:
            raise ValueError("Invalid authentication selection operation")
        purpose = "browser-authentication-" + operation
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
            raise HostedGrantUnavailable("Authentication selection key changed")
        started = self.monotonic()
        challenge = self._request(CHALLENGE_PATH, challenge_request,
                                  schema="proof-challenge-response", before_send=before_send)
        _require_proof_budget(started, self.monotonic())
        digest = hashlib.sha256(canonical_request(request)).digest()
        if challenge["request_digest"] != digest.hex():
            raise HostedGrantUnavailable("Authentication selection challenge changed")
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
            raise HostedGrantUnavailable("Authentication selection key proof is unavailable")
        result = {"request": request, "proof": {"challenge": challenge["challenge"],
                  "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode()}}
        self.contract.validate(operation + "-request", result)
        return result

    def read(self, request: dict, *, before_send=None, expires_at: str | None = None) -> dict:
        return self.execute("read", request, before_send=before_send, expires_at=expires_at)

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
            raise HostedGrantUnavailable("Authentication selection scope changed")
        if (expires_at is not None and result["expires_at"] != expires_at
                or timestamp(result["expires_at"]) > timestamp(binding["source_expires_at"])):
            raise HostedGrantUnavailable("Authentication selection deadline changed")
        attempt = result["authentication_attempt"]
        requested = request["authentication_attempt_id"]
        if (operation == "read" and requested is not None
                and (any(result["actions"].values()) or result["state"] == "reauthentication-required")):
            raise HostedGrantUnavailable("Historical authentication selection became actionable")
        if timestamp(result["expires_at"]) <= self.clock() and (
                any(result["actions"].values()) or result["state"] not in {"expired", "unavailable"}):
            raise HostedGrantUnavailable("Authentication selection expired")
        reused_admission = operation == "select" and result["state"] == "admitted" and (
            attempt is None or attempt["authentication_attempt_id"] == request["expected_previous_attempt_id"])
        if attempt is None and requested is not None and result["state"] == "admitted" and not reused_admission:
            raise HostedGrantUnavailable("Authentication selection attempt is unavailable")
        if attempt is not None:
            if requested is not None and attempt["authentication_attempt_id"] != requested and not reused_admission:
                raise HostedGrantUnavailable("Authentication selection attempt changed")
            if operation in {"select", "switch"} and not reused_admission and (
                    attempt["provider"] != request["provider"]
                    or attempt["mode"] != ("switch-account" if operation == "switch" else "sign-in")):
                raise HostedGrantUnavailable("Authentication selection provider changed")

    def _request(self, path, body, *, schema, before_send):
        if len(canonical_request(body)) > MAX_REQUEST_BYTES:
            raise HostedGrantUnavailable("Authentication selection request exceeds its limit")
        if before_send is not None:
            before_send()
        try:
            response = self.transport.request("POST", path, json_body=body)
        except Exception:
            raise HostedGrantUnavailable("Authentication selection result is unavailable") from None
        if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise HostedGrantUnavailable("Authentication selection response type is invalid")
        value = _document(response.body, limit=MAX_RESPONSE_BYTES)
        if response.status_code != 200:
            self.contract.validate("error", value)
            reason = value["reason"]
            if response.status_code not in _REFUSAL_STATUS[reason]:
                raise HostedGrantUnavailable("Authentication selection refusal is unconfirmed")
            if reason in {"rate_limited", "temporarily_unavailable"}:
                raise HostedGrantUnavailable("Authentication selection result is unavailable")
            raise BrowserAuthenticationRefused(reason)
        return self.contract.validate(schema, value)
