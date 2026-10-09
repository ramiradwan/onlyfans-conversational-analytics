"""Registered-key continuation transport using the immutable hosted contract."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import AsyncIterator, Callable, Protocol

import httpx
from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from app.core.resource_paths import resource_path
from app.persistence.auth import InstallationKeyReference
from app.security.hosted_grants import HTTPXHostedTransport, HostedGrantUnavailable, HostedTransport
from app.security.initial_handoff import _require_proof_budget, timestamp
from app.security.installation_key import InstallationProof, verify_installation_proof
from contracts.loader import verify_snapshot_integrity


PROFILE = "urn:bridge-clean:installation-setup-continuation:v2"
PROOF_PROFILE = "urn:bridge-clean:installation-setup-continuation-proof:v2"
CHALLENGE_PATH = "/v2/onboarding/installation-continuation-proof-challenges"
PATH_PREFIX = "/v2/onboarding/installation-continuations:"
MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 16384
_P256_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
_REFUSAL_STATUS = {
    "invalid": {400, 413}, "proof_invalid": {401}, "scope_conflict": {409},
    "authorization_denied": {403}, "expired": {410}, "not_ready": {409},
    "rate_limited": {429}, "temporarily_unavailable": {503},
}


class InstallationContinuationRefused(ValueError):
    """A verified closed refusal; it does not revoke any local signed grant."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("Installation continuation was refused")


class ExistingInstallationKey(Protocol):
    def reopen_existing(self) -> InstallationKeyReference: ...
    def sign_challenge(self, challenge: bytes) -> InstallationProof: ...


def _normalized(value: object, depth: int = 0) -> object:
    if depth > 16:
        raise HostedGrantUnavailable("Continuation document is too deep")
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise HostedGrantUnavailable("Invalid continuation member")
        for key in value:
            _normalized(key, depth + 1)
        return {key: _normalized(member, depth + 1) for key, member in value.items()}
    if isinstance(value, list):
        return [_normalized(member, depth + 1) for member in value]
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or abs(value) > 9007199254740991:
            raise HostedGrantUnavailable("Invalid continuation number")
        return int(value)
    if type(value) is int and abs(value) > 9007199254740991:
        raise HostedGrantUnavailable("Invalid continuation number")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise HostedGrantUnavailable("Invalid continuation string") from None
    return value


def canonical_request(value: dict) -> bytes:
    # Closed member names and string values are ASCII. Integral JSON numbers
    # share RFC8785 bytes regardless of decimal/exponent spelling or negative zero.
    try:
        return json.dumps(_normalized(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise HostedGrantUnavailable("Invalid continuation JSON") from None


def _decode(value: object, length: int) -> bytes:
    try:
        if not isinstance(value, str) or not value.isascii():
            raise ValueError
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if len(decoded) != length or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != value:
            raise ValueError
        return decoded
    except (ValueError, TypeError):
        raise HostedGrantUnavailable("Invalid continuation encoding") from None


def _document(raw: bytes, *, limit: int = MAX_RESPONSE_BYTES) -> dict:
    if len(raw) > limit:
        raise HostedGrantUnavailable("Continuation document exceeds its limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError
        return _normalized(value)
    except (ValueError, UnicodeError, RecursionError):
        raise HostedGrantUnavailable("Invalid continuation document") from None


class _Contract:
    def __init__(self) -> None:
        root = resource_path("contracts")
        verify_snapshot_integrity(root)
        directory = root / "schemas/installation-continuation/v2"
        documents = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.schema.json"))]
        documents.append(json.loads((root / "schemas/common/v1/definitions.schema.json").read_text(encoding="utf-8")))
        registry = Registry().with_resources((document["$id"], Resource.from_contents(document)) for document in documents)
        self.validators = {
            document["$id"].rsplit("/", 1)[-1].removesuffix(".schema.json"): Draft202012Validator(document, registry=registry)
            for document in documents[:-1]
        }
        self.proof = json.loads((root / "profiles/installation-setup-continuation-proof-v2/profile.json").read_text(encoding="utf-8"))

    def validate(self, name: str, value: dict) -> dict:
        try:
            self.validators[name].validate(value)
        except ValidationError:
            raise HostedGrantUnavailable("Invalid continuation contract document") from None
        return value


@lru_cache(maxsize=1)
def _contract() -> _Contract:
    return _Contract()


def validated_snapshot(value: dict, *, continuation_id: str, expires_at: str | None = None) -> dict:
    snapshot = _contract().validate("snapshot", _normalized(value))
    if snapshot["continuation_id"] != continuation_id or (expires_at is not None and snapshot["expires_at"] != expires_at):
        raise HostedGrantUnavailable("Continuation scope changed")
    timestamp(snapshot["committed_at"])
    timestamp(snapshot["expires_at"])
    return snapshot


class InstallationContinuationClient:
    def __init__(self, transport: HostedTransport, key: ExistingInstallationKey,
                 *, monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.transport, self.key, self.monotonic, self.clock = transport, key, monotonic, clock
        self.contract = _contract()

    def envelope(self, operation: str, request: dict, *, before_send: Callable[[], None] | None = None) -> dict:
        if operation not in {"prepare", "read", "stream", "binding"}:
            raise ValueError("Invalid continuation operation")
        purpose = "installation-continuation-" + operation
        request = _document(canonical_request(request), limit=MAX_REQUEST_BYTES)
        target = {"profile": PROOF_PROFILE, "target": {"operation": purpose, "request": request}}
        self.contract.validate("proof-challenge-request", target)
        if before_send is not None:
            before_send()
        key = self.key.reopen_existing()
        if request["scope"]["installation_key_id"] != key.installation_key_id:
            raise HostedGrantUnavailable("Continuation key changed")
        started = self.monotonic()
        challenge = self._request(CHALLENGE_PATH, target, expected=200,
                                  schema="proof-challenge-response", before_send=before_send)
        _require_proof_budget(started, self.monotonic())
        digest = hashlib.sha256(canonical_request(request)).digest()
        if challenge["request_digest"] != digest.hex():
            raise HostedGrantUnavailable("Continuation challenge does not match")
        timestamp(challenge["expires_at"])
        route = self.contract.proof["purposes"][purpose]
        fields = (_decode(challenge["challenge"], 32), route["method"].encode(), route["path"].encode(),
                  digest, purpose.encode(), _decode(key.installation_key_jkt, 32), self.contract.proof["audience"].encode())
        message = self.contract.proof["domain"].encode() + b"".join(len(field).to_bytes(4, "big") + field for field in fields)
        if before_send is not None:
            before_send()
        proof = self.key.sign_challenge(message)
        _require_proof_budget(started, self.monotonic())
        if (proof.installation_key_id != key.installation_key_id or proof.algorithm != "ES256"
                or len(proof.signature) != 64
                or not 0 < int.from_bytes(proof.signature[:32], "big") < _P256_ORDER
                or not 0 < int.from_bytes(proof.signature[32:], "big") <= _P256_ORDER // 2
                or not verify_installation_proof(key.public_key_jwk, message, proof)):
            raise HostedGrantUnavailable("Continuation key proof is unavailable")
        envelope = {"request": request, "proof": {"challenge": challenge["challenge"],
                    "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode()}}
        self.contract.validate(operation + "-request", envelope)
        if len(canonical_request(envelope)) > MAX_REQUEST_BYTES:
            raise HostedGrantUnavailable("Continuation request exceeds its limit")
        return envelope

    def prepare(self, request: dict, *, before_send: Callable[[], None] | None = None) -> dict:
        result = self._operation("prepare", request, expected=201, before_send=before_send)
        if result["operation_id"] != request["operation_id"]:
            raise HostedGrantUnavailable("Continuation operation changed")
        _decode(result["reference"], 32)
        if timestamp(result["expires_at"]) <= self.clock():
            raise HostedGrantUnavailable("Continuation result expired")
        return result

    def read(self, request: dict, *, before_send: Callable[[], None] | None = None) -> dict:
        result = self._operation("read", request, expected=200, before_send=before_send)
        if result["operation_id"] != request["operation_id"]:
            raise HostedGrantUnavailable("Continuation operation changed")
        if result["result"] == "found":
            if request["continuation_id"] is not None and result["continuation_id"] != request["continuation_id"]:
                raise HostedGrantUnavailable("Continuation context changed")
            _decode(result["reference"], 32)
            validated_snapshot(result["snapshot"], continuation_id=result["continuation_id"], expires_at=result["expires_at"])
        return result

    def binding(self, request: dict, *, before_send: Callable[[], None] | None = None) -> dict:
        """Return transport data; the grant owner must verify signed authority."""
        result = self._operation("binding", request, expected=200, before_send=before_send)
        if result["continuation_id"] != request["continuation_id"]:
            raise HostedGrantUnavailable("Continuation context changed")
        return result

    def _operation(self, operation, request, *, expected, before_send):
        envelope = self.envelope(operation, request, before_send=before_send)
        return self._request(PATH_PREFIX + operation, envelope, expected=expected,
                             schema=operation + "-response", before_send=before_send)

    def _request(self, path, body, *, expected, schema, before_send):
        if len(canonical_request(body)) > MAX_REQUEST_BYTES:
            raise HostedGrantUnavailable("Continuation request exceeds its limit")
        if before_send is not None:
            before_send()
        try:
            response = self.transport.request("POST", path, json_body=body)
        except Exception:
            raise HostedGrantUnavailable("Continuation result is unavailable") from None
        if response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise HostedGrantUnavailable("Continuation response type is invalid")
        value = _document(response.body)
        if response.status_code != expected:
            self.contract.validate("error", value)
            reason = value["reason"]
            if response.status_code not in _REFUSAL_STATUS[reason]:
                raise HostedGrantUnavailable("Continuation refusal is unconfirmed")
            if reason in {"rate_limited", "temporarily_unavailable"}:
                raise HostedGrantUnavailable("Continuation result is unavailable")
            raise InstallationContinuationRefused(reason)
        return self.contract.validate(schema, value)


async def continuation_frames(chunks: AsyncIterator[bytes], *, continuation_id: str,
                              expires_at: str | None = None) -> AsyncIterator[dict]:
    """Parse arrival-sized SSE chunks without waiting for a buffer to fill."""
    line = bytearray()
    frame_size = 0
    fields = {}
    first = True
    async for chunk in chunks:
        for byte in chunk:
            frame_size += 1
            if frame_size > MAX_RESPONSE_BYTES:
                raise HostedGrantUnavailable("Continuation event exceeds its limit")
            if byte != 10:
                line.append(byte)
                continue
            try:
                text = bytes(line).removesuffix(b"\r").decode("utf-8")
            except UnicodeError:
                raise HostedGrantUnavailable("Continuation event encoding is invalid") from None
            line.clear()
            if not text:
                if fields:
                    if set(fields) != {"event", "id", "data"} or fields["event"] != "installation-continuation":
                        raise HostedGrantUnavailable("Continuation event fields are invalid")
                    event = _contract().validate("event", _document(fields["data"].encode()))
                    snapshot = validated_snapshot(event["snapshot"], continuation_id=continuation_id, expires_at=expires_at)
                    if fields["id"] != f"{snapshot['epoch']}:{snapshot['revision']}" or (first and event["kind"] != "snapshot"):
                        raise HostedGrantUnavailable("Continuation event cursor is invalid")
                    first = False
                    yield event
                fields, frame_size = {}, 0
            elif text.startswith(":"):
                continue
            else:
                name, separator, value = text.partition(":")
                if not separator or name not in {"event", "id", "data"} or name in fields:
                    raise HostedGrantUnavailable("Continuation event field is invalid")
                fields[name] = value.removeprefix(" ")
    if line or fields or frame_size:
        raise HostedGrantUnavailable("Continuation event is incomplete")


class InstallationContinuationStream:
    def __init__(self, origin: str, *, client_factory=httpx.AsyncClient) -> None:
        validation = HTTPXHostedTransport(origin)
        validation.close()
        self.origin, self.client_factory = origin, client_factory

    async def events(self, envelope: dict, *, expires_at: str | None = None) -> AsyncIterator[dict]:
        _contract().validate("stream-request", envelope)
        body = canonical_request(envelope)
        if len(body) > MAX_REQUEST_BYTES:
            raise HostedGrantUnavailable("Continuation request exceeds its limit")
        async with self.client_factory(base_url=self.origin, follow_redirects=False, trust_env=False,
                                       timeout=httpx.Timeout(35.0, connect=10.0)) as client:
            async with client.stream("POST", PATH_PREFIX + "stream", content=body,
                                     headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                                              "Accept-Encoding": "identity"}) as response:
                if (response.status_code != 200
                        or response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream"
                        or response.headers.get("content-encoding", "identity").strip().lower() != "identity"):
                    raise HostedGrantUnavailable("Continuation stream is unavailable")
                async for event in continuation_frames(response.aiter_raw(),
                        continuation_id=envelope["request"]["continuation_id"], expires_at=expires_at):
                    yield event
