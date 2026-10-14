"""Authentication proof bytes, closed schemas and request/result correlation."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.security.browser_authentication import (
    CHALLENGE_PATH, PATH_PREFIX, PROFILE, PROOF_PROFILE, BrowserAuthenticationClient,
    BrowserAuthenticationRefused, _contract, binding_digest,
)
from app.security.hosted_grants import HostedGrantUnavailable, TransportResponse
from test_desktop_decision_client import VectorKey

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]
ROOT = Path(__file__).resolve().parents[1] / "contracts/browser-authentication-selection-v1"
NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
CHILD = "0199b234-5678-7000-8000-000000000099"
OTHER_CHILD = "0199b234-5678-7000-8000-000000000098"


def cases(name):
    return json.loads((ROOT / f"{name}-cases.json").read_text("utf-8"))


PROOFS = [value for value in cases("proof") if value["valid"]]


def vector(operation="read", kind="initial"):
    return copy.deepcopy(next(value for value in PROOFS if value["case_id"] == f"{kind}-{operation}"))


def response(value, status=200):
    return TransportResponse(status, json.dumps(value, separators=(",", ":")).encode(), "application/json")


class Transport:
    def __init__(self, item, result=None, status=200):
        self.item, self.result, self.status = item, result, status
        self.calls = []

    def request(self, method, path, *, json_body):
        self.calls.append((method, path, copy.deepcopy(json_body)))
        if path == CHALLENGE_PATH:
            return response({"profile": PROOF_PROFILE, "challenge": self.item["challenge"],
                "request_digest": self.item["request_digest"], "expires_at": "2026-10-12T12:01:00.000Z"})
        if isinstance(self.result, Exception):
            raise self.result
        return response(self.result, self.status)


def client(item, result=None, status=200, **kwargs):
    transport, key = Transport(item, result, status), VectorKey(item)
    kwargs.setdefault("clock", lambda: NOW)
    return BrowserAuthenticationClient(transport, key, **kwargs), transport, key


def projection(request, state="selection-required", *, exact=False):
    attempt = None if state == "selection-required" else {
        "authentication_attempt_id": request["authentication_attempt_id"] or CHILD,
        "provider": request.get("provider", "google"), "mode": "sign-in"}
    return {"profile": PROFILE, "operation_id": request["operation_id"],
        "ceremony_id": request["ceremony_id"], "binding_digest": binding_digest(request["binding"]),
        "expires_at": request["binding"]["source_expires_at"], "state": state,
        "choices": dict.fromkeys(("google", "microsoft-personal", "default"), True),
        "actions": {"select": not exact and state == "selection-required", "cancel": not exact and state == "selected",
            "open_browser": not exact and state == "selected", "switch_account": not exact and state == "admitted"},
        "authentication_attempt": attempt}


def test_published_closed_schema_cases():
    for case in cases("schema"):
        name = case["schema"].rsplit("/", 1)[-1].removesuffix(".schema.json")
        if case["valid"]:
            assert _contract().validate(name, case["value"]) == case["value"], case["case_id"]
        else:
            with pytest.raises(HostedGrantUnavailable):
                _contract().validate(name, case["value"])


@pytest.mark.parametrize("item", PROOFS, ids=lambda value: value["case_id"])
def test_independent_authentication_proof_bytes_match_every_purpose(item):
    subject, transport, key = client(item)
    operation = item["operation"].removeprefix("browser-authentication-")
    assert subject.envelope(operation, item["request"]) == {"request": item["request"],
        "proof": {name: item[name] for name in ("challenge", "signature")}}
    assert key.reopened == key.signed == 1
    assert [call[1] for call in transport.calls] == [CHALLENGE_PATH]


@pytest.mark.parametrize("kind", ["initial", "registered", "activation"])
def test_current_read_has_no_selection_side_effect(kind):
    item = vector(kind=kind)
    result = projection(item["request"])
    subject, transport, _ = client(item, result)
    assert subject.read(item["request"], expires_at=result["expires_at"]) == result
    assert [call[1] for call in transport.calls] == [CHALLENGE_PATH, PATH_PREFIX + "read"]


@pytest.mark.parametrize("field,value", [("operation_id", CHILD), ("ceremony_id", CHILD), ("binding_digest", "a" * 64)])
def test_read_refuses_changed_ceremony_scope(field, value):
    item = vector()
    result = projection(item["request"])
    result[field] = value
    subject, _, _ = client(item, result)
    with pytest.raises(HostedGrantUnavailable):
        subject.read(item["request"])


@pytest.mark.parametrize("operation", ["read-exact", "select", "switch", "cancel"])
def test_exact_command_cannot_admit_a_different_child(operation):
    item = vector(operation)
    result = projection(item["request"], "admitted", exact=operation == "read-exact")
    result["authentication_attempt"]["authentication_attempt_id"] = OTHER_CHILD
    if operation == "switch":
        result["authentication_attempt"]["mode"] = "switch-account"
    subject, _, _ = client(item, result)
    with pytest.raises(HostedGrantUnavailable):
        subject.execute(operation.replace("-exact", ""), item["request"])


@pytest.mark.parametrize("provider,mode", [("microsoft-personal", "sign-in"), ("google", "switch-account")])
def test_new_child_cannot_silently_change_provider_or_mode(provider, mode):
    item = vector("select")
    item["request"]["provider"] = "google"
    result = projection(item["request"], "selected")
    result["authentication_attempt"].update(provider=provider, mode=mode)
    subject, _, _ = client(item)
    with pytest.raises(HostedGrantUnavailable):
        subject.validate_result("select", item["request"], result)


@pytest.mark.parametrize("previous,returned", [(None, None), (CHILD, None), (CHILD, CHILD)])
def test_ordinary_select_can_reuse_current_admission_without_a_new_child(previous, returned):
    item = vector("select")
    request = {**item["request"], "expected_previous_attempt_id": previous}
    result = projection(request, "admitted")
    result["authentication_attempt"] = None if returned is None else {
        "authentication_attempt_id": returned, "provider": "default", "mode": "switch-account"}
    subject, _, _ = client(item)
    subject.validate_result("select", request, result)
    with pytest.raises(HostedGrantUnavailable):
        subject.validate_result("switch", request, result)


@pytest.mark.parametrize("action", ["select", "cancel", "open_browser", "switch_account"])
def test_historical_read_cannot_offer_current_actions(action):
    item = vector("read-exact")
    result = projection(item["request"], "selected" if action in {"cancel", "open_browser"} else "admitted", exact=True)
    result["actions"][action] = True
    subject, _, _ = client(item, result)
    with pytest.raises(HostedGrantUnavailable):
        subject.read(item["request"])


def test_historical_read_preserves_admitted_truth_instead_of_requesting_reauthentication():
    item = vector("read-exact")
    result = projection(item["request"], "admitted", exact=True)
    subject, _, _ = client(item)
    subject.validate_result("read", item["request"], result)
    result["state"] = "reauthentication-required"
    with pytest.raises(HostedGrantUnavailable):
        subject.validate_result("read", item["request"], result)


def test_expired_original_deadline_never_permits_action_but_allows_factual_reconciliation():
    item = vector("read-exact")
    result = projection(item["request"], "expired", exact=True)
    subject, _, _ = client(item, clock=lambda: NOW + timedelta(hours=1))
    subject.validate_result("read", item["request"], result)
    result["state"] = "admitted"
    with pytest.raises(HostedGrantUnavailable):
        subject.validate_result("read", item["request"], result)
    request = {**item["request"], "authentication_attempt_id": None}
    result["actions"]["switch_account"] = True
    with pytest.raises(HostedGrantUnavailable):
        subject.validate_result("read", request, result)


@pytest.mark.parametrize("operation", ["select", "switch", "cancel"])
def test_transport_uncertainty_does_not_retry_or_leak_authentication_details(operation):
    item = vector(operation)
    subject, transport, _ = client(item, OSError("private-authentication-details"))
    with pytest.raises(HostedGrantUnavailable) as refused:
        subject.execute(operation, item["request"])
    assert "private-authentication-details" not in str(refused.value)
    assert [call[1] for call in transport.calls] == [CHALLENGE_PATH, PATH_PREFIX + operation]


@pytest.mark.parametrize("status,reason", [(409, "attempt_conflict"), (409, "provider_unavailable"), (410, "expired")])
def test_closed_refusal_is_distinct_from_unknown_transport_result(status, reason):
    item = vector("select")
    subject, _, _ = client(item, {"profile": PROFILE, "reason": reason}, status)
    with pytest.raises(BrowserAuthenticationRefused) as refused:
        subject.execute("select", item["request"])
    assert refused.value.reason == reason
