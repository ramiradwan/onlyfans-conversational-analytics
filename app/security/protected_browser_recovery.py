"""Protected authority-recovery ceremony v2 and authentication selection v3.

Only exact retained recovery sources may use these separately domain-separated
protocols. Their browser and selection responses are presentation facts, never
installation grants or proof of local admission. Callers own durable admission,
single-send claims, exact reconciliation and explicit browser dispatch.
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
from app.security.browser_ceremony import binding_digest, expected_completion
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.initial_handoff import _require_proof_budget, timestamp
from app.security.installation_authority_recovery import PROFILE as RECOVERY_PROFILE, exact_request, scope_digest
from app.security.installation_continuation import _P256_ORDER, _decode, _document, canonical_request, signing_reference
from app.security.installation_key import verify_installation_proof
from contracts.loader import verify_snapshot_integrity

CEREMONY = "browser-ceremony"
AUTHENTICATION = "browser-authentication-selection"
CEREMONY_PROFILE = "urn:bridge-clean:browser-ceremony:v2"
AUTHENTICATION_PROFILE = "urn:bridge-clean:browser-authentication-selection:v3"
LIMIT = 8192
RESPONSE_LIMIT = 16384
CONFIG = {
    CEREMONY: ("v2", "browser-ceremony", "/v2/onboarding/browser-ceremony-proof-challenges",
        frozenset({"prepare", "read", "recover", "cancel"})),
    AUTHENTICATION: ("v3", "browser-authentication", "/v3/onboarding/browser-authentication-selection-proof-challenges",
        frozenset({"read", "select", "cancel"})),
}


class ProtectedBrowserRefused(ValueError):
    """A closed presentation refusal, never evidence of domain rollback."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Protected browser recovery was refused")


class Contract:
    def __init__(self, family):
        version = CONFIG[family][0]
        root = resource_path("contracts")
        verify_snapshot_integrity(root)
        documents = [json.loads(path.read_text("utf-8")) for path in (root / "schemas").rglob("*.schema.json")]
        registry = Registry().with_resources((doc["$id"], Resource.from_contents(doc)) for doc in documents)
        self.validators = {doc["$id"].rsplit("/", 1)[-1].removesuffix(".schema.json"):
            Draft202012Validator(doc, registry=registry) for doc in documents if f"/schemas/{family}/{version}/" in doc["$id"]}
        self.profile = json.loads((root / f"profiles/{family}/{version}/profile.json").read_text("utf-8"))
        self.proof = json.loads((root / f"profiles/{family}-proof/{version}/profile.json").read_text("utf-8"))

    def validate(self, schema, value):
        try:
            self.validators[schema].validate(value)
        except (ValidationError, ValueError, TypeError, RecursionError):
            raise HostedGrantUnavailable("Invalid protected browser document") from None
        return value


@lru_cache(maxsize=2)
def contract(family):
    if family not in CONFIG:
        raise ValueError("Unsupported protected browser family")
    return Contract(family)


def validate_binding(binding):
    """The source request and all duplicated scope coordinates are immutable."""
    source = binding["source"]
    request = source["request"]
    if (request != exact_request(request["recovery_id"], request["scope"])
            or source["request_digest"] != hashlib.sha256(canonical_request(request)).hexdigest()
            or source["scope_digest"] != scope_digest(request["scope"])
            or any(binding[name] != request["scope"][name] for name in (
                "installation_id", "installation_key_id", "installation_key_jkt", "organization_id"))):
        raise HostedGrantUnavailable("Protected recovery source changed")
    _decode(source["reference"], 32)
    _decode(binding["attempt_id"], 32)
    timestamp(binding["source_expires_at"])


def recovery_binding(request, prepared, *, attempt_id, completion):
    """Build only from the owner-retained exact prepare, not a caller actor."""
    source = {"kind": "authority-recovery", "profile": RECOVERY_PROFILE, "request": request,
        "request_digest": hashlib.sha256(canonical_request(request)).hexdigest(),
        "scope_digest": scope_digest(request["scope"]), "reference": prepared["reference"]}
    binding = {"source": source, "attempt_id": attempt_id, "completion": completion,
        "source_expires_at": prepared["expires_at"], "purpose": "recover-installation-authority",
        **{name: request["scope"][name] for name in ("installation_id", "installation_key_id",
            "installation_key_jkt", "organization_id")}}
    # Validate the closed nested binding with its actual published request schema.
    ceremony_request(request["recovery_id"], binding)
    return _document(canonical_request(binding), limit=LIMIT)


def challenge_target(family, verb, request):
    _, purpose, _, allowed = CONFIG[family]
    if verb not in allowed:
        raise ValueError("Unsupported protected browser operation")
    selected = contract(family)
    target = selected.validate("proof-challenge-request", {"profile": selected.proof["profile"],
        "target": {"operation": purpose + "-" + verb, "request": request}})
    validate_binding(request["binding"])
    if family == AUTHENTICATION and verb == "select" and (
            request["authentication_attempt_id"] == request["expected_previous_attempt_id"]):
        raise HostedGrantUnavailable("Protected authentication successor must be distinct")
    return target


def ceremony_request(operation_id, binding, *, ceremony_id=None):
    result = {"profile": CEREMONY_PROFILE, "operation_id": operation_id, "binding": binding}
    if ceremony_id is not None:
        result["ceremony_id"] = ceremony_id
    challenge_target(CEREMONY, "read" if ceremony_id is not None else "prepare", result)
    return _document(canonical_request(result), limit=LIMIT)


def authentication_request(verb, ceremony, *, attempt_id=None, previous_id=None):
    if verb not in CONFIG[AUTHENTICATION][3]:
        raise ValueError("Unsupported protected authentication operation")
    result = {"profile": AUTHENTICATION_PROFILE, "operation_id": ceremony["operation_id"],
        "ceremony_id": ceremony["ceremony_id"], "binding": ceremony["binding"],
        "authentication_attempt_id": attempt_id}
    if verb == "select":
        result.update(provider="default", expected_previous_attempt_id=previous_id)
    elif previous_id is not None:
        raise ValueError("Only selection can name a predecessor")
    challenge_target(AUTHENTICATION, verb, result)
    return _document(canonical_request(result), limit=LIMIT)


def validate_ceremony_result(verb, request, result, *, now, expires_at=None):
    challenge_target(CEREMONY, verb, request)
    contract(CEREMONY).validate(verb+"-response", result)
    binding = request["binding"]
    if result["operation_id"] != request["operation_id"]:
        raise HostedGrantUnavailable("Protected ceremony operation changed")
    if verb != "prepare" and result["status"] == "unavailable":
        return result
    view = result if verb == "prepare" else result["snapshot"]
    if (view["operation_id"] != request["operation_id"] or view["binding_digest"] != binding_digest(binding)
            or ("ceremony_id" in request and view["ceremony_id"] != request["ceremony_id"])
            or (expires_at is not None and view["expires_at"] != expires_at)
            or (verb != "prepare" and view["attempt_id"] != binding["attempt_id"])):
        raise HostedGrantUnavailable("Protected ceremony identity changed")
    expiry = timestamp(view["expires_at"])
    if expiry > timestamp(binding["source_expires_at"]):
        raise HostedGrantUnavailable("Protected ceremony deadline changed")
    entry = result if verb == "prepare" else result.get("entry")
    if entry is not None:
        if (not now < expiry <= now + timedelta(seconds=1800)
                or entry["completion"] != expected_completion(binding)):
            raise HostedGrantUnavailable("Protected ceremony entry is unavailable")
        _decode(entry["entry_reference"], 32)
    # A retired/expired committed snapshot is still an original commitment fact.
    # It must be reconciled by the recovery owner's separate delivery API.
    return result


def validate_authentication_result(verb, request, result, *, now, expires_at):
    challenge_target(AUTHENTICATION, verb, request)
    contract(AUTHENTICATION).validate(verb+"-response", result)
    binding = request["binding"]
    if (result["operation_id"] != request["operation_id"] or result["ceremony_id"] != request["ceremony_id"]
            or result["binding_digest"] != binding_digest(binding)
            or result["expires_at"] != expires_at or timestamp(expires_at) > timestamp(binding["source_expires_at"])):
        raise HostedGrantUnavailable("Protected authentication scope changed")
    attempt, requested = result["authentication_attempt"], request["authentication_attempt_id"]
    if attempt is not None and requested is not None and attempt["authentication_attempt_id"] != requested:
        raise HostedGrantUnavailable("Protected authentication child changed")
    if result["state"] in {"selected", "retired", "reauthentication-required", "admitted"} and attempt is None:
        raise HostedGrantUnavailable("Protected authentication child is unavailable")
    actions = result["actions"]
    if ((actions["select"] and (result["state"] not in {"selection-required", "retired"}
            or not result["choices"]["default"]))
            or (actions["cancel"] and result["state"] != "selected")
            or (actions["open_browser"] and (result["state"] != "selected" or not result["choices"]["default"]))
            or (timestamp(expires_at) <= now and any(actions.values()))):
        raise HostedGrantUnavailable("Protected authentication actions are unavailable")
    # Unlike ordinary selection, the Provider may include current actions in
    # an exact-child read. Its caller must use such a read only to observe that
    # child, never to authorize a new browser dispatch or successor selection.
    return result


class _ProtectedProofClient:
    family = None

    def __init__(self, transport, key, *, clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        self.transport, self.key, self.clock, self.monotonic = transport, key, clock, monotonic
        self.contract = contract(self.family)
        _, self.purpose, self.challenge_path, self.allowed = CONFIG[self.family]

    def _request(self, path, value, schema, guard, *, challenge=False):
        if len(canonical_request(value)) > LIMIT:
            raise HostedGrantUnavailable("Protected browser request exceeds limit")
        guard()
        try:
            response = self.transport.request("POST", path, json_body=value)
        except Exception:
            raise HostedGrantUnavailable("Protected browser outcome is unknown") from None
        if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise HostedGrantUnavailable("Protected browser response type is invalid")
        result = _document(response.body, limit=RESPONSE_LIMIT)
        if response.status_code != 200:
            self.contract.validate("error", result)
            reason = result["reason"]
            if challenge or response.status_code != self.contract.profile["refusal_status"][reason] or reason in {
                    "rate_limited", "temporarily_unavailable"}:
                raise HostedGrantUnavailable("Protected browser outcome is unknown")
            raise ProtectedBrowserRefused(reason)
        return self.contract.validate(schema, result)

    def _exchange(self, verb, request, *, before_send):
        # A missing owner guard is never a supported convenience here.
        if not callable(before_send):
            raise HostedGrantUnavailable("Protected browser owner is unavailable")
        request = _document(canonical_request(request), limit=LIMIT)
        target = challenge_target(self.family, verb, request)
        binding = request["binding"]
        before_send()
        key = signing_reference(self.key)
        if (key.installation_key_id, key.installation_key_jkt) != (binding["installation_key_id"], binding["installation_key_jkt"]):
            raise HostedGrantUnavailable("Protected browser key changed")
        purpose = self.purpose + "-" + verb
        route = self.contract.proof["purposes"][purpose]
        started = self.monotonic()
        def guard():
            before_send()
            _require_proof_budget(started, self.monotonic())
        challenge = self._request(self.challenge_path, target, "proof-challenge-response", guard, challenge=True)
        _require_proof_budget(started, self.monotonic())
        timestamp(challenge["expires_at"])
        digest = hashlib.sha256(canonical_request(request)).digest()
        if challenge["request_digest"] != digest.hex():
            raise HostedGrantUnavailable("Protected browser challenge changed")
        fields = (_decode(challenge["challenge"], 32), route["method"].encode(), route["path"].encode(),
            digest, purpose.encode(), _decode(key.installation_key_jkt, 32), self.contract.proof["audience"].encode())
        message = self.contract.proof["domain"].encode() + b"".join(len(field).to_bytes(4, "big")+field for field in fields)
        guard()
        signature = self.key.sign_challenge(message)
        _require_proof_budget(started, self.monotonic())
        if (signature.installation_key_id != key.installation_key_id or signature.algorithm != "ES256"
                or len(signature.signature) != 64 or not 0 < int.from_bytes(signature.signature[:32], "big") < _P256_ORDER
                or not 0 < int.from_bytes(signature.signature[32:], "big") <= _P256_ORDER // 2
                or not verify_installation_proof(key.public_key_jwk, message, signature)):
            raise HostedGrantUnavailable("Protected browser proof unavailable")
        envelope = self.contract.validate(verb+"-request", {"request": request, "proof": {"challenge": challenge["challenge"],
            "signature": base64.urlsafe_b64encode(signature.signature).rstrip(b"=").decode()}})
        result = self._request(route["path"], envelope, verb+"-response", guard)
        before_send()
        return request, result


class ProtectedCeremonyClient(_ProtectedProofClient):
    family = CEREMONY

    def execute(self, verb, request, *, before_send, expires_at=None):
        request, result = self._exchange(verb, request, before_send=before_send)
        return validate_ceremony_result(verb, request, result, now=self.clock(), expires_at=expires_at)


class ProtectedAuthenticationClient(_ProtectedProofClient):
    family = AUTHENTICATION

    def execute(self, verb, request, *, before_send, expires_at):
        request, result = self._exchange(verb, request, before_send=before_send)
        return validate_authentication_result(verb, request, result, now=self.clock(), expires_at=expires_at)
