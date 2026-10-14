"""Independent Product reader for the published delivery-recovery contract."""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
from datetime import datetime
from decimal import Decimal
from functools import lru_cache

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from app.core.resource_paths import resource_path
from app.security.hosted_grants import TransportResponse
from contracts.loader import verify_snapshot_integrity

PROFILE = "urn:bridge-clean:capability-license-delivery-recovery:v1"
PROOF_PROFILE = "urn:bridge-clean:capability-license-delivery-recovery-proof:v1"
AUDIENCE = "urn:bridge-clean:commercial-control-plane:capability-license-delivery-recovery"
DOMAIN = b"BRIDGE-CLEAN-CAPABILITY-LICENSE-DELIVERY-RECOVERY-PROOF-V1\0"
CHALLENGE_PATH = "/v1/capability-license-delivery-recovery-proof-challenges"
PREFIX = "/v1/capability-license-delivery-recoveries:"
MAX_GENERATION = 9007199254740991
ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
_STATUS = {"invalid": {400, 413}, "proof_invalid": {401}, "authority_unavailable": {403},
    "generation_conflict": {409}, "generation_superseded": {409}, "invalid_generation": {409},
    "delivery_completed": {409}, "rate_limited": {429}, "temporarily_unavailable": {503}}


class RecoveryUnavailable(ValueError):
    """No authoritative delivery result was obtained."""


class RecoveryRefused(RecoveryUnavailable):
    def __init__(self, reason, retry_at=None):
        super().__init__("Delivery recovery was refused")
        self.reason, self.retry_at = reason, retry_at


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _integer(token: str) -> int:
    coefficient = token.lower().split("e", 1)[0].lstrip("-+").replace(".", "")
    if not coefficient.strip("0"):
        return 0
    value = Decimal(token)
    if not value.is_finite() or not -MAX_GENERATION <= value <= MAX_GENERATION or value != value.to_integral_value():
        raise ValueError("Invalid delivery generation")
    return int(value)


def _members(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate delivery member")
        value[key] = item
    return value


def decode(raw: bytes, *, limit: int = 4096) -> dict:
    try:
        if not isinstance(raw, bytes) or len(raw) > min(limit, 4096):
            raise ValueError()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_members,
            parse_float=_integer, parse_int=_integer, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError()
        # Encoding the entire decoded document also rejects lone UTF-16 surrogates.
        canonical(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, ArithmeticError):
        raise RecoveryUnavailable("Invalid delivery recovery document") from None


@lru_cache(maxsize=1)
def _validators():
    root = resource_path("contracts")
    verify_snapshot_integrity(root)
    documents = [json.loads(path.read_text("utf-8")) for path in (root / "schemas").rglob("*.schema.json")]
    registry = Registry().with_resources((item["$id"], Resource.from_contents(item)) for item in documents)
    prefix = "capability-license-delivery-recovery-"
    return {item["$id"].rsplit("/", 1)[-1].removeprefix(prefix).removesuffix(".schema.json"):
        Draft202012Validator(item, registry=registry) for item in documents
        if item["$id"].rsplit("/", 1)[-1].startswith(prefix)}


def validate(name: str, value: dict) -> dict:
    try:
        value = decode(canonical(value))
        _validators()[name].validate(value)
        return value
    except (ValueError, TypeError, ValidationError, RecursionError):
        raise RecoveryUnavailable("Invalid delivery recovery document") from None


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise RecoveryUnavailable("Invalid delivery recovery timestamp") from None


def unbase64(value: str, size: int) -> bytes:
    try:
        result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if len(result) != size or base64.urlsafe_b64encode(result).rstrip(b"=").decode() != value:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise RecoveryUnavailable("Invalid delivery proof encoding") from None


def proof_bytes(action: str, request: dict, challenge: str) -> bytes:
    if action not in {"read", "recover"}:
        raise RecoveryUnavailable("Invalid delivery proof purpose")
    binding = request["binding"]
    fields = (unbase64(challenge, 32), b"POST", (PREFIX + action).encode(),
        hashlib.sha256(canonical(request)).digest(), ("capability-license-delivery-" + action).encode(),
        binding["installation_id"].encode(), binding["organization_id"].encode(),
        binding["installation_key_id"].encode(), AUDIENCE.encode())
    return DOMAIN + b"".join(struct.pack("!I", len(field)) + field for field in fields)


class DeliveryRecoveryClient:
    def __init__(self, transport, key, *, monotonic=time.monotonic):
        self.transport, self.key, self.monotonic = transport, key, monotonic

    def prepare(self, action, operation_id, binding, generation):
        request = {"profile": PROFILE, "activation_operation_id": operation_id,
            "delivery_generation": generation, "binding": binding}
        purpose = "capability-license-delivery-" + action
        document = validate("proof-challenge-request", {"profile": PROOF_PROFILE, "purpose": purpose, "request": request})
        started = self.monotonic()
        challenge = self._request(CHALLENGE_PATH, document, "proof-challenge-response", {201})
        issued, expires = timestamp(challenge["issued_at"]), timestamp(challenge["expires_at"])
        lifetime = (expires - issued).total_seconds()
        if (challenge["purpose"] != purpose or challenge["request_digest"] != hashlib.sha256(canonical(request)).hexdigest()
                or not 0 < lifetime <= 60):
            raise RecoveryUnavailable("Delivery proof challenge changed")
        # Provider owns absolute expiry. Count the entire round trip against its
        # lifetime without requiring the desktop's wall clock to match Provider.
        if not 0 <= self.monotonic() - started < lifetime:
            raise RecoveryUnavailable("Delivery installation proof expired")
        proof = self.key.sign_challenge(proof_bytes(action, request, challenge["challenge"]))
        if (proof.installation_key_id != binding["installation_key_id"] or proof.algorithm != "ES256"
                or len(proof.signature) != 64 or not 0 < int.from_bytes(proof.signature[:32], "big") < ORDER
                or not 0 < int.from_bytes(proof.signature[32:], "big") <= ORDER // 2):
            raise RecoveryUnavailable("Delivery installation proof changed")
        if not 0 <= self.monotonic() - started < lifetime:
            raise RecoveryUnavailable("Delivery installation proof expired")
        return validate(action + "-request", {"request": request, "proof": {
            "key_id": binding["installation_key_id"], "challenge": challenge["challenge"],
            "signature": base64.urlsafe_b64encode(proof.signature).rstrip(b"=").decode("ascii")}})

    def read(self, operation_id, binding, generation=None):
        return self.send("read", self.prepare("read", operation_id, binding, generation))

    def send(self, action, envelope):
        validate(action + "-request", envelope)
        request = envelope["request"]
        result = self._request(PREFIX + action, envelope, action + "-response", {200} if action == "read" else {200, 201})
        if any(result[field] != request[field] for field in ("activation_operation_id", "binding")):
            raise RecoveryUnavailable("Delivery response scope changed")
        if action == "recover":
            if (result["delivery_generation"] != request["delivery_generation"]
                    or not 0 < (timestamp(result["redemption"]["expires_at"]) - timestamp(result["authorized_at"])).total_seconds() <= 600):
                raise RecoveryUnavailable("Delivery authorization changed")
        else:
            requested, current, state = result["requested_generation"], result["current_generation"], result["state"]
            if (requested != request["delivery_generation"]
                    or state in {"authorization-available", "authorization-expired"} and requested not in {None, current}
                    or state == "not-issued" and not requested > current
                    or state == "superseded" and not requested < current):
                raise RecoveryUnavailable("Delivery read generation changed")
            if state == "authorization-expired" and timestamp(result["retry_at"]) < timestamp(result["expires_at"]):
                raise RecoveryUnavailable("Delivery retry time changed")
        return result

    def _request(self, path, document, schema, statuses):
        try:
            response = self.transport.request("POST", path, json_body=document)
        except Exception:
            raise RecoveryUnavailable("Delivery recovery transport unavailable") from None
        if (not isinstance(response, TransportResponse) or type(response.status_code) is not int
                or response.content_type.split(";", 1)[0].strip().lower() != "application/json"):
            raise RecoveryUnavailable("Invalid delivery recovery response")
        value = decode(response.body)
        if response.status_code not in statuses:
            error = validate("error", value)
            if response.status_code not in _STATUS[error["reason"]]:
                raise RecoveryUnavailable("Invalid delivery refusal status")
            raise RecoveryRefused(error["reason"], timestamp(error["retry_at"]).timestamp() if "retry_at" in error else None)
        return validate(schema, value)
