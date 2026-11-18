"""Consume the published replacement proof vectors, not generated lookalikes."""
import base64
import copy
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import replace

import pytest

from app.persistence.auth import InstallationKeyReference
from app.security.hosted_grants import HostedGrantUnavailable, TransportResponse
from app.security.installation_continuation import canonical_request
from app.security.installation_key import InstallationProof
from app.security.source_replacement import (
    INITIAL, REGISTERED, SourceReplacementClient, SourceReplacementRefused, original_prepare, replacement_request,
)

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]
ROOT = Path(__file__).resolve().parents[1] / "contracts"
NOW = datetime(2025, 8, 7, tzinfo=timezone.utc)


class Key:
    def __init__(self, kind, vector):
        self.vector = vector
        request = vector["request"]
        kid = request["destination"]["installation_key"]["kid"] if kind == INITIAL else request["scope"]["installation_key_id"]
        jwk = vector["public_key"]
        thumbprint = base64.urlsafe_b64encode(hashlib.sha256(canonical_request(jwk)).digest()).rstrip(b"=").decode()
        self.reference = InstallationKeyReference("test-vector", "published-public-only", "ES256", kid, thumbprint,
            json.dumps({**jwk, "kid": kid}), NOW, NOW)
        self.signed = 0

    def ensure_ready(self):
        pytest.fail("Replacement cannot create an installation key")

    def reopen_existing(self):
        return self.reference

    def sign_challenge(self, message):
        self.signed += 1
        assert message.hex() == self.vector["proof_hex"]
        return InstallationProof(self.reference.installation_key_id, "ES256",
            base64.urlsafe_b64decode(self.vector["proof"]["signature"] + "=="))


class Transport:
    def __init__(self, challenge, result):
        self.challenge, self.result = challenge, result
        self.status = 200
        self.calls = []

    def request(self, method, path, *, json_body):
        self.calls.append((method, path, copy.deepcopy(json_body)))
        if path.endswith("proof-challenges"):
            return TransportResponse(200, json.dumps(self.challenge).encode(), "application/json")
        if isinstance(self.result, Exception):
            raise self.result
        if isinstance(self.result, bytes):
            return TransportResponse(self.status, self.result, "application/json")
        return TransportResponse(self.status, json.dumps(self.result).encode(), "application/json")


@pytest.fixture(params=[INITIAL, REGISTERED])
def fixture(request):
    kind = request.param
    vectors = json.loads((ROOT / f"{kind}-replacement-v1/cases.json").read_text("utf-8"))
    vector = vectors["proof_cases"][0]
    cases = {case["case_id"]: case["value"] for case in vectors["schema_cases"] if case["valid"]}
    transport = Transport(cases["challenge-response"], cases["prepared"])
    key = Key(kind, vector)
    client = SourceReplacementClient(kind, transport, key, clock=lambda: NOW)
    return kind, client, transport, key, vector, cases


def send(fixture, *, guard=lambda: None):
    kind, client, _, _, vector, _ = fixture
    return client.replace(vector["request"], before_send=guard,
        intended_creator_id="creator.test" if kind == INITIAL else None)


def test_source_replacement_matches_published_request_proof_and_response(fixture):
    kind, client, transport, key, vector, cases = fixture
    request = vector["request"]
    assert replacement_request(kind, original_prepare(kind, request), request["successor_operation_id"],
        continuation_id=request.get("predecessor_continuation_id")) == request
    assert send(fixture) == cases["prepared"]
    assert transport.calls == [
        ("POST", client.challenge_path, cases["challenge"]),
        ("POST", client.route["path"], cases["replace"]),
    ]
    assert key.signed == 1


@pytest.mark.parametrize("change", ["digest", "same-operation", "profile", "scope", "unknown"])
def test_invalid_predecessor_fails_before_key_or_transport(fixture, change):
    kind, client, transport, key, vector, _ = fixture
    value = copy.deepcopy(vector["request"])
    if change == "digest":
        value["predecessor_prepare_digest"] = "0" * 64
    elif change == "same-operation":
        value["successor_operation_id"] = value["predecessor_operation_id"]
    elif change == "profile":
        value["profile"] = "unknown"
    elif change == "scope":
        value["destination" if kind == INITIAL else "scope"]["installation_id"] = "another"
    else:
        value["extra"] = True
    with pytest.raises(HostedGrantUnavailable):
        client.replace(value, before_send=lambda: None, intended_creator_id="creator.test")
    assert not transport.calls and key.signed == 0


def test_wrong_existing_key_is_refused_before_transport(fixture):
    _, _, transport, key, _, _ = fixture
    key.reference = replace(key.reference, installation_key_id="ik1.AAECAwQFBgcICQoLDA0ODw")
    with pytest.raises(HostedGrantUnavailable):
        send(fixture)
    assert not transport.calls and key.signed == 0


@pytest.mark.parametrize("field", ["predecessor", "successor", "digest", "expired", "longer", "reference", "specific"])
def test_changed_result_never_adopts_a_successor(fixture, field):
    kind, _, transport, _, vector, _ = fixture
    value = copy.deepcopy(transport.result)
    if field == "predecessor":
        value["predecessor_operation_id"] = vector["request"]["successor_operation_id"]
    elif field == "successor":
        value["successor"]["operation_id"] = vector["request"]["predecessor_operation_id"]
    elif field == "digest":
        value["successor_prepare_digest"] = "0" * 64
    elif field == "expired":
        value["successor"]["expires_at"] = "2025-08-07T00:00:00.000Z"
    elif field == "longer":
        value["successor"]["expires_at"] = "2025-08-07T01:00:00.000Z"
    elif field == "reference":
        value["successor"]["reference"] = "B" * 43
    elif kind == INITIAL:
        value["intended_creator_id"] = "another-creator"
    else:
        value["successor"]["continuation_id"] = vector["request"]["predecessor_continuation_id"]
    transport.result = value
    with pytest.raises(HostedGrantUnavailable):
        send(fixture)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("outcome", ["lost", "duplicate", "unavailable", "wrong-status", "conflict"])
def test_response_loss_or_refusal_never_retries(fixture, outcome):
    kind, _, transport, _, _, cases = fixture
    if outcome == "lost":
        transport.result = OSError("connection lost")
    elif outcome == "duplicate":
        transport.result = b'{"profile":"x","profile":"y"}'
    elif outcome == "unavailable":
        transport.status = 503
        transport.result = {"profile": cases["prepared"]["profile"], "reason": "temporarily_unavailable"}
    else:
        transport.status = 403 if outcome == "wrong-status" else 409
        transport.result = cases["conflict"]
    with pytest.raises(SourceReplacementRefused if outcome == "conflict" else HostedGrantUnavailable):
        send(fixture)
    assert len(transport.calls) == 2


def test_stale_guard_after_challenge_does_not_sign_or_mutate(fixture):
    _, _, transport, key, _, _ = fixture
    def guard():
        if transport.calls:
            raise HostedGrantUnavailable("scope changed")
    with pytest.raises(HostedGrantUnavailable):
        send(fixture, guard=guard)
    assert len(transport.calls) == 1 and key.signed == 0


def test_stale_guard_after_signing_does_not_mutate(fixture):
    _, _, transport, key, _, _ = fixture
    def guard():
        if key.signed:
            raise HostedGrantUnavailable("scope changed")
    with pytest.raises(HostedGrantUnavailable):
        send(fixture, guard=guard)
    assert len(transport.calls) == 1 and key.signed == 1


def test_original_domain_outcomes_remain_distinct_from_successor(fixture):
    kind, _, transport, _, _, cases = fixture
    if kind == INITIAL:
        for name in ("accepted", "committed"):
            transport.result = cases[name]
            assert send(fixture) == cases[name]
            assert "successor" not in transport.result
    else:
        transport.status = 409
        transport.result = cases["commercial"]
        with pytest.raises(SourceReplacementRefused) as refusal:
            send(fixture)
        assert refusal.value.reason == "commercial_reconciliation_required"
