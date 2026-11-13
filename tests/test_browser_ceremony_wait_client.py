"""Wait client conformance against the published proof, schema and frame vectors."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from app.security import browser_ceremony_wait as wait
from app.security.hosted_grants import HostedGrantUnavailable, TransportResponse
from app.security.installation_continuation import _decode, canonical_request
from app.security.installation_key import InstallationProof
from test_browser_ceremony_client import VectorKey

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]
ROOT = Path(__file__).resolve().parents[1] / "contracts/browser-ceremony-wait-v1"


def cases(name):
    return json.loads((ROOT / f"{name}-cases.json").read_bytes())


GOOD = next(item for item in cases("proof") if item["valid"])
CEREMONY = GOOD["request"]["ceremony_id"]


class Key(VectorKey):
    def __init__(self, item):
        super().__init__(GOOD)
        self.vector = item
        self.reference = replace(self.reference, public_key_jwk=json.dumps({
            "kid": self.reference.installation_key_id, **item["public_key"]}))
        self.message = None

    def sign_challenge(self, message):
        self.signed += 1
        self.message = message
        return InstallationProof(self.reference.installation_key_id, "ES256",
                                 _decode(self.vector["signature"], 64))


class Transport:
    def __init__(self, item, challenge=None):
        self.item, self.challenge = item, challenge

    def request(self, method, path, *, json_body):
        assert (method, path) == ("POST", wait.CHALLENGE_PATH)
        if json_body["target"]["operation"] != self.item["operation"]:
            raise HostedGrantUnavailable("Wrong proof purpose")
        value = self.challenge if self.challenge is not None else {
            "profile": wait.PROOF_PROFILE, "challenge": self.item["challenge"],
            "request_digest": self.item["request_digest"], "expires_at": "2026-11-12T12:01:00.000Z"}
        return TransportResponse(200, json.dumps(value).encode(), "application/json")


def client(item=GOOD, **kwargs):
    key = Key(item)
    return wait.BrowserCeremonyWaitClient(Transport(item), key, "https://provider.invalid", **kwargs), key


@pytest.mark.parametrize("item", cases("proof"), ids=lambda item: item["case_id"])
def test_every_published_proof(item):
    subject, key = client(item)
    if item["valid"]:
        assert canonical_request(item["request"]).decode() == item["canonical_request"]
        assert hashlib.sha256(canonical_request(item["request"])).hexdigest() == item["request_digest"]
        assert subject.envelope(item["request"]) == {
            "request": item["request"], "proof": {name: item[name] for name in ("challenge", "signature")}}
        assert key.message.hex() == item["proof_bytes_hex"]
    else:
        with pytest.raises(HostedGrantUnavailable):
            subject.envelope(item["request"])


@pytest.mark.parametrize("item", cases("schema"), ids=lambda item: item["case_id"])
def test_every_published_closed_schema(item):
    root = ROOT.parent
    documents = [json.loads(path.read_bytes()) for path in (root / "schemas").rglob("*.schema.json")]
    registry = Registry().with_resources((value["$id"], Resource.from_contents(value)) for value in documents)
    validator = Draft202012Validator(json.loads((root / item["schema"]).read_bytes()), registry=registry)
    assert validator.is_valid(item["value"]) == item["valid"]


@pytest.mark.parametrize("item", [item for item in cases("schema")
    if item["schema"].endswith(("/wait-event.schema.json", "/proof-challenge-response.schema.json"))],
    ids=lambda item: item["case_id"])
def test_every_published_incoming_document_through_the_client(item):
    name = item["schema"].rsplit("/", 1)[-1].removesuffix(".schema.json")
    value = copy.deepcopy(item["value"])

    def exercise():
        subject, _ = client()
        if name == "wait-event":
            return wait.parse_hint(json.dumps(value), CEREMONY)
        if name == "proof-challenge-response":
            subject.transport = Transport(GOOD, value)
            return subject._challenge({"profile": wait.PROOF_PROFILE,
                "target": {"operation": wait.PURPOSE, "request": GOOD["request"]}})

    if item["valid"]:
        exercise()
    else:
        with pytest.raises(HostedGrantUnavailable):
            exercise()


@pytest.mark.parametrize("item", cases("frame"), ids=lambda item: item["case_id"])
async def test_every_published_frame_trace(item):
    frames = item["expected"]["frames"]
    wire = b": keepalive\n\n" + b"".join(
        b"event: browser-ceremony\ndata: " + json.dumps({"profile": wait.PROFILE,
            "ceremony_id": CEREMONY, "revision": revision,
            "presentation_state": state, "domain_status": status}).encode() + b"\n\n"
        for revision, state, status in frames)
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=wire, headers={"content-type": "text/event-stream"}))
    subject, _ = client(stream_transport=transport)
    assert [list(hint) async for hint in subject.hints({"request": GOOD["request"]})] == frames


@pytest.mark.parametrize("wire", [b"event: browser-ceremony\ndata: {}\n\n",
    b"event: browser-ceremony\nid: 1\ndata: {}\n\n", b"data: {}\n\n",
    b"event: browser-ceremony\ndata: {}\ndata: {}\n\n", b"data: \xff\n\n",
    b"event: browser-ceremony\ndata: {}", b"x" * 1025])
async def test_malformed_wire_frames_are_refused(wire):
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=wire, headers={"content-type": "text/event-stream"}))
    subject, _ = client(stream_transport=transport)
    with pytest.raises(HostedGrantUnavailable):
        _ = [hint async for hint in subject.hints({"request": GOOD["request"]})]


def test_other_ceremony_is_only_an_ignored_hint():
    value = next(item["value"] for item in cases("schema") if item["case_id"] == "wait-event")
    assert wait.parse_hint(json.dumps(value), "0199b234-5678-7000-8000-000000000099") is None
