"""Installation-key proof and exact result validation for a focused Provider task."""
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
from app.security.hosted_grants import HostedGrantUnavailable, HostedTransport, HTTPXHostedTransport
from app.security.initial_handoff import _require_proof_budget, timestamp
from app.security.installation_continuation import (
    ExistingInstallationKey, _decode, _document, canonical_request,
)
from app.security.installation_key import verify_installation_proof
from contracts.loader import verify_snapshot_integrity

PROFILE = "urn:bridge-clean:browser-ceremony:v1"
PROOF_PROFILE = "urn:bridge-clean:browser-ceremony-proof:v1"
CHALLENGE_PATH = "/v1/onboarding/browser-ceremony-proof-challenges"
PATH_PREFIX = "/v1/onboarding/browser-ceremonies:"
ENTRY_PREFIX = "/onboarding/browser-ceremonies/"
MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 16384
_P256_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
_REFUSAL_STATUS = {
    "invalid": {400, 413}, "proof_invalid": {401}, "operation_conflict": {409},
    "authorization_denied": {403}, "expired": {410}, "not_ready": {409},
    "rate_limited": {429}, "temporarily_unavailable": {503},
}


class BrowserCeremonyRefused(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("Browser task was refused")


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
            if "/schemas/browser-ceremony/v1/" in value["$id"]}
        self.proof = json.loads((root / "profiles/browser-ceremony-proof/v1/profile.json").read_text("utf-8"))

    def validate(self, name: str, value: dict) -> dict:
        try:
            self.validators[name].validate(value)
        except (ValidationError, TypeError, ValueError, RecursionError):
            raise HostedGrantUnavailable("Invalid browser ceremony document") from None
        return value


@lru_cache(maxsize=1)
def _contract() -> _Contract:
    return _Contract()


def binding_digest(binding: dict) -> str:
    return hashlib.sha256(canonical_request(binding)).hexdigest()


def browser_entry_url(origin: str, reference: str) -> str:
    """Only the release-selected Provider origin and fixed entry path can be opened."""
    validation = HTTPXHostedTransport(origin)
    validation.close()
    _decode(reference, 32)
    return origin + ENTRY_PREFIX + reference


def expected_completion(binding: dict) -> dict:
    selected = binding["completion"]
    if selected["mode"] == "system-browser":
        return {"mode": "system-browser"}
    return {"mode": "extension-auth-flow", "return_uri":
            f"https://{selected['extension_id']}.chromiumapp.org/browser-ceremony/v1/complete"}


class BrowserCeremonyClient:
    """Transport never retries a mutation or creates a replacement operation."""

    def __init__(self, transport: HostedTransport, key: ExistingInstallationKey, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.transport, self.key, self.monotonic, self.clock = transport, key, monotonic, clock
        self.contract = _contract()

    def envelope(self, operation: str, request: dict, *, before_send=None) -> dict:
        if operation not in {"prepare", "read", "recover", "cancel"}:
            raise ValueError("Invalid browser ceremony operation")
        purpose = "browser-ceremony-" + operation
        request = _document(canonical_request(request), limit=MAX_REQUEST_BYTES)
        challenge_request = {"profile": PROOF_PROFILE,
                             "target": {"operation": purpose, "request": request}}
        self.contract.validate("proof-challenge-request", challenge_request)
        if before_send is not None:
            before_send()
        # Initial source preparation already created the key. A task can only
        # reopen it, including when that same key has since been registered.
        key = self.key.reopen_existing()
        binding = request["binding"]
        if (binding["installation_key_id"], binding["installation_key_jkt"]) != (
                key.installation_key_id, key.installation_key_jkt):
            raise HostedGrantUnavailable("Browser ceremony key changed")
        started = self.monotonic()
        challenge = self._request(CHALLENGE_PATH, challenge_request,
                                  schema="proof-challenge-response", before_send=before_send)
        _require_proof_budget(started, self.monotonic())
        digest = hashlib.sha256(canonical_request(request)).digest()
        if challenge["request_digest"] != digest.hex():
            raise HostedGrantUnavailable("Browser ceremony challenge changed")
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
            raise HostedGrantUnavailable("Browser ceremony key proof is unavailable")
        result = {"request": request, "proof": {"challenge": challenge["challenge"],
                  "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode()}}
        self.contract.validate(operation + "-request", result)
        return result

    def prepare(self, request: dict, *, before_send=None) -> dict:
        return self.execute("prepare", request, before_send=before_send)

    def read(self, request: dict, *, expires_at: str | None = None, before_send=None) -> dict:
        return self.execute("read", request, expires_at=expires_at, before_send=before_send)

    def recover(self, request: dict, *, expires_at: str | None = None, before_send=None) -> dict:
        return self.execute("recover", request, expires_at=expires_at, before_send=before_send)

    def cancel(self, request: dict, *, expires_at: str | None = None, before_send=None) -> dict:
        return self.execute("cancel", request, expires_at=expires_at, before_send=before_send)

    def execute(self, operation: str, request: dict, *, expires_at: str | None = None,
                before_send=None) -> dict:
        envelope = self.envelope(operation, request, before_send=before_send)
        result = self._request(PATH_PREFIX + operation, envelope,
                               schema=operation + "-response", before_send=before_send)
        self.validate_result(operation, request, result, expires_at=expires_at)
        return result

    def validate_result(self, operation: str, request: dict, result: dict, *,
                        expires_at: str | None = None) -> None:
        self.contract.validate(operation + "-response", result)
        binding = request["binding"]
        if result["operation_id"] != request["operation_id"]:
            raise HostedGrantUnavailable("Browser ceremony operation changed")
        if operation != "prepare" and result["status"] == "unavailable":
            return
        view = result if operation == "prepare" else result["snapshot"]
        if (view["operation_id"] != request["operation_id"]
                or view["binding_digest"] != binding_digest(binding)
                or ("ceremony_id" in request and view["ceremony_id"] != request["ceremony_id"])
                or (expires_at is not None and view["expires_at"] != expires_at)
                or (operation != "prepare" and view["attempt_id"] != binding["attempt_id"])):
            raise HostedGrantUnavailable("Browser ceremony scope changed")
        expiry = timestamp(view["expires_at"])
        if expiry > timestamp(binding["source_expires_at"]):
            raise HostedGrantUnavailable("Browser ceremony deadline changed")
        if operation == "prepare" and expiry <= self.clock():
            raise HostedGrantUnavailable("Browser ceremony entry expired")
        entry = result if operation == "prepare" else result.get("entry")
        if entry is not None:
            _decode(entry["entry_reference"], 32)
            if entry["completion"] != expected_completion(binding):
                raise HostedGrantUnavailable("Browser ceremony completion changed")
        if "result" in view:
            self._domain_result(binding, view["result"])

    @staticmethod
    def _domain_result(binding: dict, result: dict) -> None:
        kind, source = result["kind"], binding["source"]
        if kind == "initial-handoff-receipt" and source["kind"] == "initial" and binding["purpose"] == "connect-account":
            receipt = result["receipt"]["receipt"]
            if (receipt["operation_id"] == source["operation_id"]
                    and all(receipt[name] == binding[name] for name in (
                        "installation_id", "installation_key_id", "installation_key_jkt"))):
                return
        if (kind == "registered-binding-ready" and source["kind"] == "registered"
                and binding["purpose"] == "connect-account"
                and result["continuation_id"] == source["continuation_id"]):
            return
        if (kind == "activation-redemption" and binding["purpose"] == "activate-complimentary"
                and result["activation_operation_id"] == binding["activation_operation_id"]):
            _decode(result["redemption"]["continuation"].removeprefix("clr1."), 32)
            timestamp(result["redemption"]["expires_at"])
            return
        raise HostedGrantUnavailable("Browser ceremony domain result changed")

    def _request(self, path, body, *, schema, before_send):
        if len(canonical_request(body)) > MAX_REQUEST_BYTES:
            raise HostedGrantUnavailable("Browser ceremony request exceeds its limit")
        if before_send is not None:
            before_send()
        try:
            response = self.transport.request("POST", path, json_body=body)
        except Exception:
            raise HostedGrantUnavailable("Browser ceremony result is unavailable") from None
        if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise HostedGrantUnavailable("Browser ceremony response type is invalid")
        value = _document(response.body, limit=MAX_RESPONSE_BYTES)
        if response.status_code != 200:
            self.contract.validate("error", value)
            reason = value["reason"]
            if response.status_code not in _REFUSAL_STATUS[reason]:
                raise HostedGrantUnavailable("Browser ceremony refusal is unconfirmed")
            if reason in {"rate_limited", "temporarily_unavailable"}:
                raise HostedGrantUnavailable("Browser ceremony result is unavailable")
            raise BrowserCeremonyRefused(reason)
        return self.contract.validate(schema, value)
