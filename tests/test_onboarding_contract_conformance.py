"""Qualify the pinned protocol; these oracles do not claim runtime adoption."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

pytestmark = [pytest.mark.ci_tier("fast")]

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
VECTORS = CONTRACTS / "onboarding-continuity-v1"
ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def records(name: str) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], json.loads((VECTORS / name).read_bytes()))


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def decode(value: str) -> bytes:
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise ValueError("noncanonical encoding")
    return raw


def accepts_proof(record: dict[str, Any]) -> bool:
    try:
        key = record["public_key"]
        if set(key) != {"crv", "kty", "x", "y"} or key["crv"] != "P-256" or key["kty"] != "EC":
            return False
        route = {
            "transfer-redeem": "/v1/onboarding/transfers:redeem",
            "transfer-recover": "/v1/onboarding/transfers:recover",
            "hosted-stream": "/v1/onboarding/streams",
        }[record["operation"]]
        challenge = decode(record["challenge"])
        if len(challenge) != 32:
            return False
        pieces = (
            challenge, b"POST", route.encode(),
            hashlib.sha256(canonical(record["request"])).digest(),
            record["operation"].encode(), hashlib.sha256(canonical(key)).digest(),
            b"urn:bridge-clean:commercial-control-plane:onboarding",
        )
        message = b"BRIDGE-CLEAN-ONBOARDING-PROOF-V1\0" + b"".join(
            len(piece).to_bytes(4, "big") + piece for piece in pieces
        )
        if message.hex() != record["proof_bytes_hex"]:
            return False
        signature = decode(record["signature"])
        if len(signature) != 64:
            return False
        r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
        if not 0 < r < ORDER or not 0 < s <= ORDER // 2:
            return False
        public = ec.EllipticCurvePublicNumbers(
            int.from_bytes(decode(key["x"]), "big"),
            int.from_bytes(decode(key["y"]), "big"), ec.SECP256R1(),
        ).public_key()
        public.verify(utils.encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def cursor_decision(record: dict[str, Any]) -> str:
    if record["authenticated_transport_generation"] != record["frame_transport_generation"]:
        return "ignore"
    same_epoch = record["current_epoch"] == record["frame_epoch"]
    newer = record["frame_revision"] > record["current_revision"]
    if record["kind"] == "snapshot":
        if not same_epoch:
            return "apply" if record["initial_frame"] else "snapshot-required"
        return "apply" if newer else "ignore"
    if record["initial_frame"] or not same_epoch:
        return "snapshot-required"
    if not newer:
        return "ignore"
    return "apply" if record["frame_revision"] - record["current_revision"] == 1 else "snapshot-required"


def transfer_decision(record: dict[str, Any]) -> str:
    if any(record[name] != record["bound_" + name] for name in ("actor", "organization")):
        return "invalid"
    if record["rate_limited"]:
        return "rate_limited"
    if not record["proof_valid"] or record["proof_used"] or record["proof_expires"] <= record["now"]:
        return "proof_invalid"
    if record["kind"] != record["expected_kind"]:
        return "wrong_destination"
    if not record["request_matches"]:
        return "operation_conflict"
    if record["state"] == "replaced":
        return "replaced"
    if record["state"] == "redeemed":
        if record["destination"] != record["bound_destination"]:
            return "wrong_destination"
        if record["operation"] == record["bound_operation"]:
            return "expired" if record["now"] >= record.get("redeemed_at", 1000) + 1800 else "already-redeemed"
    if record["now"] >= record["expires"]:
        return "expired"
    return "used" if record["state"] == "redeemed" else "redeemed"


def test_onboarding_family_has_exact_manifest_closure() -> None:
    manifest = json.loads((VECTORS / "manifest.json").read_bytes())
    assert {path.name for path in VECTORS.iterdir()} == {"manifest.json", *(
        record["path"] for record in manifest["files"]
    )}
    for record in manifest["files"]:
        data = (VECTORS / record["path"]).read_bytes()
        assert len(data) == record["size"]
        assert hashlib.sha256(data).hexdigest() == record["sha256"]
    assert sum(len(records(record["path"])) for record in manifest["files"]) == 107


@pytest.mark.parametrize("case", records("schema-cases.json"), ids=lambda case: case["case_id"])
def test_pinned_schema_and_prohibited_field_cases(case: dict[str, Any]) -> None:
    resources = []
    for path in (CONTRACTS / "schemas").rglob("*.schema.json"):
        value = json.loads(path.read_bytes())
        resources.append((value["$id"], Resource.from_contents(value)))
    registry: Registry[Any] = Registry().with_resources(resources)
    validator = Draft202012Validator(
        json.loads((CONTRACTS / case["schema"]).read_bytes()), registry=registry,
    )
    assert validator.is_valid(case["value"]) == case["valid"]


@pytest.mark.parametrize("case", records("proof-cases.json"), ids=lambda case: case["case_id"])
def test_independent_openssl_verification(case: dict[str, Any]) -> None:
    assert accepts_proof(case) == case["valid"]


@pytest.mark.parametrize("case", records("cursor-cases.json"), ids=lambda case: case["case_id"])
def test_transport_epoch_and_revision_fences(case: dict[str, Any]) -> None:
    assert cursor_decision(case) == case["expected"]


@pytest.mark.parametrize("case", records("transfer-cases.json"), ids=lambda case: case["case_id"])
def test_destination_authority_and_replay_cases(case: dict[str, Any]) -> None:
    assert transfer_decision(case["input"]) == case["expected"]
