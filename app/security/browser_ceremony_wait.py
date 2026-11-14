"""Held ceremony outcome hints. A hint only starts proof reconciliation and never settles anything."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import AsyncIterator, Callable

import httpx

from app.onboarding import timing
from app.security.browser_ceremony import _P256_ORDER
from app.security.hosted_grants import HostedGrantUnavailable, HostedTransport, HTTPXHostedTransport
from app.security.initial_handoff import _require_proof_budget, timestamp
from app.security.installation_continuation import ExistingInstallationKey, _decode, _document, canonical_request
from app.security.installation_key import verify_installation_proof

PROFILE = "urn:bridge-clean:browser-ceremony-wait:v1"
PROOF_PROFILE = "urn:bridge-clean:browser-ceremony-wait-proof:v1"
CHALLENGE_PATH = "/v1/onboarding/browser-ceremony-wait-proof-challenges"
WAIT_PATH = "/v1/onboarding/browser-ceremonies:wait"
PURPOSE = "browser-ceremony-wait"
DOMAIN = b"BRIDGE-CLEAN-BROWSER-CEREMONY-WAIT-PROOF-V1\0"
AUDIENCE = b"urn:bridge-clean:commercial-control-plane:browser-ceremony"
EVENT = "browser-ceremony"
MAX_REQUEST_BYTES = 8192
MAX_FRAME_BYTES = 1024
PRESENTATION = frozenset({"active", "retired", "expired"})
DOMAIN_STATUS = frozenset({"awaiting-authentication", "awaiting-confirmation", "pending", "ready",
                           "committed", "unavailable", "revoked"})
_UUID7 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
Hint = tuple[int, str, str]


class CeremonyWaitUnavailable(HostedGrantUnavailable):
    """The wait could not be held. `retry_after` is the server's hint in seconds, if any."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def wait_request(request: dict) -> dict:
    """The exact wait request for a known ceremony, from its v1 ceremony request."""
    if not isinstance(request.get("ceremony_id"), str) or not _UUID7.match(request["ceremony_id"]):
        raise HostedGrantUnavailable("Browser ceremony is not known yet")
    return {"profile": PROFILE, "operation_id": request["operation_id"], "binding": request["binding"],
            "ceremony_id": request["ceremony_id"]}


def proof_message(request: dict, challenge: str, key_jkt: str) -> bytes:
    digest = hashlib.sha256(canonical_request(request)).digest()
    fields = (_decode(challenge, 32), b"POST", WAIT_PATH.encode(), digest, PURPOSE.encode(),
              _decode(key_jkt, 32), AUDIENCE)
    return DOMAIN + b"".join(len(field).to_bytes(4, "big") + field for field in fields)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "")
    return float(value) if value.isdigit() and int(value) <= 300 else None


def parse_hint(raw: str, ceremony_id: str) -> Hint | None:
    """A closed event for this ceremony, or None for a frame about another ceremony."""
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        raise HostedGrantUnavailable("Invalid ceremony wait event") from None
    if (not isinstance(value, dict)
            or set(value) != {"profile", "ceremony_id", "revision", "presentation_state", "domain_status"}
            or value["profile"] != PROFILE or not isinstance(value["ceremony_id"], str)
            or not _UUID7.match(value["ceremony_id"])
            or type(value["revision"]) is not int or not 0 <= value["revision"] <= 9007199254740991
            or value["presentation_state"] not in PRESENTATION or value["domain_status"] not in DOMAIN_STATUS):
        raise HostedGrantUnavailable("Invalid ceremony wait event")
    if value["ceremony_id"] != ceremony_id:
        return None
    return value["revision"], value["presentation_state"], value["domain_status"]


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate key")
    return result


class BrowserCeremonyWaitClient:
    """Each held wait gets a fresh single-use proof. The stream carries hints only."""

    def __init__(self, transport: HostedTransport, key: ExistingInstallationKey, origin: str, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 stream_transport: httpx.AsyncBaseTransport | None = None) -> None:
        validation = HTTPXHostedTransport(origin)
        validation.close()
        self.transport, self.key, self.origin, self.monotonic = transport, key, origin, monotonic
        self.stream_transport = stream_transport

    def envelope(self, request: dict, *, before_send=None) -> dict:
        request = _document(canonical_request(request), limit=MAX_REQUEST_BYTES)
        if set(request) != {"profile", "operation_id", "binding", "ceremony_id"} or request["profile"] != PROFILE:
            raise HostedGrantUnavailable("Invalid ceremony wait request")
        if before_send is not None:
            before_send()
        key = self.key.reopen_existing()
        binding = request["binding"]
        if (binding["installation_key_id"], binding["installation_key_jkt"]) != (
                key.installation_key_id, key.installation_key_jkt):
            raise HostedGrantUnavailable("Browser ceremony key changed")
        started = self.monotonic()
        challenge = self._challenge({"profile": PROOF_PROFILE, "target": {"operation": PURPOSE, "request": request}})
        _require_proof_budget(started, self.monotonic())
        if challenge["request_digest"] != hashlib.sha256(canonical_request(request)).hexdigest():
            raise HostedGrantUnavailable("Ceremony wait challenge changed")
        message = proof_message(request, challenge["challenge"], key.installation_key_jkt)
        if before_send is not None:
            before_send()
        proof = self.key.sign_challenge(message)
        _require_proof_budget(started, self.monotonic())
        if (proof.installation_key_id != key.installation_key_id or proof.algorithm != "ES256"
                or len(proof.signature) != 64
                or not 0 < int.from_bytes(proof.signature[:32], "big") < _P256_ORDER
                or not 0 < int.from_bytes(proof.signature[32:], "big") <= _P256_ORDER // 2
                or not verify_installation_proof(key.public_key_jwk, message, proof)):
            raise HostedGrantUnavailable("Ceremony wait key proof is unavailable")
        return {"request": request, "proof": {"challenge": challenge["challenge"],
                "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode()}}

    def _challenge(self, body: dict) -> dict:
        try:
            with timing.exchange("wait", "challenge"):
                response = self.transport.request("POST", CHALLENGE_PATH, json_body=body)
        except Exception:
            raise CeremonyWaitUnavailable("Ceremony wait challenge is unavailable") from None
        if response.status_code == 429:
            raise CeremonyWaitUnavailable("Ceremony wait is rate limited")
        if (response.status_code != 200
                or response.content_type.split(";", 1)[0].strip().lower() != "application/json"):
            raise CeremonyWaitUnavailable("Ceremony wait challenge is unavailable")
        value = _document(response.body, limit=4096)
        if (set(value) != {"profile", "challenge", "request_digest", "expires_at"} or value["profile"] != PROOF_PROFILE
                or not isinstance(value["request_digest"], str) or not _HEX64.match(value["request_digest"])):
            raise HostedGrantUnavailable("Invalid ceremony wait challenge")
        _decode(value["challenge"], 32)
        timestamp(value["expires_at"])
        return value

    async def hints(self, envelope: dict) -> AsyncIterator[Hint]:
        """Yield each hint for this ceremony until the server ends the stream."""
        ceremony_id = envelope["request"]["ceremony_id"]
        async with httpx.AsyncClient(base_url=self.origin, follow_redirects=False, trust_env=False,
                                     transport=self.stream_transport,
                                     timeout=httpx.Timeout(35.0, connect=10.0)) as client:
            timing.observe("wait", "protected-send", "ok", 0)
            started = self.monotonic()
            async with client.stream("POST", WAIT_PATH, json=envelope,
                                     headers={"Accept": "text/event-stream", "Accept-Encoding": "identity"}) as response:
                timing.observe("wait", "protected-receive", "ok" if response.status_code == 200 else "error",
                    max(0, (self.monotonic() - started) * 1000))
                if response.status_code in {429, 503}:
                    raise CeremonyWaitUnavailable("Ceremony wait is unavailable", retry_after=_retry_after(response))
                if (response.status_code != 200
                        or response.headers.get("content-type", "").split(";")[0] != "text/event-stream"
                        or response.headers.get("content-encoding", "identity").lower() != "identity"):
                    raise CeremonyWaitUnavailable("Ceremony wait is unavailable")
                pending = bytearray()
                event, data, size = "", [], 0
                async for chunk in response.aiter_bytes():
                    pending.extend(chunk)
                    if len(pending) > MAX_FRAME_BYTES and b"\n" not in pending:
                        raise HostedGrantUnavailable("Ceremony wait line too large")
                    while b"\n" in pending:
                        raw, _, tail = pending.partition(b"\n")
                        pending = bytearray(tail)
                        size += len(raw) + 1
                        if size > MAX_FRAME_BYTES:
                            raise HostedGrantUnavailable("Ceremony wait frame too large")
                        try:
                            line = raw.rstrip(b"\r").decode("utf-8")
                        except UnicodeError:
                            raise HostedGrantUnavailable("Invalid ceremony wait encoding") from None
                        if not line:
                            if data or event:
                                if event != EVENT or len(data) != 1:
                                    raise HostedGrantUnavailable("Unexpected ceremony wait event")
                                hint = parse_hint(data[0], ceremony_id)
                                if hint is not None:
                                    yield hint
                            event, data, size = "", [], 0
                        elif line.startswith("event:"):
                            if event:
                                raise HostedGrantUnavailable("Duplicate ceremony wait event field")
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif line.startswith(":"):
                            size = 0 if not (event or data) else size
                        else:
                            raise HostedGrantUnavailable("Unknown ceremony wait field")
                if pending or data or event:
                    raise HostedGrantUnavailable("Incomplete ceremony wait frame")
