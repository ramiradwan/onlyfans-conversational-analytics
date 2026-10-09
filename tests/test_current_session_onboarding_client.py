"""Current-session v2 selection preserves the published version boundary."""

import base64
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.security.hosted_grants import HostedGrantUnavailable, TransportResponse
from app.security.initial_handoff import (
    CHALLENGE_PATH, PATH_PREFIX, PROFILE, PROOF_PROFILE, InitialHandoffClient,
    InitialHandoffRefused, canonical, validate_contract, validate_wait_event,
)

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]
CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
FAMILY = CONTRACTS / "initial-installation-handoff-v2"
SCHEMAS = json.loads((FAMILY / "schema-cases.json").read_text())
PROOFS = [case for case in json.loads((FAMILY / "proof-cases.json").read_text()) if case["valid"]]


@pytest.mark.parametrize("case", SCHEMAS, ids=lambda case: case["case_id"])
def test_current_initial_client_validates_published_v2_shapes(case):
    name = case["schema"].rsplit("/", 1)[-1].removesuffix(".schema.json")
    if case["valid"]:
        assert validate_contract(name, case["value"]) == case["value"]
    else:
        with pytest.raises(HostedGrantUnavailable):
            validate_contract(name, case["value"])


@pytest.mark.parametrize("vector", PROOFS, ids=lambda case: case["case_id"])
def test_initial_production_proof_uses_only_the_exact_published_v2_domain_and_route(vector):
    calls = []
    key_id = "ik1.AAECAwQFBgcICQoLDA0ODw"
    thumbprint = base64.urlsafe_b64encode(hashlib.sha256(canonical(vector["public_key"])).digest()).rstrip(b"=").decode()

    def sign(message):
        assert message.hex() == vector["proof_bytes_hex"]
        return SimpleNamespace(installation_key_id=key_id, algorithm="ES256",
            signature=base64.urlsafe_b64decode(vector["signature"] + "=="))

    def request(method, path, *, json_body):
        calls.append((method, path, json_body))
        assert path == CHALLENGE_PATH == "/v2/onboarding/installation-handoff-proof-challenges"
        return TransportResponse(200, json.dumps({"profile": PROOF_PROFILE,
            "challenge": vector["challenge"], "request_digest": hashlib.sha256(canonical(vector["request"])).hexdigest(),
            "expires_at": "2026-10-09T12:01:00.000Z"}).encode(), "application/json")

    key = SimpleNamespace(ensure_ready=lambda: SimpleNamespace(installation_key_id=key_id,
        installation_key_jkt=thumbprint), sign_challenge=sign)
    client = InitialHandoffClient(SimpleNamespace(request=request), key)
    envelope = client.envelope(vector["operation"].removeprefix("initial-handoff-"), vector["request"])
    assert envelope["request"]["profile"] == PROFILE
    assert envelope["proof"]["signature"] == vector["signature"]
    assert len(calls) == 1
    assert PATH_PREFIX == "/v2/onboarding/installation-handoffs:"


def test_a_v1_initial_request_is_refused_before_key_access_or_transport():
    request = copy.deepcopy(PROOFS[0]["request"])
    request["profile"] = "urn:bridge-clean:initial-installation-handoff:v1"
    client = InitialHandoffClient(SimpleNamespace(request=lambda *a, **k: pytest.fail("Transport must not run")),
        SimpleNamespace(ensure_ready=lambda: pytest.fail("Key must not be accessed")))
    with pytest.raises(HostedGrantUnavailable):
        client.envelope("prepare", request)


@pytest.mark.parametrize("state", ["authentication-required", "confirmation-required"])
def test_current_session_and_action_confirmation_states_are_distinct(state):
    value = {"profile": PROFILE, "status": state, "revision": 3}
    assert validate_wait_event(value) == value
    with pytest.raises(HostedGrantUnavailable):
        validate_wait_event({**value, "status": "reauthentication-required"})
    with pytest.raises(HostedGrantUnavailable):
        validate_wait_event({**value, "profile": "urn:bridge-clean:initial-installation-handoff:v1"})


@pytest.mark.parametrize("reason,status,expected", [
    ("authentication_required", 401, "authentication_required"),
    ("confirmation_required", 409, "confirmation_required"),
    ("proof_invalid", 401, "handoff_refused"),
    ("confirmation_required", 401, "handoff_refused"),
])
def test_only_exact_current_session_refusals_explain_required_action(reason, status, expected):
    transport = SimpleNamespace(request=lambda *a, **k: TransportResponse(status,
        json.dumps({"profile": PROFILE, "reason": reason}).encode(), "application/json"))
    client = InitialHandoffClient(transport, object())
    with pytest.raises(InitialHandoffRefused) as error:
        client._request(PATH_PREFIX + "complete", {}, expected=200)
    assert error.value.code == expected


def test_unsigned_unstructured_http_401_does_not_become_an_authentication_fact():
    transport = SimpleNamespace(request=lambda *a, **k: TransportResponse(401, b'{"reason":"login"}', "application/json"))
    with pytest.raises(HostedGrantUnavailable):
        InitialHandoffClient(transport, object())._request(PATH_PREFIX + "complete", {}, expected=200)
