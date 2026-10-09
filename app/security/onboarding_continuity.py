"""Registered installation continuity proofs and closed hosted projections."""
from __future__ import annotations

import base64
import hashlib
import re
import time
from datetime import datetime, timezone

from app.security.initial_handoff import canonical, _decode, _require_proof_budget, timestamp
from app.security.hosted_grants import HostedGrantUnavailable, _response_object

PROFILE = "urn:bridge-clean:hosted-onboarding:v1"
PROOF_PROFILE = "urn:bridge-clean:onboarding-proof:v1"


class HostedOnboardingContinuity:
    def __init__(self, transport, key, *, clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
        self.transport, self.key, self.clock = transport, key, clock
        self.monotonic = monotonic

    def envelope(self, request: dict) -> dict:
        key = self.key.ensure_ready()
        started = self.monotonic()
        response = self.transport.request("POST", "/v1/onboarding/stream-proof-challenges", json_body={
            "profile": PROOF_PROFILE, "target": {"operation": "hosted-stream", "request": request}})
        _require_proof_budget(started, self.monotonic())
        if response.status_code != 200:
            raise HostedGrantUnavailable("Hosted continuity proof unavailable")
        value = _response_object(response)
        digest = hashlib.sha256(canonical(request)).digest()
        if (set(value) != {"profile", "challenge", "expires_at", "request_digest"}
                or value["profile"] != PROOF_PROFILE or value["request_digest"] != digest.hex()):
            raise HostedGrantUnavailable("Invalid continuity challenge")
        timestamp(value["expires_at"])
        fields = (_decode(value["challenge"]), b"POST", b"/v1/onboarding/streams", digest,
                  b"hosted-stream", _decode(key.installation_key_jkt),
                  b"urn:bridge-clean:commercial-control-plane:onboarding")
        message = b"BRIDGE-CLEAN-ONBOARDING-PROOF-V1\0" + b"".join(len(x).to_bytes(4, "big") + x for x in fields)
        proof = self.key.sign_challenge(message)
        _require_proof_budget(started, self.monotonic())
        if proof.installation_key_id != key.installation_key_id or proof.algorithm != "ES256" or len(proof.signature) != 64:
            raise HostedGrantUnavailable("Continuity key proof unavailable")
        return {"request": request, "proof": {"challenge": value["challenge"],
            "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode()}}


def validated_event(value: dict, *, transaction_id: str) -> dict:
    if (not isinstance(value, dict) or set(value) != {"profile", "kind", "snapshot"} or value["profile"] != PROFILE
            or value["kind"] not in {"snapshot", "committed"}):
        raise HostedGrantUnavailable("Invalid hosted event")
    snapshot = value["snapshot"]
    if (not isinstance(snapshot, dict) or set(snapshot) != {"profile", "source", "onboarding_transaction_id",
            "epoch", "revision", "committed_at", "facts"} or snapshot["profile"] != PROFILE
            or snapshot["source"] != "hosted-onboarding" or snapshot["onboarding_transaction_id"] != transaction_id
            or not isinstance(snapshot["epoch"], str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", snapshot["epoch"])
            or type(snapshot["revision"]) is not int or not 0 <= snapshot["revision"] <= 9007199254740991):
        raise HostedGrantUnavailable("Invalid hosted snapshot")
    facts = snapshot["facts"]
    enums = {"identity": {"verified"}, "authorization": {"current", "authentication-required", "denied"},
             "claim": {"none", "ready", "consuming", "consumed", "expired", "replaced"},
             "association": {"none", "pending", "approved", "rejected", "revoked", "expired"},
             "commercial": {"none", "awaiting-confirmation", "committed", "declined"},
             "transfer": {"none", "issued", "redeemed", "expired", "replaced"}}
    if (not isinstance(facts, dict) or set(facts) != {*enums, "milestones"}
            or any(not isinstance(facts[name], str) or facts[name] not in values for name, values in enums.items())):
        raise HostedGrantUnavailable("Invalid hosted facts")
    milestones = facts["milestones"]
    timestamp(snapshot["committed_at"])
    if not isinstance(milestones, list) or len(milestones) > 4:
        raise HostedGrantUnavailable("Invalid hosted milestones")
    seen = set()
    order = ["installed", "enrolled", "account-bound", "first-capture-ready"]
    last_index = -1
    for entry in milestones:
        if (not isinstance(entry, dict) or set(entry) != {"milestone", "occurred_at"}
                or not isinstance(entry["milestone"], str) or entry["milestone"] not in order
                or entry["milestone"] in seen or not isinstance(entry["occurred_at"], str)):
            raise HostedGrantUnavailable("Invalid hosted milestone")
        seen.add(entry["milestone"])
        current_index = order.index(entry["milestone"])
        if current_index <= last_index:
            raise HostedGrantUnavailable("Invalid hosted milestone order")
        last_index = current_index
        timestamp(entry["occurred_at"])
    return snapshot
