"""Production client against published proof bytes and exact desktop decision scopes."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.persistence.auth import InstallationKeyReference
from app.security.desktop_decision import (
    CHALLENGE_PATH, PATH_PREFIX, PROFILE, PROOF_PROFILE, DesktopDecisionClient,
    DesktopDecisionRefused, _contract, binding_digest, decision_digest,
)
from app.security.hosted_grants import HostedGrantUnavailable, TransportResponse
from app.security.installation_continuation import _document, canonical_request
from app.security.installation_key import InstallationProof

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]
ROOT = Path(__file__).resolve().parents[1] / "contracts/desktop-decision-v1"
NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def cases(name):
    return json.loads((ROOT / f"{name}-cases.json").read_text("utf-8"))


PROOFS = [value for value in cases("proof") if value["valid"]]


def vector(operation="read", kind="initial"):
    return copy.deepcopy(next(value for value in PROOFS if value["case_id"] == f"{kind}-{operation}"))


def response(value, status=200):
    return TransportResponse(status, json.dumps(value, separators=(",", ":")).encode(), "application/json")


class VectorKey:
    def __init__(self, item):
        self.vector = item
        binding = item["request"]["binding"]
        self.reference = InstallationKeyReference(
            "test-vector", "published-public-only", "ES256", binding["installation_key_id"],
            base64.urlsafe_b64encode(hashlib.sha256(canonical_request(item["public_key"])).digest()).rstrip(b"=").decode(),
            json.dumps({**item["public_key"], "kid": binding["installation_key_id"]}), NOW, NOW,
        )
        self.reopened = self.signed = 0

    def ensure_ready(self):
        pytest.fail("Desktop consent attempted to create a key")

    def reopen_existing(self):
        self.reopened += 1
        return self.reference

    def sign_challenge(self, message):
        self.signed += 1
        assert message.hex() == self.vector["proof_bytes_hex"]
        return InstallationProof(self.reference.installation_key_id, "ES256",
                                 base64.urlsafe_b64decode(self.vector["signature"] + "=="))


class Transport:
    def __init__(self, item, result=None, status=200):
        self.vector, self.result, self.status = item, result, status
        self.calls = []
        self.challenge_changes = {}

    def request(self, method, path, *, json_body):
        self.calls.append((method, path, copy.deepcopy(json_body)))
        if path == CHALLENGE_PATH:
            return response({"profile": PROOF_PROFILE, "challenge": self.vector["challenge"],
                             "request_digest": self.vector["request_digest"],
                             "expires_at": "2026-10-12T12:01:00.000Z", **self.challenge_changes})
        if isinstance(self.result, Exception):
            raise self.result
        if isinstance(self.result, TransportResponse):
            return self.result
        return response(self.result, self.status)


def client(item, result=None, status=200, **kwargs):
    transport, key = Transport(item, result, status), VectorKey(item)
    kwargs.setdefault("clock", lambda: NOW)
    return DesktopDecisionClient(transport, key, **kwargs), transport, key


def found(request, state="confirmation-required"):
    result = {"profile": PROFILE, "operation_id": request["operation_id"],
              "ceremony_id": request["ceremony_id"], "binding_digest": binding_digest(request["binding"]),
              "state": state}
    if state == "confirmation-required":
        kind = "activation" if request["binding"]["purpose"] == "activate-complimentary" else "connect"
        decision = copy.deepcopy(next(case["value"] for case in cases("schema")
                                      if case["case_id"] == kind + "-decision"))
        result.update(decision=decision, decision_digest=decision_digest(decision),
                      decision_reference=request["binding"]["source"]["reference"])
    return result


def test_all_published_closed_schema_cases():
    for case in cases("schema"):
        name = case["schema"].rsplit("/", 1)[-1].removesuffix(".schema.json")
        if case["valid"]:
            assert _contract().validate(name, case["value"]) == case["value"], case["case_id"]
        else:
            with pytest.raises(HostedGrantUnavailable):
                _contract().validate(name, case["value"])


@pytest.mark.parametrize("item", PROOFS, ids=lambda value: value["case_id"])
def test_independent_domain_matches_published_proof_bytes(item):
    subject, transport, key = client(item)
    operation = item["operation"].removeprefix("desktop-decision-")
    assert subject.envelope(operation, item["request"]) == {
        "request": item["request"], "proof": {name: item[name] for name in ("challenge", "signature")}}
    assert key.reopened == key.signed == 1
    assert [call[1] for call in transport.calls] == [CHALLENGE_PATH]


@pytest.mark.parametrize("kind", ["initial", "registered", "activation"])
def test_read_displays_exact_existing_scope_without_submit(kind):
    item = vector(kind=kind)
    expected = found(item["request"])
    subject, transport, _ = client(item, expected)
    assert subject.read(item["request"], expires_at="2026-10-12T12:10:00.000Z") == expected
    assert [call[1] for call in transport.calls] == [CHALLENGE_PATH, PATH_PREFIX + "read"]


@pytest.mark.parametrize("field", ["installation_key_id", "installation_key_jkt"])
def test_changed_key_refuses_before_network(field):
    item = vector()
    subject, transport, key = client(item)
    key.reference = replace(key.reference, **{field: "changed"})
    with pytest.raises(HostedGrantUnavailable, match="key changed"):
        subject.read(item["request"])
    assert not transport.calls and not key.signed


@pytest.mark.parametrize("field,value", [
    ("operation_id", "0199b234-5678-7000-8000-000000000009"),
    ("ceremony_id", "0199b234-5678-7000-8000-000000000009"),
    ("binding_digest", "a" * 64),
])
@pytest.mark.parametrize("state", ["confirmation-required", "accepted", "unavailable"])
def test_every_result_correlates_exact_original_operation(field, value, state):
    item = vector()
    result = found(item["request"], state)
    result[field] = value
    subject, _, _ = client(item, result)
    with pytest.raises(HostedGrantUnavailable):
        subject.read(item["request"])


@pytest.mark.parametrize("change", ["creator", "digest", "reference", "purpose", "term-order"])
def test_display_cannot_substitute_target_action_or_canonical_terms(change):
    item = vector(kind="activation" if change in {"purpose", "term-order"} else "initial")
    result = found(item["request"])
    if change == "creator":
        result["decision"]["creator_id"] = "other-creator"
        result["decision_digest"] = decision_digest(result["decision"])
    elif change == "digest":
        result["decision_digest"] = "0" * 64
    elif change == "reference":
        result["decision_reference"] = "A" * 42 + "B"
    elif change == "purpose":
        result["decision"] = found(vector()["request"])["decision"]
        result["decision_digest"] = decision_digest(result["decision"])
    else:
        result["decision"]["offer"]["fallback_major_versions"] = [2, 1]
        result["decision_digest"] = decision_digest(result["decision"])
    subject, _, _ = client(item, result)
    with pytest.raises(HostedGrantUnavailable):
        subject.read(item["request"])


@pytest.mark.parametrize("expiry,upper", [
    ("2026-10-12T12:00:00.000Z", None),
    ("2026-10-12T12:11:00.000Z", None),
    ("2026-10-12T12:10:00.000Z", "2026-10-12T12:09:00.000Z"),
])
def test_display_expiry_is_live_and_within_original_source_and_ceremony(expiry, upper):
    item = vector()
    result = found(item["request"])
    result["decision"]["expires_at"] = expiry
    result["decision_digest"] = decision_digest(result["decision"])
    subject, _, _ = client(item, result)
    with pytest.raises(HostedGrantUnavailable, match="deadline changed"):
        subject.read(item["request"], expires_at=upper)


def test_shorter_fixed_session_or_quote_deadline_is_valid():
    item = vector(kind="activation")
    result = found(item["request"])
    result["decision"]["expires_at"] = "2026-10-12T12:01:00.000Z"
    result["decision_digest"] = decision_digest(result["decision"])
    subject, _, _ = client(item, result)
    assert subject.read(item["request"], expires_at="2026-10-12T12:10:00.000Z") == result


def test_accepted_submit_can_reconcile_after_original_deadline_without_minting():
    item = vector("submit", "activation")
    result = found(item["request"], "accepted")
    subject, transport, _ = client(item, result, clock=lambda: NOW + timedelta(hours=1))
    assert subject.submit(item["request"], expires_at="2026-10-12T12:10:00.000Z") == result
    assert len(transport.calls) == 2
    assert set(result) == {"profile", "operation_id", "ceremony_id", "binding_digest", "state"}


def test_lost_submit_does_not_retry_read_or_replace_consent():
    item = vector("submit", "activation")
    subject, transport, _ = client(item, OSError("reply lost"))
    with pytest.raises(HostedGrantUnavailable):
        subject.submit(item["request"])
    assert [call[1] for call in transport.calls] == [CHALLENGE_PATH, PATH_PREFIX + "submit"]


def test_cancellation_fence_runs_before_final_submit():
    item = vector("submit")
    subject, transport, key = client(item, found(item["request"], "accepted"))

    def fence():
        if key.signed:
            raise RuntimeError("attempt retired")

    with pytest.raises(RuntimeError, match="attempt retired"):
        subject.submit(item["request"], before_send=fence)
    assert len(transport.calls) == 1


def test_elapsed_challenge_budget_exhausted_before_signing():
    ticks = iter([0.0, 60.0])
    subject, transport, key = client(vector(), monotonic=lambda: next(ticks))
    with pytest.raises(HostedGrantUnavailable, match="budget exhausted"):
        subject.envelope("read", vector()["request"])
    assert len(transport.calls) == 1 and not key.signed


def test_challenge_digest_mismatch_stops_before_signing():
    subject, transport, key = client(vector())
    transport.challenge_changes["request_digest"] = "0" * 64
    with pytest.raises(HostedGrantUnavailable, match="challenge changed"):
        subject.read(vector()["request"])
    assert len(transport.calls) == 1 and not key.signed


@pytest.mark.parametrize("reason,status", [("decision_conflict", 409), ("authentication_required", 401)])
def test_only_closed_consistent_refusal_is_definitive(reason, status):
    item = vector("submit")
    body = {"profile": PROFILE, "reason": reason}
    subject, _, _ = client(item, body, status)
    with pytest.raises(DesktopDecisionRefused) as refused:
        subject.submit(item["request"])
    assert refused.value.reason == reason
    subject, _, _ = client(item, body, 403)
    with pytest.raises(HostedGrantUnavailable):
        subject.submit(item["request"])


@pytest.mark.parametrize("reply", [
    TransportResponse(200, b'{"profile":"x","profile":"y"}', "application/json"),
    TransportResponse(200, b"{}", "text/html"),
    TransportResponse(200, b" " * 16385, "application/json"),
])
def test_ambiguous_oversized_or_wrong_type_response_is_unavailable(reply):
    item = vector()
    subject, _, _ = client(item, reply)
    with pytest.raises(HostedGrantUnavailable):
        subject.read(item["request"])


def test_published_parser_negatives_are_rejected():
    for case in cases("parsing"):
        if not case["valid"]:
            with pytest.raises((HostedGrantUnavailable, UnicodeError)):
                _document(case["document"].encode(), limit=case["limit"])
