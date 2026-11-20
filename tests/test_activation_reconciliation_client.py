"""Exercise the published original-activation reconciliation proof vectors."""
import copy
import json

import pytest

from app.security.activation_reconciliation import ActivationReconciliationClient, ActivationReconciliationRefused, exact_request
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.installation_continuation import PROFILE as SOURCE_PROFILE
from test_source_replacement_client import ROOT, Key, Transport, REGISTERED

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]


@pytest.fixture
def reconciliation():
    vectors = json.loads((ROOT / "complimentary-activation-reconciliation-v1/cases.json").read_text("utf-8"))
    vector = vectors["proof_cases"][0]
    cases = {case["case_id"]: case["value"] for case in vectors["schema_cases"] if case["valid"]}
    transport = Transport(cases["challenge-response"], cases["committed"])
    key = Key(REGISTERED, vector)
    return ActivationReconciliationClient(transport, key), transport, key, vector, cases


@pytest.mark.parametrize("status", ["committed", "expired-uncommitted", "unresolved"])
def test_published_exact_reconciliation_proof(reconciliation, status):
    client, transport, key, vector, cases = reconciliation
    request = vector["request"]
    prepare = {"profile": SOURCE_PROFILE, "operation_id": request["source_operation_id"], "scope": request["scope"]}
    assert exact_request(prepare, continuation_id=request["source_continuation_id"],
        activation_operation_id=request["activation_operation_id"]) == request
    transport.result = cases[status]
    assert client.read(request, before_send=lambda: None) == cases[status]
    assert transport.calls == [("POST", client.challenge_path, cases["challenge"]),
        ("POST", client.route["path"], cases["read"])]
    assert key.signed == 1


@pytest.mark.parametrize("change", ["digest", "scope", "extra"])
def test_changed_source_is_rejected_before_any_exchange(reconciliation, change):
    client, transport, key, vector, _ = reconciliation
    request = copy.deepcopy(vector["request"])
    if change == "digest":
        request["source_prepare_digest"] = "0" * 64
    elif change == "scope":
        request["scope"]["creator_account_id"] = "other"
    else:
        request["actor_id"] = "forbidden"
    with pytest.raises(HostedGrantUnavailable):
        client.read(request, before_send=lambda: None)
    assert not transport.calls and key.signed == 0


@pytest.mark.parametrize("field", ["source_operation_id", "activation_operation_id", "status"])
def test_mismatched_response_cannot_resolve_original(reconciliation, field):
    client, transport, _, vector, _ = reconciliation
    transport.result = {**transport.result, field: "0198a1b2-c3d4-7300-8000-000000000099" if field != "status" else "success"}
    with pytest.raises(HostedGrantUnavailable):
        client.read(vector["request"], before_send=lambda: None)


def test_refusal_is_not_a_terminal_commercial_outcome(reconciliation):
    client, transport, _, vector, cases = reconciliation
    transport.result = cases["proof-invalid"]
    transport.status = 401
    with pytest.raises(ActivationReconciliationRefused, match="refused"):
        client.read(vector["request"], before_send=lambda: None)


def test_authority_change_between_challenge_and_signing_prevents_protected_read(reconciliation):
    client, transport, key, vector, _ = reconciliation
    def guard():
        if transport.calls:
            raise HostedGrantUnavailable("Changed")
    with pytest.raises(HostedGrantUnavailable):
        client.read(vector["request"], before_send=guard)
    assert len(transport.calls) == 1 and key.signed == 0
