"""Purpose-bound initial key admission, separate from registered continuity."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, Mapping

import httpx

from app.security.hosted_grants import (
    HostedGrantUnavailable, HostedTransport, InstallationProofAuthority,
    HTTPXHostedTransport, _response_object,
)

PROFILE = "urn:bridge-clean:initial-installation-handoff:v1"
PROOF_PROFILE = "urn:bridge-clean:initial-installation-handoff-proof:v1"
CHALLENGE_PATH = "/v1/onboarding/installation-handoff-proof-challenges"
PATH_PREFIX = "/v1/onboarding/installation-handoffs:"
_DOMAIN = b"BRIDGE-CLEAN-INITIAL-INSTALLATION-HANDOFF-PROOF-V1\0"
_AUDIENCE = b"urn:bridge-clean:commercial-control-plane:initial-installation-handoff"
_B64 = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class InitialHandoffRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__("Initial handoff was refused")
        self.code = code


def canonical(value: object) -> bytes:
    # Contract fields contain strings, bounded integers, arrays and objects only.
    # All member names are ASCII; sort_keys therefore matches RFC8785 ordering.
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _decode(value: str) -> bytes:
    if not isinstance(value, str) or not _B64.fullmatch(value):
        raise HostedGrantUnavailable("Invalid initial handoff value")
    raw = base64.urlsafe_b64decode(value + "=")
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise HostedGrantUnavailable("Noncanonical initial handoff value")
    return raw


def valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}", value))


def valid_uuid7(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value))


def timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", value):
        raise HostedGrantUnavailable("Invalid hosted timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HostedGrantUnavailable("Invalid hosted timestamp") from None


def _object(raw: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise HostedGrantUnavailable("Duplicate hosted field")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise HostedGrantUnavailable("Invalid initial handoff document") from None
    if not isinstance(value, dict):
        raise HostedGrantUnavailable("Invalid initial handoff document")
    return value


def validate_wait_event(value: dict) -> dict:
    status = value.get("status")
    fields = {
        "waiting": {"profile", "status", "expires_at", "revision"},
        "authorized": {"profile", "status", "scope", "expires_at", "revision"},
        "recovery-required": {"profile", "status", "receipt", "revision"},
        "expired": {"profile", "status", "revision"},
        "reauthentication-required": {"profile", "status", "revision"},
        "revoked": {"profile", "status", "revision"},
    }
    revision = value.get("revision")
    if (value.get("profile") != PROFILE or set(value) != fields.get(status)
            or type(revision) is not int or not 0 <= revision <= 9007199254740991):
        raise HostedGrantUnavailable("Invalid initial handoff event")
    if "scope" in value:
        validate_scope(value["scope"])
    if "receipt" in value:
        validate_receipt(value["receipt"])
    if "expires_at" in value:
        timestamp(value["expires_at"])
    return value


def validate_scope(scope: dict) -> None:
    fields = {"onboarding_transaction_id", "organization_id", "intended_creator_id", "association_request_id",
              "installation_id", "installation_key_jkt", "action_digests", "authorization_revision"}
    if not isinstance(scope, dict) or set(scope) != fields:
        raise HostedGrantUnavailable("Invalid initial scope")
    if (type(scope["authorization_revision"]) is not int or not 1 <= scope["authorization_revision"] <= 9007199254740991
            or not all(valid_identifier(scope[name]) for name in fields - {"authorization_revision", "action_digests"})
            or not valid_uuid7(scope["association_request_id"])):
        raise HostedGrantUnavailable("Invalid initial scope")
    digests = scope["action_digests"]
    if (not isinstance(digests, dict) or set(digests) != {"issue-installation-claim", "approve-creator-association"}
            or not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in digests.values())):
        raise HostedGrantUnavailable("Invalid initial scope")
    _decode(scope["installation_key_jkt"])


def validate_receipt(receipt: dict) -> None:
    fields = {"claim_id", "operation_id", "onboarding_transaction_id", "organization_id", "installation_id",
              "installation_key_id", "installation_key_jkt", "consumed_at"}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or not all(isinstance(v, str) and 0 < len(v) <= 128 for v in receipt.values())):
        raise HostedGrantUnavailable("Invalid initial receipt")
    _decode(receipt["installation_key_jkt"])
    if (not valid_uuid7(receipt["claim_id"]) or not valid_uuid7(receipt["operation_id"])
            or not re.fullmatch(r"ik1\.[A-Za-z0-9_-]{22}", receipt["installation_key_id"])
            or not all(valid_identifier(receipt[name]) for name in {"onboarding_transaction_id", "organization_id", "installation_id"})):
        raise HostedGrantUnavailable("Invalid initial receipt")
    timestamp(receipt["consumed_at"])


class InitialHandoffClient:
    def __init__(self, transport: HostedTransport, key: InstallationProofAuthority,
                 *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.transport = transport
        self.key = key
        self.clock = clock

    def envelope(self, operation: str, request: dict, *, before_send: Callable[[], None] | None = None) -> dict:
        if operation not in {"prepare", "wait", "complete", "receipt"}:
            raise ValueError("Invalid handoff operation")
        key = self.key.ensure_ready()
        purpose = "initial-handoff-" + operation
        challenge = self._request(CHALLENGE_PATH, {"profile": PROOF_PROFILE,
            "target": {"operation": purpose, "request": request}}, expected=200, before_send=before_send)
        digest = hashlib.sha256(canonical(request)).digest()
        if (set(challenge) != {"profile", "challenge", "request_digest", "expires_at"}
                or challenge["profile"] != PROOF_PROFILE or challenge["request_digest"] != digest.hex()):
            raise HostedGrantUnavailable("Invalid initial proof challenge")
        try:
            expiry = timestamp(challenge["expires_at"])
            remaining = (expiry - self.clock()).total_seconds()
        except (TypeError, ValueError, AttributeError):
            raise HostedGrantUnavailable("Invalid initial proof expiry") from None
        if not 0 < remaining <= 60:
            raise HostedGrantUnavailable("Initial proof challenge expired")
        pieces = (_decode(challenge["challenge"]), b"POST", (PATH_PREFIX + operation).encode(), digest,
                  purpose.encode(), _decode(key.installation_key_jkt), _AUDIENCE)
        message = _DOMAIN + b"".join(len(piece).to_bytes(4, "big") + piece for piece in pieces)
        proof = self.key.sign_challenge(message)
        if proof.installation_key_id != key.installation_key_id or proof.algorithm != "ES256" or len(proof.signature) != 64:
            raise HostedGrantUnavailable("Initial key proof unavailable")
        return {"request": request, "proof": {"challenge": challenge["challenge"], "key_id": key.installation_key_id,
                "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode()}}

    def prepare(self, request: dict, *, before_send: Callable[[], None] | None = None) -> dict:
        envelope = self.envelope("prepare", request, before_send=before_send)
        result = self._request(PATH_PREFIX + "prepare", envelope, expected=201, before_send=before_send)
        if (set(result) != {"profile", "reference", "operation_id", "expires_at"}
                or result["profile"] != PROFILE or result["operation_id"] != request["operation_id"]):
            raise HostedGrantUnavailable("Invalid initial preparation")
        _decode(result["reference"])
        expiry = timestamp(result["expires_at"])
        if not 0 < (expiry - self.clock()).total_seconds() <= 600:
            raise HostedGrantUnavailable("Initial preparation expired")
        return result

    def complete(self, request: dict) -> dict:
        result = self._request(PATH_PREFIX + "complete", self.envelope("complete", request), expected=200)
        allowed = {"profile", "status", "bootstrap"} if result.get("status") == "completed" else {"profile", "status", "receipt"}
        if set(result) != allowed or result.get("profile") != PROFILE or result.get("status") not in {"completed", "recovery-required"}:
            raise HostedGrantUnavailable("Invalid initial completion")
        if "receipt" in result:
            validate_receipt(result["receipt"])
        return result

    def receipt(self, request: dict) -> dict:
        result = self._request(PATH_PREFIX + "receipt", self.envelope("receipt", request), expected=200)
        if set(result) != {"profile", "status", "receipt"} or result.get("profile") != PROFILE or result.get("status") != "recovery-required":
            raise HostedGrantUnavailable("Invalid initial receipt")
        validate_receipt(result["receipt"])
        return result["receipt"]

    def _request(self, path: str, body: dict, *, expected: int,
                 before_send: Callable[[], None] | None = None) -> dict:
        if before_send is not None:
            before_send()
        try:
            response = self.transport.request("POST", path, json_body=body)
        except Exception:
            raise HostedGrantUnavailable("Initial handoff result unavailable") from None
        if response.status_code != expected:
            if response.status_code >= 500 or response.status_code in {408, 429}:
                raise HostedGrantUnavailable("Initial handoff result unavailable")
            # Do not diagnose a cause from status alone.
            raise InitialHandoffRefused("handoff_refused")
        return dict(_response_object(response))


class OnboardingStreamAdmissionRefused(HostedGrantUnavailable):
    """Registered stream admission refused; not a local revocation decision."""


class InitialHandoffStream:
    """Bounded POST SSE transport. Each connection receives a fresh body proof."""
    def __init__(self, origin: str, *, continuity: bool = False) -> None:
        validation = HTTPXHostedTransport(origin)
        validation.close()
        self.origin = origin
        self.continuity = continuity

    async def events(self, envelope: dict) -> AsyncIterator[dict]:
        path = "/v1/onboarding/streams" if self.continuity else PATH_PREFIX + "wait"
        event_name = "onboarding" if self.continuity else "initial-handoff"
        maximum = 16384 if self.continuity else 8192
        async with httpx.AsyncClient(base_url=self.origin, follow_redirects=False, trust_env=False,
                                     timeout=httpx.Timeout(35.0, connect=10.0)) as client:
            async with client.stream("POST", path, json=envelope,
                                     headers={"Accept": "text/event-stream", "Accept-Encoding": "identity"}) as response:
                if self.continuity and response.status_code == 403:
                    raise OnboardingStreamAdmissionRefused("Onboarding stream admission refused")
                if (response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "text/event-stream"
                        or response.headers.get("content-encoding", "identity").lower() != "identity"):
                    raise HostedGrantUnavailable("Onboarding stream unavailable")
                pending = bytearray()
                event, event_id, data, size = "", None, [], 0
                # A fixed HTTPX chunk size buffers small SSE frames until enough
                # bytes arrive or the connection closes. Parse each arrival now;
                # the line and frame limits below still bound the response.
                async for chunk in response.aiter_bytes():
                    pending.extend(chunk)
                    if len(pending) > maximum and b"\n" not in pending:
                        raise HostedGrantUnavailable("Onboarding stream line too large")
                    while b"\n" in pending:
                        raw, _, tail = pending.partition(b"\n")
                        pending = bytearray(tail)
                        size += len(raw) + 1
                        if size > maximum:
                            raise HostedGrantUnavailable("Onboarding stream frame too large")
                        try:
                            line = raw.rstrip(b"\r").decode("utf-8")
                        except UnicodeError:
                            raise HostedGrantUnavailable("Invalid onboarding stream encoding") from None
                        if not line:
                            if data:
                                if event != event_name:
                                    raise HostedGrantUnavailable("Unexpected onboarding stream event")
                                if self.continuity and len(data) != 1:
                                    raise HostedGrantUnavailable("Hosted stream requires one data line")
                                value = _object("\n".join(data))
                                if self.continuity:
                                    projection = value.get("snapshot", {})
                                    if event_id != f"{projection.get('epoch')}:{projection.get('revision')}":
                                        raise HostedGrantUnavailable("Hosted stream cursor mismatch")
                                yield value if self.continuity else validate_wait_event(value)
                            event, event_id, data, size = "", None, [], 0
                        elif line.startswith("event:"):
                            if event:
                                raise HostedGrantUnavailable("Duplicate onboarding event field")
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif line.startswith("id:"):
                            if event_id is not None:
                                raise HostedGrantUnavailable("Duplicate onboarding cursor")
                            event_id = line[3:].strip()
                        elif not line.startswith(":"):
                            raise HostedGrantUnavailable("Unknown onboarding stream field")
                if pending or data or event or event_id is not None:
                    raise HostedGrantUnavailable("Incomplete onboarding stream frame")
