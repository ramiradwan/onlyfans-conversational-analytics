"""Local proof construction and result correlation against the published contract."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.persistence.auth import InstallationKeyReference
from app.security.browser_ceremony import (
    CHALLENGE_PATH, ENTRY_PREFIX, PATH_PREFIX, PROFILE, PROOF_PROFILE,
    BrowserCeremonyClient, BrowserCeremonyRefused, _contract,
    binding_digest, browser_entry_url, expected_completion,
)
from app.security.hosted_grants import HostedGrantUnavailable, TransportResponse
from app.security.installation_continuation import _document, canonical_request
from app.security.installation_key import InstallationProof

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]
ROOT = Path(__file__).resolve().parents[1] / "contracts/browser-ceremony-v1"
NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def cases(name):
    return json.loads((ROOT / f"{name}-cases.json").read_text("utf-8"))


PROOFS = [value for value in cases("proof") if value["valid"]]


def response(value, status=200):
    return TransportResponse(status, json.dumps(value, separators=(",", ":")).encode(), "application/json")


class VectorKey:
    def __init__(self, vector):
        self.vector = vector
        binding = vector["request"]["binding"]
        self.reference = InstallationKeyReference(
            "test-vector", "published-public-only", "ES256", binding["installation_key_id"],
            base64.urlsafe_b64encode(hashlib.sha256(canonical_request(vector["public_key"])).digest()).rstrip(b"=").decode(),
            json.dumps({**vector["public_key"], "kid": binding["installation_key_id"]}), NOW, NOW,
        )
        self.reopened = self.signed = 0

    def ensure_ready(self):
        pytest.fail("A browser ceremony attempted to create an installation key")

    def reopen_existing(self):
        self.reopened += 1
        return self.reference

    def sign_challenge(self, message):
        self.signed += 1
        assert message.hex() == self.vector["proof_bytes_hex"]
        return InstallationProof(self.reference.installation_key_id, "ES256",
                                 base64.urlsafe_b64decode(self.vector["signature"] + "=="))


class Transport:
    def __init__(self, vector, result=None, status=200):
        self.vector, self.result, self.status = vector, result, status
        self.calls = []

    def request(self, method, path, *, json_body):
        self.calls.append((method, path, copy.deepcopy(json_body)))
        if path == CHALLENGE_PATH:
            return response({"profile": PROOF_PROFILE, "challenge": self.vector["challenge"],
                             "request_digest": self.vector["request_digest"], "expires_at": "2026-10-11T12:01:00.000Z"})
        if isinstance(self.result, Exception):
            raise self.result
        return response(self.result, self.status)


def client(vector, result=None, status=200, **kwargs):
    transport, key = Transport(vector, result, status), VectorKey(vector)
    return BrowserCeremonyClient(transport, key, clock=lambda: NOW, **kwargs), transport, key


def vector(operation="read", kind="initial"):
    return next(value for value in PROOFS if value["case_id"] == f"{kind}-{operation}")


def found(request):
    binding = request["binding"]
    return {"profile": PROFILE, "operation_id": request["operation_id"], "status": "found",
            "snapshot": {"ceremony_id": request["ceremony_id"], "operation_id": request["operation_id"],
                         "attempt_id": binding["attempt_id"], "binding_digest": binding_digest(binding),
                         "expires_at": binding["source_expires_at"], "revision": 1,
                         "presentation_state": "active", "domain_status": "awaiting-confirmation"},
            "entry": {"entry_reference": binding["source"]["reference"],
                      "completion": expected_completion(binding)}}


@pytest.mark.parametrize("case", cases("schema"), ids=lambda value: value["case_id"])
def test_every_published_closed_schema_case(case):
    name = case["schema"].rsplit("/", 1)[-1].removesuffix(".schema.json")
    if case["valid"]:
        _contract().validate(name, case["value"])
    else:
        with pytest.raises(HostedGrantUnavailable):
            _contract().validate(name, case["value"])


@pytest.mark.parametrize("case", cases("parsing"), ids=lambda value: value["case_id"])
def test_published_encoding_and_size_cases(case):
    def parse():
        return canonical_request(_document(case["document"].encode("utf-8"), limit=case["limit"]))
    if case["valid"]:
        value = json.loads(case["document"])
        if isinstance(value, dict):
            assert parse().hex() == case["canonical_hex"]
        else:
            # Generic canonical-JSON vectors also exercise scalar byte limits.
            # HTTP ceremony documents impose the additional object-only rule.
            assert len(case["document"].encode("utf-8")) <= case["limit"]
            assert canonical_request(value).hex() == case["canonical_hex"]
            with pytest.raises(HostedGrantUnavailable):
                parse()
    else:
        with pytest.raises((HostedGrantUnavailable, UnicodeError)):
            parse()


@pytest.mark.parametrize("item", PROOFS, ids=lambda value: value["case_id"])
def test_separate_proof_domain_matches_published_bytes(item):
    subject, transport, key = client(item)
    operation = item["operation"].removeprefix("browser-ceremony-")
    result = subject.envelope(operation, item["request"])
    assert result == {"request": item["request"], "proof": {
        "challenge": item["challenge"], "signature": item["signature"]}}
    assert key.reopened == key.signed == 1
    assert [call[1] for call in transport.calls] == [CHALLENGE_PATH]


@pytest.mark.parametrize("field", ["installation_key_id", "installation_key_jkt"])
def test_wrong_key_stops_before_challenge_or_signing(field):
    item = vector()
    subject, transport, key = client(item)
    key.reference = replace(key.reference, **{field: "different-key"})
    with pytest.raises(HostedGrantUnavailable, match="key changed"):
        subject.read(item["request"])
    assert not transport.calls and key.signed == 0


def test_proof_clock_budget_is_enforced_before_signing():
    ticks = iter([0.0, 60.0])
    subject, transport, key = client(vector(), monotonic=lambda: next(ticks))
    with pytest.raises(HostedGrantUnavailable, match="budget exhausted"):
        subject.read(vector()["request"])
    assert len(transport.calls) == 1 and key.signed == 0


def test_lost_prepare_reply_does_not_repeat_or_replace_operation():
    item = vector("prepare")
    subject, transport, _ = client(item, OSError("lost reply"))
    with pytest.raises(HostedGrantUnavailable):
        subject.prepare(item["request"])
    assert [call[1] for call in transport.calls] == [CHALLENGE_PATH, PATH_PREFIX + "prepare"]
    original = vector()
    unknown = copy.deepcopy(original["request"])
    unknown.pop("ceremony_id")
    unknown_vector = next(value for value in PROOFS if value["case_id"] == "initial-read-unknown")
    result = {"profile": PROFILE, "operation_id": unknown["operation_id"], "status": "unavailable"}
    reader, reads, _ = client(unknown_vector, result)
    assert reader.read(unknown) == result
    assert [call[1] for call in reads.calls] == [CHALLENGE_PATH, PATH_PREFIX + "read"]


@pytest.mark.parametrize("field", ["operation_id", "ceremony_id", "attempt_id", "binding_digest", "expires_at"])
def test_result_cannot_change_saved_scope(field):
    item = vector()
    result = found(item["request"])
    values = {"operation_id": "0199b234-5678-7000-8000-000000000007",
              "ceremony_id": "0199b234-5678-7000-8000-000000000008",
              "attempt_id": "A" * 43, "binding_digest": "a" * 64,
              "expires_at": "2026-10-11T12:09:59.000Z"}
    result["snapshot"][field] = values[field]
    subject, _, _ = client(item, result)
    with pytest.raises(HostedGrantUnavailable):
        subject.read(item["request"], expires_at=item["request"]["binding"]["source_expires_at"])


def test_completion_must_match_exact_selected_extension():
    item = vector()
    result = found(item["request"])
    result["entry"]["completion"]["return_uri"] = (
        "https://" + "b" * 32 + ".chromiumapp.org/browser-ceremony/v1/complete")
    subject, _, _ = client(item, result)
    with pytest.raises(HostedGrantUnavailable, match="completion changed"):
        subject.read(item["request"])


def test_retired_presentation_preserves_verified_domain_status_without_entry():
    item = vector()
    result = found(item["request"])
    result.pop("entry")
    result["snapshot"].update(presentation_state="retired", domain_status="committed")
    subject, _, _ = client(item, result)
    assert subject.read(item["request"]) == result


def test_only_closed_consistent_refusal_is_definitive():
    item = vector("prepare")
    result = {"profile": PROFILE, "reason": "operation_conflict"}
    subject, _, _ = client(item, result, 409)
    with pytest.raises(BrowserCeremonyRefused) as refused:
        subject.prepare(item["request"])
    assert refused.value.reason == "operation_conflict"
    subject, _, _ = client(item, result, 403)
    with pytest.raises(HostedGrantUnavailable):
        subject.prepare(item["request"])


def test_browser_entry_has_the_pinned_fixed_path_and_no_client_return_uri():
    profile = json.loads((ROOT.parent / "profiles/browser-ceremony/v1/profile.json").read_text("utf-8"))
    assert profile["browser_entry_path"] == ENTRY_PREFIX + "{entry_reference}"
    reference = "A" * 43
    assert browser_entry_url("https://provider.example.test", reference) == (
        "https://provider.example.test" + ENTRY_PREFIX + reference)
    for origin in ("https://provider.example.test/other", "https://provider.example.test?return=elsewhere"):
        with pytest.raises(ValueError):
            browser_entry_url(origin, reference)


@pytest.mark.parametrize("changed", [None, "operation_id", "installation_id", "installation_key_id", "installation_key_jkt"])
def test_initial_receipt_must_belong_to_the_exact_source_operation(changed):
    item = vector()
    binding = item["request"]["binding"]
    result = found(item["request"])
    result.pop("entry")
    receipt = {name: binding[name] for name in ("installation_id", "installation_key_id", "installation_key_jkt")}
    receipt.update(claim_id="00000000-0000-7000-8000-000000000001", operation_id=binding["source"]["operation_id"],
        onboarding_transaction_id="transaction", organization_id="organization", consumed_at="2026-10-11T12:00:00.000Z")
    if changed is not None:
        receipt[changed] = {"operation_id": "00000000-0000-7000-8000-000000000009", "installation_id": "other-installation",
            "installation_key_id": "other-key", "installation_key_jkt": "A" * 43}[changed]
    result["snapshot"].update(presentation_state="retired", domain_status="committed", result={
        "kind": "initial-handoff-receipt", "receipt": {"profile": binding["source"]["profile"], "status": "recovery-required", "receipt": receipt}})
    subject, _, _ = client(item, result)
    if changed is None:
        assert subject.read(item["request"]) == result
    else:
        with pytest.raises(HostedGrantUnavailable):
            subject.read(item["request"])
