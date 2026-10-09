"""Proof construction uses elapsed time; server validity remains authoritative."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.security.hosted_grants import HostedGrantUnavailable, TransportResponse
from app.security.initial_handoff import InitialHandoffClient, canonical, validate_wait_event
from app.security.onboarding_continuity import HostedOnboardingContinuity

pytestmark = [pytest.mark.ci_tier("fast")]
CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
INSTANT = datetime(2026, 10, 9, 10, 39, 26, tzinfo=timezone.utc)


class ProofCase:
    def __init__(self, family, *, wall_offset=0, response_delay=0, signing_delay=0, change=None,
                 operation="complete", preparation_change=None):
        initial = family == "initial"
        directory = "initial-installation-handoff-v1" if initial else "onboarding-continuity-v1"
        records = json.loads((CONTRACTS / directory / "proof-cases.json").read_bytes())
        self.vector = next(value for value in records if value["case_id"] ==
                           (f"{operation}-valid" if initial else "hosted-stream-valid"))
        self.request = copy.deepcopy(self.vector["request"])
        self.elapsed = 0.0
        self.requests = []
        self.signed = []
        self.key_id = "ik1.AAECAwQFBgcICQoLDA0ODw"
        thumbprint = base64.urlsafe_b64encode(hashlib.sha256(canonical(self.vector["public_key"])).digest()).rstrip(b"=").decode()

        def sign(message):
            self.signed.append(message)
            assert message.hex() == self.vector["proof_bytes_hex"]
            self.elapsed += signing_delay
            return SimpleNamespace(installation_key_id=self.key_id, algorithm="ES256",
                                   signature=base64.urlsafe_b64decode(self.vector["signature"] + "=="))

        def request(method, path, *, json_body):
            self.requests.append((method, path))
            if initial and path == "/v1/onboarding/installation-handoffs:prepare":
                assert json_body["request"] == self.request
                value = {"profile": "urn:bridge-clean:initial-installation-handoff:v1",
                         "reference": "A" * 43, "operation_id": self.request["operation_id"],
                         "expires_at": (INSTANT + timedelta(seconds=600)).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
                if preparation_change is not None:
                    preparation_change(value)
                return TransportResponse(201, json.dumps(value).encode(), "application/json")
            assert path == ("/v1/onboarding/installation-handoff-proof-challenges" if initial
                            else "/v1/onboarding/stream-proof-challenges")
            assert json_body["target"]["request"] == self.request
            self.elapsed += response_delay
            value = {"profile": ("urn:bridge-clean:initial-installation-handoff-proof:v1" if initial
                                  else "urn:bridge-clean:onboarding-proof:v1"),
                     "challenge": self.vector["challenge"],
                     "request_digest": hashlib.sha256(canonical(self.request)).hexdigest(),
                     "expires_at": (INSTANT + timedelta(seconds=60)).isoformat(timespec="milliseconds").replace("+00:00", "Z")}
            if change is not None:
                change(value)
            return TransportResponse(200, json.dumps(value).encode(), "application/json")

        self.key = SimpleNamespace(ensure_ready=lambda: SimpleNamespace(
            installation_key_id=self.key_id, installation_key_jkt=thumbprint), sign_challenge=sign)
        client_type = InitialHandoffClient if initial else HostedOnboardingContinuity
        self.client = client_type(SimpleNamespace(request=request), self.key,
                                  clock=lambda: INSTANT + timedelta(seconds=wall_offset),
                                  monotonic=lambda: self.elapsed)
        self.initial = initial
        self.operation = operation

    def envelope(self):
        if self.initial:
            return self.client.envelope(self.operation, self.request)
        return self.client.envelope(self.request)


@pytest.mark.parametrize("family", ["initial", "registered"])
@pytest.mark.parametrize("offset", [-120, -0.05, 0, 0.05, 120])
def test_prompt_proof_construction_is_independent_of_wall_clock_offset(family, offset):
    case = ProofCase(family, wall_offset=offset, response_delay=0.001, signing_delay=0.001)
    result = case.envelope()
    assert result["request"] == case.request
    assert result["proof"]["signature"] == case.vector["signature"]
    assert len(case.requests) == len(case.signed) == 1


@pytest.mark.parametrize("family", ["initial", "registered"])
@pytest.mark.parametrize("delay", [59.999, 60.0, 60.001])
def test_challenge_response_consumes_the_original_budget(family, delay):
    case = ProofCase(family, response_delay=delay)
    if delay < 60:
        case.envelope()
        assert len(case.signed) == 1
    else:
        with pytest.raises(HostedGrantUnavailable, match="budget"):
            case.envelope()
        assert case.signed == []
    assert len(case.requests) == 1


@pytest.mark.parametrize("family", ["initial", "registered"])
@pytest.mark.parametrize("delay", [58.999, 59.0, 60.0])
def test_signing_cannot_restart_or_overrun_the_budget(family, delay):
    case = ProofCase(family, response_delay=1, signing_delay=delay)
    if 1 + delay < 60:
        case.envelope()
    else:
        with pytest.raises(HostedGrantUnavailable, match="budget"):
            case.envelope()
    assert len(case.requests) == len(case.signed) == 1


@pytest.mark.parametrize("family", ["initial", "registered"])
@pytest.mark.parametrize("boundary", ["response", "signing"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_nonfinite_or_backward_elapsed_time_is_refused(family, boundary, value):
    case = ProofCase(family, response_delay=value if boundary == "response" else 0,
                     signing_delay=value if boundary == "signing" else 0)
    with pytest.raises(HostedGrantUnavailable, match="budget"):
        case.envelope()
    assert len(case.requests) == 1
    assert len(case.signed) == (1 if boundary == "signing" else 0)


@pytest.mark.parametrize("family", ["initial", "registered"])
@pytest.mark.parametrize("field,value", [
    ("profile", "urn:wrong"), ("request_digest", "0" * 64),
    ("expires_at", "2026-10-09T10:40:26Z"),
    ("expires_at", "2026-02-30T10:40:26.000Z"), ("expires_at", 60),
    ("challenge", "not-canonical"), ("unexpected", "closed-object"),
])
def test_clock_independence_preserves_strict_challenge_validation(family, field, value):
    case = ProofCase(family, change=lambda reply: reply.update({field: value}))
    with pytest.raises(HostedGrantUnavailable):
        case.envelope()
    assert len(case.requests) == 1 and case.signed == []


@pytest.mark.parametrize("field,value", [
    ("installation_id", "invalid scope"), ("authorization_revision", 0),
    ("installation_key_jkt", "not-a-thumbprint"), ("unexpected", "closed-object"),
])
def test_authorized_event_scope_validation_is_unchanged(field, value):
    records = json.loads((CONTRACTS / "initial-installation-handoff-v1" / "schema-cases.json").read_bytes())
    event = copy.deepcopy(next(item["value"] for item in records if item["case_id"] == "wait-event-valid"))
    event["scope"][field] = value
    with pytest.raises(HostedGrantUnavailable):
        validate_wait_event(event)


@pytest.mark.parametrize("offset", [-0.05, 0, 0.05])
def test_valid_preparation_allows_small_wall_clock_offsets(offset):
    case = ProofCase("initial", operation="prepare", wall_offset=offset)
    result = case.client.prepare(case.request)
    assert result["operation_id"] == case.request["operation_id"]
    assert result["reference"] == "A" * 43
    assert len(case.requests) == 2 and len(case.signed) == 1


@pytest.mark.parametrize("offset", [-0.001, 0])
def test_expired_preparation_response_remains_refused(offset):
    expiry = (INSTANT + timedelta(seconds=offset)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    case = ProofCase("initial", operation="prepare",
                     preparation_change=lambda value: value.update(expires_at=expiry))
    with pytest.raises(HostedGrantUnavailable, match="^Initial preparation expired$"):
        case.client.prepare(case.request)
    assert len(case.requests) == 2 and len(case.signed) == 1


@pytest.mark.parametrize("field,value", [
    ("profile", "urn:wrong"), ("operation_id", "different-operation"),
    ("reference", "A" * 42), ("reference", "A" * 42 + "B"),
    ("expires_at", "2026-10-09T10:49:26Z"),
    ("expires_at", "2026-02-30T10:49:26.000Z"), ("expires_at", 600),
    ("unexpected", "closed-object"),
])
def test_preparation_validation_stays_bound_to_its_response(field, value):
    case = ProofCase("initial", operation="prepare", wall_offset=-0.05,
                     preparation_change=lambda reply: reply.update({field: value}))
    with pytest.raises(HostedGrantUnavailable):
        case.client.prepare(case.request)
    assert len(case.requests) == 2 and len(case.signed) == 1
