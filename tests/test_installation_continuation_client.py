"""Independent local consumer checks against published continuation vectors."""

import base64
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.persistence.auth import InstallationKeyReference
from app.security.hosted_grants import HostedGrantUnavailable, TransportResponse
from app.security.installation_continuation import (
    CHALLENGE_PATH, PATH_PREFIX, PROFILE, PROOF_PROFILE,
    InstallationContinuationClient, InstallationContinuationRefused,
    _contract, _document, canonical_request, continuation_frames,
)
from app.security.installation_key import InstallationProof


pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]
ROOT = Path(__file__).resolve().parents[1] / "contracts/installation-setup-continuation-v2"
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def cases(name):
    return json.loads((ROOT / f"{name}-cases.json").read_text(encoding="utf-8"))


PROOFS = [case for case in cases("proof") if case["valid"]]
SCHEMAS = cases("schema")
PARSING = cases("parsing")
RESPONSES = cases("response")


def response(value, status=200):
    return TransportResponse(status, json.dumps(value, separators=(",", ":")).encode(), "application/json")


class VectorKey:
    """Uses a published public key and matching signature; no private key exists."""

    def __init__(self, vector):
        self.vector = vector
        key_id = vector["request"]["scope"]["installation_key_id"]
        thumbprint = hashlib.sha256(canonical_request(vector["public_key"])).digest()
        self.reference = InstallationKeyReference(
            "test-vector", "published-public-only", "ES256", key_id,
            base64.urlsafe_b64encode(thumbprint).rstrip(b"=").decode(),
            json.dumps({**vector["public_key"], "kid": key_id}), NOW, NOW,
        )
        self.reopened = 0
        self.signed = 0

    def ensure_ready(self):
        pytest.fail("registered continuation attempted initial key creation")

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
                             "request_digest": self.vector["request_digest"], "expires_at": "2026-10-09T12:01:00.000Z"})
        if isinstance(self.result, Exception):
            raise self.result
        return response(self.result, self.status)


def client(vector, result=None, status=200, **kwargs):
    transport, key = Transport(vector, result, status), VectorKey(vector)
    return InstallationContinuationClient(transport, key, clock=lambda: NOW, **kwargs), transport, key


@pytest.mark.parametrize("case", SCHEMAS, ids=lambda c: c["case_id"])
def test_consumer_uses_every_published_closed_schema_case(case):
    name = case["schema"].rsplit("/", 1)[-1].removesuffix(".schema.json")
    if case["valid"]:
        _contract().validate(name, case["value"])
    else:
        with pytest.raises(HostedGrantUnavailable):
            _contract().validate(name, case["value"])


@pytest.mark.parametrize("case", PARSING, ids=lambda c: c["case_id"])
def test_published_json_encoding_and_raw_size_cases(case):
    def parse():
        raw = case["document"].encode("utf-8")
        return canonical_request(_document(raw, limit=case["limit"]))

    if case["valid"]:
        assert parse().hex() == case["canonical_hex"]
    else:
        with pytest.raises((HostedGrantUnavailable, UnicodeError)):
            parse()


@pytest.mark.parametrize("vector", PROOFS, ids=lambda c: c["case_id"])
def test_proof_bytes_match_independent_published_vectors_without_initial_key_path(vector):
    subject, transport, key = client(vector)
    operation = vector["operation"].removeprefix("installation-continuation-")
    envelope = subject.envelope(operation, vector["request"])
    assert envelope == {"request": vector["request"], "proof": {
        "challenge": vector["challenge"], "signature": vector["signature"],
    }}
    assert key.reopened == key.signed == 1
    assert len(transport.calls) == 1
    assert transport.calls[0][1] == CHALLENGE_PATH


@pytest.mark.parametrize("case", RESPONSES, ids=lambda c: c["case_id"])
def test_exact_read_result_cannot_change_operation_context_or_expiry(case):
    vector = next(item for item in PROOFS if item["case_id"] == "read-known-valid")
    subject, transport, _ = client(vector, case["response"])
    if case["expected"]:
        assert subject.read(vector["request"]) == case["response"]
    else:
        with pytest.raises(HostedGrantUnavailable):
            subject.read(vector["request"])
    assert [call[1] for call in transport.calls] == [CHALLENGE_PATH, PATH_PREFIX + "read"]


def test_prepare_lost_response_is_not_replayed_by_transport():
    vector = PROOFS[0]
    subject, transport, _ = client(vector, OSError("fixture lost response"))
    with pytest.raises(HostedGrantUnavailable):
        subject.prepare(vector["request"])
    assert [call[1] for call in transport.calls] == [CHALLENGE_PATH, PATH_PREFIX + "prepare"]


@pytest.mark.parametrize("result", ["expired", "unavailable"])
def test_read_terminal_result_never_prepares_another_context(result):
    vector = next(item for item in PROOFS if item["case_id"] == "read-valid")
    body = {"profile": PROFILE, "operation_id": vector["request"]["operation_id"], "result": result}
    subject, transport, _ = client(vector, body)
    assert subject.read(vector["request"]) == body
    assert len(transport.calls) == 2
    assert not any(path.endswith(":prepare") for _, path, _ in transport.calls)


def test_wrong_local_key_prevents_challenge_or_signing():
    vector = PROOFS[0]
    subject, transport, key = client(vector)
    request = copy.deepcopy(vector["request"])
    request["scope"]["installation_key_id"] = "ik1.AAAAAAAAAAAAAAAAAAAAAA"
    with pytest.raises(HostedGrantUnavailable, match="key changed"):
        subject.envelope("prepare", request)
    assert transport.calls == []
    assert key.signed == 0


def test_expired_monotonic_proof_budget_prevents_signature():
    vector = PROOFS[0]
    ticks = iter([0.0, 60.0])
    subject, transport, key = client(vector, monotonic=lambda: next(ticks))
    with pytest.raises(HostedGrantUnavailable, match="budget exhausted"):
        subject.envelope("prepare", vector["request"])
    assert len(transport.calls) == 1
    assert key.signed == 0


def test_closed_refusal_is_the_only_source_for_a_specific_reason():
    vector = PROOFS[0]
    subject, _, _ = client(vector, {"profile": PROFILE, "reason": "scope_conflict"}, status=409)
    with pytest.raises(InstallationContinuationRefused) as refused:
        subject.prepare(vector["request"])
    assert refused.value.reason == "scope_conflict"
    subject, _, _ = client(vector, {"profile": PROFILE, "reason": "scope_conflict"}, status=403)
    with pytest.raises(HostedGrantUnavailable):
        subject.prepare(vector["request"])


def event_frame(*, kind="snapshot", revision=1, extra="", event_id=None):
    value = copy.deepcopy(RESPONSES[0]["response"]["snapshot"])
    value["revision"] = revision
    event = {"profile": PROFILE, "kind": kind, "snapshot": value}
    cursor = event_id or f"{value['epoch']}:{revision}"
    return (f"event: installation-continuation\nid: {cursor}\n{extra}data: " + json.dumps(event) + "\n\n").encode(), event


async def chunks(*values):
    for value in values:
        yield value


@pytest.mark.asyncio
async def test_stream_delivers_first_frame_before_waiting_for_more_network_bytes():
    frame, event = event_frame()
    reached_next_read = False

    async def source():
        nonlocal reached_next_read
        yield b": heartbeat\n\n" + frame
        reached_next_read = True
        raise AssertionError("consumer waited beyond a complete first frame")

    events = continuation_frames(source(), continuation_id=event["snapshot"]["continuation_id"])
    assert await anext(events) == event
    assert reached_next_read is False
    await events.aclose()


@pytest.mark.asyncio
async def test_stream_accepts_fragmented_frames_and_multiple_arrivals_without_polling():
    first, event = event_frame()
    second, later = event_frame(kind="committed", revision=2)
    values = [item async for item in continuation_frames(chunks(first[:10], first[10:] + second),
              continuation_id=event["snapshot"]["continuation_id"], expires_at=event["snapshot"]["expires_at"])]
    assert values == [event, later]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["cursor", "duplicate", "first-commit", "large", "partial", "wrong-context", "multiline"])
async def test_stream_rejects_invalid_or_out_of_scope_frames(bad):
    frame, event = event_frame()
    context = event["snapshot"]["continuation_id"]
    if bad == "cursor":
        frame, _ = event_frame(event_id="wrong")
    elif bad == "duplicate":
        frame, _ = event_frame(extra="event: installation-continuation\n")
    elif bad == "first-commit":
        frame, _ = event_frame(kind="committed")
    elif bad == "large":
        frame = b":" + b"x" * 16384 + b"\n\n"
    elif bad == "partial":
        frame = frame[:-1]
    elif bad == "wrong-context":
        context = "0199b234-5678-7000-8000-000000000009"
    elif bad == "multiline":
        frame, _ = event_frame(extra="data: {}\n")
    with pytest.raises(HostedGrantUnavailable):
        _ = [item async for item in continuation_frames(chunks(frame), continuation_id=context)]
