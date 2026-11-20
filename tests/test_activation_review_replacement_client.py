"""Published same-terms review replacement schema and proof vectors."""
import copy
import json
from dataclasses import replace

import pytest

from app.security.activation_review_replacement import ActivationReviewReplacementClient, replacement_request, prepare_request, contract
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.source_replacement import SourceReplacementRefused, REGISTERED
from test_source_replacement_client import ROOT, NOW, Key, Transport

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]


@pytest.fixture
def review():
    vectors = json.loads((ROOT / "complimentary-activation-review-replacement-v1/cases.json").read_text("utf-8"))
    vector = vectors["proof_cases"][0]
    cases = {case["case_id"]: case["value"] for case in vectors["schema_cases"] if case["valid"]}
    transport = Transport(cases["challenge-response"], cases["prepared"])
    key = Key(REGISTERED, vector)
    return ActivationReviewReplacementClient(transport, key, clock=lambda: NOW), transport, key, vector, cases


def test_review_replacement_published_proof_and_response(review):
    client, transport, key, vector, cases = review
    request = vector["request"]
    assert replacement_request(prepare_request(request), continuation_id=request["source_continuation_id"],
        activation_operation_id=request["activation_operation_id"], successor_source_operation_id=request["successor_source_operation_id"],
        successor_activation_operation_id=request["successor_activation_operation_id"]) == request
    assert client.replace(request, before_send=lambda: None) == cases["prepared"]
    assert transport.calls == [("POST", client.challenge_path, cases["challenge"]),
        ("POST", client.route["path"], cases["replace"])]
    assert key.signed == 1


@pytest.mark.parametrize("change", ["digest", "creator", "source-equals-activation", "successor-equals-original",
    "successors-equal", "extra"])
def test_review_replacement_refuses_changed_request_before_exchange(review, change):
    client, transport, key, vector, _ = review
    request = copy.deepcopy(vector["request"])
    if change == "digest":
        request["source_prepare_digest"] = "0"*64
    elif change == "creator":
        request["scope"]["creator_account_id"] = "another"
    elif change == "source-equals-activation":
        request["activation_operation_id"] = request["source_operation_id"]
    elif change == "successor-equals-original":
        request["successor_activation_operation_id"] = request["source_operation_id"]
    elif change == "successors-equal":
        request["successor_activation_operation_id"] = request["successor_source_operation_id"]
    else:
        request["expected_customer_id"] = "forbidden"
    with pytest.raises(HostedGrantUnavailable):
        client.replace(request, before_send=lambda: None)
    assert not transport.calls and key.signed == 0


@pytest.mark.parametrize("field", ["source_operation_id", "activation_operation_id", "successor_activation_operation_id",
    "successor_prepare_digest", "successor.operation_id", "successor.continuation_id", "successor.expires_at", "successor.reference"])
def test_review_replacement_refuses_mismatched_result(review, field):
    client, transport, _, vector, _ = review
    result = copy.deepcopy(transport.result)
    wrong = "0198a1b2-c3d4-7300-8000-000000000099"
    if field.startswith("successor."):
        name = field.split(".")[1]
        result["successor"][name] = {"operation_id": wrong, "continuation_id": vector["request"]["source_continuation_id"],
            "expires_at": "2025-08-07T00:31:00.000Z", "reference": "invalid"}[name]
    else:
        result[field] = "0"*64 if field == "successor_prepare_digest" else wrong
    transport.result = result
    with pytest.raises(HostedGrantUnavailable):
        client.replace(vector["request"], before_send=lambda: None)


@pytest.mark.parametrize("reason", ["proof_invalid", "original_committed", "review_unavailable", "operation_conflict"])
def test_review_replacement_refusal_is_not_successor(review, reason):
    client, transport, _, vector, cases = review
    transport.result = cases[reason]
    transport.status = contract().profile["refusal_status"][reason]
    with pytest.raises(SourceReplacementRefused) as exc:
        client.replace(vector["request"], before_send=lambda: None)
    assert exc.value.reason == reason


def test_review_replacement_requires_existing_exact_key(review):
    client, transport, key, vector, _ = review
    key.reference = replace(key.reference, installation_key_id="ik1.AAECAwQFBgcICQoLDA0ODw")
    with pytest.raises(HostedGrantUnavailable):
        client.replace(vector["request"], before_send=lambda: None)
    assert not transport.calls


def test_review_replacement_rechecks_guard_before_signing(review):
    client, transport, key, vector, _ = review
    def guard():
        if transport.calls:
            raise HostedGrantUnavailable("Changed")
    with pytest.raises(HostedGrantUnavailable):
        client.replace(vector["request"], before_send=guard)
    assert len(transport.calls) == 1 and key.signed == 0


def test_review_replacement_rejects_duplicate_json_members(review):
    client, transport, _, vector, _ = review
    transport.result = json.dumps(transport.result).replace('"status": "prepared"', '"status": "prepared", "status": "prepared"').encode()
    with pytest.raises(HostedGrantUnavailable):
        client.replace(vector["request"], before_send=lambda: None)
