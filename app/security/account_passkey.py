"""Closed Product transport for the account-passkey native protocol.

The caller owns the setup operation and the locally permitted installation key.
A hosted response never opens a local session.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from functools import cached_property
from typing import Any

import rfc8785
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable

from app.core.resource_paths import resource_path
from app.security.hosted_grants import HostedGrantUnavailable, HostedTransport
from app.security.initial_handoff import _require_proof_budget, timestamp
from app.security.installation_continuation import (
    ExistingInstallationKey,
    _P256_ORDER,
    signing_reference,
)
from app.security.installation_key import InstallationKeyError, verify_installation_proof
from app.security.signed_objects import SignedObjectRejected, decode, encode, parse
from contracts.loader import verify_snapshot_integrity

PREFIX = "urn:bridge-clean:account-passkey-"
PROOF_PROFILE = PREFIX + "installation-proof:v1"
ENROLLMENT_PROFILE = PREFIX + "enrollment:v1"
BINDING_PROFILE = PREFIX + "local-binding:v1"
ACCESS_PROFILE = PREFIX + "initial-local-access:v1"
WITHDRAWAL_PROFILE = PREFIX + "local-withdrawal:v1"
PROOF_AUDIENCE = b"urn:bridge-clean:commercial-control-plane:account-passkey"
PROOF_DOMAIN = b"BRIDGE-CLEAN-ACCOUNT-PASSKEY-INSTALLATION-PROOF-V1\x00"
NATIVE_PATH = "/v1/account-passkeys/native"
REQUEST_LIMIT = 24_576
RESPONSE_LIMIT = 16_384

_SCHEMA_HOST = "https://contracts.bridge-clean.invalid/"
_SCHEMA_ROOT = "schemas/account-passkey/v1/"
_COMMON = "schemas/common/v1/definitions.schema.json"
_REFUSALS = {
    "invalid": 400,
    "proof_invalid": 401,
    "authorization_denied": 403,
    "operation_conflict": 409,
    "not_ready": 409,
    "result_unavailable": 410,
    "expired": 410,
    "rate_limited": 429,
    "temporarily_unavailable": 503,
}
# Each purpose is paired with its own path, request schema and success status.
_OPERATIONS = {
    "enrollment-begin": ("/enrollment:begin", "enrollment-begin", "enrollment-begin-response", 200),
    "enrollment-finish": ("/enrollment:finish", "enrollment-finish", "enrollment-result", 201),
    "enrollment-receipt": ("/enrollment:receipt", "enrollment-receipt", "enrollment-result", 200),
    "enrollment-cancel": (
        "/enrollment:cancel",
        "enrollment-cancel",
        "enrollment-cancel-response",
        200,
    ),
    "binding-read": ("/binding:read", "local-binding-read", "local-binding-read-response", 200),
    "initial-local-access": (
        "/local-access:issue",
        "initial-local-access",
        "initial-local-access-response",
        200,
    ),
    "withdrawal-ack": (
        "/withdrawal:ack",
        "local-withdrawal-ack",
        "local-withdrawal-ack-response",
        200,
    ),
}


class AccountPasskeyRefused(ValueError):
    """A closed refusal of a proof-bound operation."""

    def __init__(self, code: str, support_ref: str | None) -> None:
        self.code = code
        self.support_ref = support_ref
        super().__init__("Account passkey operation refused")


class AccountPasskeySchemas:
    """Resolve versioned schemas from a trusted, application-selected snapshot."""

    def __init__(self, document: Callable[[str], dict[str, Any]]) -> None:
        self._document = document
        self._validators: dict[str, Draft202012Validator] = {}

    @classmethod
    def from_release(cls) -> AccountPasskeySchemas:
        root = resource_path("contracts")
        manifest = verify_snapshot_integrity(root)
        files = {item["path"]: item["sha256"] for item in manifest["files"]}

        def load(path: str) -> dict[str, Any]:
            data = (root / path).read_bytes()
            if hashlib.sha256(data).hexdigest() != files[path]:
                raise ValueError("Account passkey schema changed")
            return json.loads(data)

        return cls(load)

    @cached_property
    def _registry(self) -> Registry:
        def retrieve(uri: str) -> Resource:
            if not uri.startswith(_SCHEMA_HOST):
                raise NoSuchResource(ref=uri)
            path = uri.removeprefix(_SCHEMA_HOST)
            if path != _COMMON and not path.startswith(_SCHEMA_ROOT):
                raise NoSuchResource(ref=uri)
            suffix = path.removeprefix(_SCHEMA_ROOT).removesuffix(".schema.json")
            if path != _COMMON and (not suffix or not suffix.replace("-", "").isalnum()):
                raise NoSuchResource(ref=uri)
            document = self._document(path)
            if document.get("$id") != uri:
                raise NoSuchResource(ref=uri)
            return Resource.from_contents(document)

        return Registry(retrieve=retrieve)

    def validate(self, schema: str, value: object) -> dict[str, Any]:
        try:
            validator = self._validators.get(schema)
            if validator is None:
                uri = f"{_SCHEMA_HOST}{_SCHEMA_ROOT}{schema}.schema.json"
                document = self._registry.get_or_retrieve(uri).value.contents
                validator = Draft202012Validator(document, registry=self._registry)
                self._validators[schema] = validator
            if not isinstance(value, dict):
                raise ValueError
            validator.validate(value)
        except (
            ValidationError,
            OSError,
            KeyError,
            TypeError,
            ValueError,
            RecursionError,
            NoSuchResource,
            Unresolvable,
            LookupError,
        ):
            raise HostedGrantUnavailable("Account passkey document is invalid") from None
        return value


def is_low_s(signature: bytes) -> bool:
    return (
        len(signature) == 64
        and 0 < int.from_bytes(signature[:32], "big") < _P256_ORDER
        and 0 < int.from_bytes(signature[32:], "big") <= _P256_ORDER // 2
    )


def proof_bytes(purpose: str, request: dict[str, Any], challenge: str, key_jkt: str) -> bytes:
    route, _, _, _ = _OPERATIONS[purpose]
    raw_challenge = decode(challenge, 32)
    thumbprint = decode(key_jkt, 32)
    if len(raw_challenge) != 32 or len(thumbprint) != 32:
        raise SignedObjectRejected()
    digest = hashlib.sha256(rfc8785.dumps(request)).digest()
    fields = (
        raw_challenge,
        b"POST",
        (NATIVE_PATH + route).encode("ascii"),
        digest,
        purpose.encode("ascii"),
        thumbprint,
        PROOF_AUDIENCE,
    )
    return PROOF_DOMAIN + b"".join(len(field).to_bytes(4, "big") + field for field in fields)


class AccountPasskeyNativeClient:
    """Obtain a fresh proof for every exact, guarded native request."""

    def __init__(
        self,
        transport: HostedTransport,
        key: ExistingInstallationKey,
        schemas: AccountPasskeySchemas,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._key = key
        self._schemas = schemas
        self._monotonic = monotonic

    def exchange(
        self,
        purpose: str,
        request: dict[str, Any],
        *,
        expected_key_jkt: str,
        before_send: Callable[[], None],
    ) -> dict[str, Any]:
        route, request_schema, response_schema, expected_status = _OPERATIONS[purpose]
        try:
            # Detach from the caller before computing the digest and proof. A
            # later mutation of the input cannot change the bound operation.
            request = parse(rfc8785.dumps(request), maximum=REQUEST_LIMIT)
            target = self._schemas.validate(
                "proof-challenge-request",
                {
                    "profile": PROOF_PROFILE,
                    "target": {"operation": purpose, "request": request},
                },
            )
        except (SignedObjectRejected, rfc8785.CanonicalizationError):
            raise HostedGrantUnavailable("Account passkey request is invalid") from None
        before_send()
        try:
            reference = signing_reference(self._key)
        except InstallationKeyError:
            raise HostedGrantUnavailable(
                "Account passkey installation key is unavailable"
            ) from None
        if reference.installation_key_jkt != expected_key_jkt:
            raise HostedGrantUnavailable("Account passkey installation key changed")

        started = self._monotonic()

        def guard() -> None:
            before_send()
            _require_proof_budget(started, self._monotonic())

        challenge = self._post(
            "/proof-challenges",
            target,
            "proof-challenge-response",
            200,
            guard,
            is_challenge=True,
        )
        timestamp(challenge["expires_at"])
        try:
            if challenge["request_digest"] != hashlib.sha256(rfc8785.dumps(request)).hexdigest():
                raise SignedObjectRejected()
            message = proof_bytes(purpose, request, challenge["challenge"], expected_key_jkt)
        except (SignedObjectRejected, rfc8785.CanonicalizationError):
            raise HostedGrantUnavailable("Account passkey challenge is invalid") from None
        guard()
        try:
            signature = self._key.sign_challenge(message)
        except InstallationKeyError:
            raise HostedGrantUnavailable("Account passkey proof is unavailable") from None
        if (
            signature.algorithm != "ES256"
            or signature.installation_key_id != reference.installation_key_id
            or not is_low_s(signature.signature)
            or not verify_installation_proof(reference.public_key_jwk, message, signature)
        ):
            raise HostedGrantUnavailable("Account passkey proof is unavailable")
        envelope = self._schemas.validate(
            request_schema + "-request",
            {
                "request": request,
                "proof": {
                    "profile": PROOF_PROFILE,
                    "challenge": challenge["challenge"],
                    "key_id": reference.installation_key_id,
                    "signature": encode(signature.signature),
                },
            },
        )
        return self._post(route, envelope, response_schema, expected_status, guard)

    def _post(
        self,
        route: str,
        value: dict[str, Any],
        schema: str,
        expected_status: int,
        guard: Callable[[], None],
        *,
        is_challenge: bool = False,
    ) -> dict[str, Any]:
        try:
            if len(rfc8785.dumps(value)) > REQUEST_LIMIT:
                raise SignedObjectRejected()
        except (SignedObjectRejected, rfc8785.CanonicalizationError):
            raise HostedGrantUnavailable("Account passkey request is invalid") from None
        guard()
        try:
            response = self._transport.request("POST", NATIVE_PATH + route, json_body=value)
        except Exception:
            raise HostedGrantUnavailable("Account passkey outcome is unknown") from None
        if response.content_type.partition(";")[0].strip().lower() != "application/json":
            raise HostedGrantUnavailable("Account passkey response type is invalid")
        try:
            result = parse(response.body, maximum=RESPONSE_LIMIT)
        except SignedObjectRejected:
            raise HostedGrantUnavailable("Account passkey response is invalid") from None
        if response.status_code == expected_status:
            return self._schemas.validate(schema, result)
        refusal = self._schemas.validate("native-error", result)
        code = refusal["error"]
        if (
            is_challenge
            or _REFUSALS.get(code) != response.status_code
            or code in {"rate_limited", "temporarily_unavailable"}
        ):
            raise HostedGrantUnavailable("Account passkey outcome is unknown")
        support = refusal.get("support_ref")
        if support is not None:
            try:
                if len(decode(support, 32)) != 32:
                    raise SignedObjectRejected()
            except SignedObjectRejected:
                raise HostedGrantUnavailable("Account passkey response is invalid") from None
        raise AccountPasskeyRefused(code, support)
