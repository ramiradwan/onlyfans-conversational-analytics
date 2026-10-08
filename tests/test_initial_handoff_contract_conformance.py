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

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
VECTORS = CONTRACTS / "initial-installation-handoff-v1"
pytestmark = [pytest.mark.ci_tier("fast")]

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
            "initial-handoff-" + name: "/v1/onboarding/installation-handoffs:" + name
            for name in ("prepare", "wait", "complete", "receipt")
        }[record["operation"]]
        if len(decode(key["x"])) != 32 or len(decode(key["y"])) != 32:
            return False
        challenge = decode(record["challenge"])
        if len(challenge) != 32:
            return False
        pieces = (
            challenge,
            b"POST",
            route.encode(),
            hashlib.sha256(canonical(record["request"])).digest(),
            record["operation"].encode(),
            hashlib.sha256(canonical(key)).digest(),
            b"urn:bridge-clean:commercial-control-plane:initial-installation-handoff",
        )
        message = b"BRIDGE-CLEAN-INITIAL-INSTALLATION-HANDOFF-PROOF-V1\0" + b"".join(
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
            int.from_bytes(decode(key["y"]), "big"),
            ec.SECP256R1(),
        ).public_key()
        public.verify(utils.encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def scope_digests_match(record: dict[str, Any]) -> bool:
    scope = record["scope"]
    common = {
        name: scope[name]
        for name in (
            "organization_id",
            "onboarding_transaction_id",
            "installation_id",
            "installation_key_jkt",
            "intended_creator_id",
        )
    }
    action_values = {
        "issue-installation-claim": {
            **common,
            "profile": "urn:bridge-clean:installation-claim:v2",
            "device": record["destination"]["device"],
        },
        "approve-creator-association": {
            **common,
            "association_request_id": scope["association_request_id"],
        },
    }
    calculated = {
        name: hashlib.sha256(canonical({**value, "action": name})).hexdigest()
        for name, value in action_values.items()
    }
    return bool(scope["action_digests"] == calculated)


def admission_outcomes(record: dict[str, Any]) -> dict[str, Any]:
    state = record["context"].copy()
    receipt: tuple[str, str, int] | None = None
    challenges: set[str] = set()
    outcomes: list[str] = []
    bootstrap_count = 0

    def evaluate(attempt: dict[str, Any]) -> str:
        nonlocal receipt, bootstrap_count
        state.update(attempt.get("set", {}))
        operation = attempt["kind"]
        instant = state["now"]
        if operation == "legacy-consume":
            return "not_enrolled" if receipt is None else "409-installation-claim-already-consumed"
        if not all(state[name] for name in ("current_authority", "principal_matches")):
            return "authorization_changed"
        checks = (
            attempt["proof_valid"],
            attempt["key_matches"],
            attempt["challenge_admitted"],
            attempt["admitted_key_matches"],
            attempt["challenge"] not in challenges,
            instant < attempt["proof_expires"],
            attempt["admitted_digest"] == attempt["request_digest"],
            attempt["purpose"]
            == ("bootstrap-recovery" if operation == "recover" else "initial-handoff-" + operation),
            operation != "complete"
            or attempt["admitted_revision"] == attempt["authorization_revision"],
        )
        if not all(checks):
            return "proof_invalid"
        challenges.add(attempt["challenge"])
        if not state["key_matches"]:
            return "wrong_destination"
        if not state["scope_matches"]:
            return "authorization_changed"
        if operation in {"receipt", "recover"}:
            if receipt is None:
                return "not_enrolled"
            if operation == "receipt":
                return (
                    "recovery-required"
                    if attempt["operation_id"] == receipt[0]
                    else "operation_conflict"
                )
            bootstrap_count += 1
            return "recovered"
        if receipt is not None:
            if instant - receipt[2] >= 1800:
                return "expired"
            exact_retry = (attempt["operation_id"], attempt["request_digest"]) == receipt[:2]
            return (
                "operation_conflict"
                if operation == "complete" and not exact_retry
                else "recovery-required"
            )
        if instant - state["prepared_at"] >= 600:
            return "expired"
        if not state["authorized"]:
            return "waiting" if operation == "wait" else "authorization_changed"
        authenticated = state["auth_time"]
        if authenticated is None or authenticated > instant or instant - authenticated > 300:
            return "reauthentication_required"
        policy_checks = (
            state["digests_match"],
            state["both_digests"],
            state["confirmation_unused"],
            state["confirmation_matches"],
            instant < state["confirmation_expires"],
            state["authorization_revision"] == attempt["authorization_revision"],
        )
        if not all(policy_checks):
            return "authorization_changed"
        if operation == "wait":
            return "authorized"
        if state["existing_installation"]:
            return "already_enrolled"
        if state["transient_failure"]:
            return "temporarily_unavailable"
        receipt = (attempt["operation_id"], attempt["request_digest"], instant)
        state["confirmation_unused"] = False
        bootstrap_count += 1
        return "completed"

    outcomes.extend(evaluate(attempt) for attempt in record["steps"])
    enrolled = 0 if receipt is None else 1
    return {
        "results": outcomes,
        "enrollments": enrolled,
        "claim_consumptions": enrolled,
        "issue_confirmations_consumed": enrolled,
        "creator_confirmations_consumed": 0,
        "bootstrap_issuances": bootstrap_count,
        "commercial_issuances": 0,
    }


def lifecycle_outcomes(record: dict[str, Any]) -> dict[str, Any]:
    prepared: tuple[str, str, str] | None = None
    reservations: dict[str, str] = {}
    results = []
    for item in record["steps"]:
        if item["kind"] == "prepare":
            exact = (item["operation"], item["destination"], item["digest"])
            if not item["proof_valid"]:
                outcome = "proof_invalid"
            elif prepared is not None and prepared != exact:
                outcome = "operation_conflict"
            else:
                prepared, outcome = exact, "prepared"
        elif prepared is None:
            outcome = "invalid"
        elif item["destination"] != prepared[1]:
            outcome = "wrong_destination"
        elif not (item["current_scoped_auth"] and item["confirmation_unused"]):
            outcome = "authorization_changed"
        elif item["operation"] in reservations:
            outcome = (
                "authorized"
                if reservations[item["operation"]] == item["digest"]
                else "operation_conflict"
            )
        else:
            reservations[item["operation"]] = item["digest"]
            outcome = "authorized"
        results.append(outcome)
    return {
        "results": results,
        "authorization_revision": len(reservations),
        "issue_confirmations_consumed": 0,
        "creator_confirmations_consumed": 0,
        "enrollments": 0,
    }


def test_initial_handoff_exact_manifest_closure() -> None:
    manifest = json.loads((VECTORS / "manifest.json").read_bytes())
    assert {path.name for path in VECTORS.iterdir()} == {
        "manifest.json",
        *(entry["path"] for entry in manifest["files"]),
    }
    for entry in manifest["files"]:
        content = (VECTORS / entry["path"]).read_bytes()
        assert len(content) == entry["size"]
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
    assert sum(len(records(entry["path"])) for entry in manifest["files"]) == 125


@pytest.mark.parametrize("case", records("schema-cases.json"), ids=lambda case: case["case_id"])
def test_independent_initial_handoff_schema(case: dict[str, Any]) -> None:
    resources = []
    for path in (CONTRACTS / "schemas").rglob("*.schema.json"):
        value = json.loads(path.read_bytes())
        resources.append((value["$id"], Resource.from_contents(value)))
    registry: Registry[Any] = Registry().with_resources(resources)
    schema = json.loads((CONTRACTS / case["schema"]).read_bytes())
    assert Draft202012Validator(schema, registry=registry).is_valid(case["value"]) == case["valid"]


@pytest.mark.parametrize("case", records("proof-cases.json"), ids=lambda case: case["case_id"])
def test_independent_openssl_initial_handoff_proof(case: dict[str, Any]) -> None:
    assert accepts_proof(case) == case["valid"]


@pytest.mark.parametrize("case", records("admission-cases.json"), ids=lambda case: case["case_id"])
def test_independent_initial_handoff_admission(case: dict[str, Any]) -> None:
    assert admission_outcomes(case) == case["expected"]


@pytest.mark.parametrize("case", records("digest-cases.json"), ids=lambda case: case["case_id"])
def test_independent_initial_handoff_auth_digests(case: dict[str, Any]) -> None:
    assert scope_digests_match(case) == case["valid"]


@pytest.mark.parametrize("case", records("lifecycle-cases.json"), ids=lambda case: case["case_id"])
def test_independent_initial_handoff_reservations(case: dict[str, Any]) -> None:
    assert lifecycle_outcomes(case) == case["expected"]


def test_private_jwk_member_is_refused_in_memory() -> None:
    case = records("proof-cases.json")[2]
    case["public_key"]["d"] = "not-private-material"
    assert not accepts_proof(case)
