"""Bounded credentialless exchanges and fresh native proofs."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.security.signed_objects import decode, encode
from app.security.text_analysis_contract import canonical
from app.security.text_analysis_proof import ProofContext, prepare_request, verify_proof
from app.security.text_analysis_reissue import (
    CHALLENGE_PATH, PATH_PREFIX, PROFILE, PROOF_PROFILE, ReissueContext,
    TextAnalysisReissueClient, TextAnalysisReissueError, ValidatedReissueResult,
)
from app.security.text_analysis_reissue_transport import (
    ENTRY_PREFIX, TextAnalysisReissueHTTPTransport, browser_entry_url,
)
from tests.test_text_analysis_authority import local_signer  # noqa: F401
from tests.test_text_analysis_proof import ExistingKey
from tests.test_text_analysis_reissue import context, RESPONSES, NOW, schemas  # noqa: F401

pytestmark = pytest.mark.ci_tier("fast")


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed, self.reads = chunks, False, 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def response(body=b"{}", *, status=200, headers=None, stream=None):
    return httpx.Response(status, headers=headers or {"content-type": "application/json", "cache-control": "no-store"},
                          stream=stream or Stream([body]))


@pytest.mark.parametrize("origin", ["http://example.test", "https://u:p@example.test", "https://example.test/path",
                                     "https://example.test/?x", "https://example.test/?", "https://example.test/#"])
def test_origin_and_navigation_are_fixed(origin):
    with pytest.raises(TextAnalysisReissueError):
        TextAnalysisReissueHTTPTransport(origin, transport=httpx.MockTransport(lambda _: response()))
    with pytest.raises(TextAnalysisReissueError):
        browser_entry_url(origin, "A" * 43)


def test_browser_entry_matches_the_published_route():
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[1]
    spec = yaml.safe_load((root / "contracts/openapi/license-reissue-text-analysis-v2.openapi.yaml").read_bytes())
    assert ENTRY_PREFIX + "{entry_reference}" in spec["paths"]
    assert spec["paths"][ENTRY_PREFIX + "{entry_reference}"]["get"]["operationId"] == "openTextAnalysisLicenseReissueV2"
    assert browser_entry_url("https://example.test/", "A" * 43) == "https://example.test" + ENTRY_PREFIX + "A" * 43
    for bad in ("A" * 43 + "=", "../bad", "https://elsewhere.test", "A" * 42):
        with pytest.raises(TextAnalysisReissueError):
            browser_entry_url("https://example.test", bad)


@pytest.mark.parametrize("headers", [
    {"content-type": "text/html", "cache-control": "no-store"},
    {"content-type": "application/json", "cache-control": "public"},
    {"content-type": "application/json"},
    {"content-type": "application/json", "cache-control": "no-store", "content-encoding": "gzip"},
    [("content-type", "application/json"), ("content-type", "text/html"), ("cache-control", "no-store")],
    {"content-type": "application/json", "cache-control": "no-store", "content-length": "4097"},
])
def test_invalid_headers_refuse_before_body_reads(headers):
    stream = Stream([b"sensitive response"])
    transport = TextAnalysisReissueHTTPTransport("https://example.test", transport=httpx.MockTransport(
        lambda _: response(headers=headers, stream=stream)))
    with pytest.raises(TextAnalysisReissueError) as raised:
        transport.request(CHALLENGE_PATH, b"{}", maximum=4096)
    assert stream.reads == 0 and stream.closed
    assert "sensitive" not in str(raised.value)


@pytest.mark.parametrize("status,maximum", [(200, 4096), (503, 4096)])
def test_streamed_body_bounds_close_and_do_not_retry(status, maximum):
    stream = Stream([b"x" * 4096, b"x"])
    calls = []
    def handle(request):
        calls.append(request)
        return response(status=status, stream=stream)
    transport = TextAnalysisReissueHTTPTransport("https://example.test", transport=httpx.MockTransport(handle))
    with pytest.raises(TextAnalysisReissueError) as raised:
        transport.request(CHALLENGE_PATH, b"{}", maximum=maximum)
    assert raised.value.code == "response_size" and stream.closed and len(calls) == 1


def test_cookies_and_redirects_have_no_followup_authority():
    requests = []
    def handle(request):
        requests.append(request)
        return response(status=302, headers={"content-type": "application/json", "cache-control": "no-store",
            "set-cookie": "session=secret; Secure", "location": "https://other.test"})
    transport = TextAnalysisReissueHTTPTransport("https://example.test", transport=httpx.MockTransport(handle))
    for _ in range(2):
        result = transport.request(CHALLENGE_PATH, b"{}", maximum=4096)
        assert result.status_code == 302
    assert len(requests) == 2
    for request in requests:
        assert request.url.host == "example.test"
        assert all(name not in request.headers for name in ("cookie", "authorization", "proxy-authorization", "idempotency-key"))
        assert request.content == b"{}"


@pytest.mark.parametrize("path,raw,maximum", [("https://other.test", b"{}", 4096),
    (PATH_PREFIX + "read?x", b"{}", 24576), (CHALLENGE_PATH, b"x" * 12289, 4096),
    (PATH_PREFIX + "read", b"x" * 8193, 24576), (CHALLENGE_PATH, b"{}", True)])
def test_invalid_request_never_reaches_transport(path, raw, maximum):
    def refuse(_):
        pytest.fail("invalid request reached HTTP")
    transport = TextAnalysisReissueHTTPTransport("https://example.test", transport=httpx.MockTransport(refuse))
    with pytest.raises(TextAnalysisReissueError):
        transport.request(path, raw, maximum=maximum)


def test_socket_failure_is_content_free_and_not_retried():
    calls = []
    def fail(request):
        calls.append(request)
        raise RuntimeError("private body and path")
    transport = TextAnalysisReissueHTTPTransport("https://example.test", transport=httpx.MockTransport(fail))
    with pytest.raises(TextAnalysisReissueError) as raised:
        transport.request(CHALLENGE_PATH, b"{}", maximum=4096)
    assert raised.value.code == "transport_unavailable" and "private" not in str(raised.value) and len(calls) == 1


class TimedStream(Stream):
    def __init__(self, clock, delays, *, close_delay=0):
        super().__init__([b"x"] * len(delays))
        self.clock, self.delays, self.close_delay = clock, delays, close_delay

    def __iter__(self):
        for delay, chunk in zip(self.delays, self.chunks):
            self.clock[0] += delay
            self.reads += 1
            yield chunk

    def close(self):
        self.clock[0] += self.close_delay
        super().close()


def test_late_headers_refuse_before_body_reads():
    clock, calls, stream = [0], [], Stream([b"{}"])
    def handle(request):
        calls.append(request)
        assert request.extensions["timeout"] == dict(connect=10, read=10, write=10, pool=10)
        clock[0] = 10
        return response(stream=stream)
    transport = TextAnalysisReissueHTTPTransport("https://example.test", timeout_seconds=10,
        monotonic=lambda: clock[0], transport=httpx.MockTransport(handle))
    with pytest.raises(TextAnalysisReissueError) as raised:
        transport.request(CHALLENGE_PATH, b"{}", maximum=4096)
    assert raised.value.code == "transport_deadline" and stream.reads == 0
    assert stream.closed and len(calls) == 1


def test_small_fragment_checks_window_before_another_read():
    clock = [0]
    stream = TimedStream(clock, [10, 0])
    transport = TextAnalysisReissueHTTPTransport("https://example.test", timeout_seconds=10,
        monotonic=lambda: clock[0], transport=httpx.MockTransport(lambda _: response(stream=stream)))
    with pytest.raises(TextAnalysisReissueError) as raised:
        transport.request(CHALLENGE_PATH, b"{}", maximum=4096)
    assert raised.value.code == "transport_deadline" and stream.reads == 1 and stream.closed


def test_fragment_delays_accumulate_within_acceptance_window():
    clock = [0]
    stream = TimedStream(clock, [4, 4, 4, 0])
    transport = TextAnalysisReissueHTTPTransport("https://example.test", timeout_seconds=10,
        monotonic=lambda: clock[0], transport=httpx.MockTransport(lambda _: response(stream=stream)))
    with pytest.raises(TextAnalysisReissueError) as raised:
        transport.request(CHALLENGE_PATH, b"{}", maximum=4096)
    assert raised.value.code == "transport_deadline" and stream.reads == 3 and stream.closed


def test_late_explicit_close_prevents_response_acceptance(monkeypatch):
    clock = [0]
    stream = TimedStream(clock, [], close_delay=10)
    reply = response(stream=stream)
    # Leave closure to the request's explicit cleanup boundary.
    monkeypatch.setattr(reply, "iter_raw", lambda **_: iter([b"{}"]))
    transport = TextAnalysisReissueHTTPTransport("https://example.test", timeout_seconds=10,
        monotonic=lambda: clock[0], transport=httpx.MockTransport(lambda _: reply))
    with pytest.raises(TextAnalysisReissueError) as raised:
        transport.request(CHALLENGE_PATH, b"{}", maximum=4096)
    assert raised.value.code == "transport_deadline" and stream.closed


def make_reissue_client(schemas, local_signer, *, origin="https://example.test", now=NOW, reason=None):
    from app.persistence.auth import InstallationKeyReference
    kid, trust, sign = local_signer
    private = ec.generate_private_key(ec.SECP256R1())
    point = private.public_key().public_numbers()
    public = {"kty": "EC", "crv": "P-256", "x": encode(point.x.to_bytes(32, "big")), "y": encode(point.y.to_bytes(32, "big"))}
    digest = hashlib.sha256(canonical(public)).digest()
    key_id, jkt = "ik1." + encode(digest[:16]), encode(digest)
    original = json.loads(context().original_prepare)
    original["binding"].update(installation_key_id=key_id, installation_key_jkt=jkt)
    basis = ReissueContext(canonical(original), canonical(dict(public, kid=key_id)))
    proof_context = ProofContext("reissue-v2-prepare", **basis.binding,
                                public_key_jwk=basis.public_key_jwk, expected_request=basis.original_prepare)
    class Key(ExistingKey):
        ensure_calls = 0

        def reopen_existing(self):
            observed = super().reopen_existing()
            return InstallationKeyReference("fixture-provider", "fixture-key", "ES256",
                observed.installation_key_id, observed.installation_key_jkt,
                observed.public_key_jwk, now, now)

        def ensure_ready(self):
            self.ensure_calls += 1
            return self.reopen_existing()
    key = Key(private, proof_context)
    confirmation = copy.deepcopy(RESPONSES[0]["confirmation"])
    if reason is not None:
        confirmation["reissue_reason"] = reason
    confirmation["binding"] = basis.binding
    confirmation["expires_at"] = (now + timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    authority_context = dict(RESPONSES[0]["context"], expected_installation_key_id=key_id, expected_installation_key_jkt=jkt)
    result = copy.deepcopy(RESPONSES[0]["result"])
    result["binding"] = basis.binding
    payload = json.loads(decode(result["signed_authority"].split(".")[1], 16384))
    payload.update(basis.binding)
    result["signed_authority"] = sign({"alg": "ES256", "kid": kid, "typ": "urn:bridge-clean:text-analysis-license:v1"}, payload)
    result_raw = None
    requests, challenges = [], {}
    clock = [now]
    mode = {"state": "authorized", "late": False, "error": None, "replies": {},
            "lose_finalize_reply": False, "result_raw": None}
    def supplied(action):
        replies = mode["replies"].get(action, [])
        if not replies:
            return None
        status, body = replies.pop(0)
        return response(canonical(body), status=status)
    def handle(request):
        nonlocal result_raw
        assert not any(name in request.headers for name in ("cookie", "authorization", "idempotency-key"))
        value = json.loads(request.content)
        requests.append((request.url.path, value))
        if request.url.path == CHALLENGE_PATH:
            overridden = supplied("challenge")
            if overridden is not None:
                return overridden
            action = value["purpose"].rsplit("-", 1)[1]
            prepared = prepare_request(canonical(value["request"]), context=replace(proof_context,
                operation="reissue-v2-" + action, expected_request=canonical(value["request"])), schemas=schemas)
            identifier = "019a1957-0000-7000-8000-" + f"{len(challenges)+1:012d}"
            challenge = {"profile": PROOF_PROFILE, "purpose": value["purpose"], "challenge_id": identifier,
                "challenge": "A" * 43, "audience": "urn:bridge-clean:commercial-control-plane:text-analysis-license-reissue:v2",
                "request_digest": prepared.request_digest.hex(), "issued_at": clock[0].strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "expires_at": (clock[0] + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
            challenges[identifier] = (prepared, False)
            return response(canonical(challenge), headers={"content-type": "application/json", "cache-control": "no-store", "set-cookie": "ignore=secret"})
        action = request.url.path.removeprefix(PATH_PREFIX)
        prepared, consumed = challenges[value["proof"]["challenge_id"]]
        assert not consumed
        assert verify_proof(value["proof"]["signature"], prepared=prepared,
            challenge_id=value["proof"]["challenge_id"], challenge=value["proof"]["challenge"],
            context=replace(proof_context, operation="reissue-v2-" + action, expected_request=canonical(value["request"])), schemas=schemas)
        challenges[value["proof"]["challenge_id"]] = (prepared, True)
        overridden = supplied(action)
        if overridden is not None:
            return overridden
        if mode["error"] is not None:
            status, body = mode["error"]
            return response(canonical(body), status=status)
        common = {"profile": PROFILE, "operation_id": basis.operation_id, "binding": basis.binding}
        if action == "prepare":
            confirmation["operation_id"] = value["request"]["operation_id"]
            result["operation_id"] = value["request"]["operation_id"]
            common["operation_id"] = value["request"]["operation_id"]
            return response(canonical(dict(common, state="awaiting-authorization",
                expires_at=(now + timedelta(seconds=1800)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), entry_reference="A" * 43)))
        common["operation_id"] = confirmation["operation_id"]
        if action == "read" and mode["state"] == "awaiting-authorization":
            return response(canonical(dict(common, state="awaiting-authorization",
                expires_at=(now + timedelta(seconds=1800)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))))
        if action == "read" and mode["state"] == "authorized":
            return response(canonical(dict(common, state="authorized", expires_at=confirmation["expires_at"],
                confirmation=confirmation, confirmation_digest=hashlib.sha256(canonical(confirmation)).hexdigest())))
        if action == "finalize":
            result["finalization_request_digest"] = hashlib.sha256(canonical(value["request"])).hexdigest()
            result_raw = b" " + canonical(result) + b"\n"
            mode["result_raw"] = result_raw
            mode["state"] = "completed"
            if mode["lose_finalize_reply"]:
                mode["lose_finalize_reply"] = False
                raise httpx.ReadError("response unavailable", request=request)
            if mode["late"]:
                clock[0] += timedelta(minutes=31)
            return response(result_raw, status=201)
        if action == "recover" and result_raw is None:
            return response(canonical({"profile": PROFILE, "reason": "not_completed"}), status=409)
        assert result_raw is not None
        return response(result_raw)
    transport = TextAnalysisReissueHTTPTransport(origin, transport=httpx.MockTransport(handle))
    client = TextAnalysisReissueClient(transport, key, schemas, trust, clock=lambda: clock[0], monotonic=lambda: 0)
    return client, basis, authority_context, requests, challenges, mode, clock


@pytest.fixture
def scenario(schemas, local_signer):
    return make_reissue_client(schemas, local_signer)


def test_complete_client_flow_uses_fresh_proof_and_exact_recovery(scenario):
    client, basis, authority_context, requests, challenges, mode, clock = scenario
    prepared = client.prepare(basis)
    authorized = client.read(prepared.context, previous=prepared)
    mode["late"] = True
    result = client.finalize(authorized.confirmation, authority_context=authority_context)
    assert isinstance(result, ValidatedReissueResult)
    recovered = client.recover(prepared.context, confirmation=authorized.confirmation,
        authority_context=authority_context, original_finalization_request=result.original_finalization_request,
        previous_response=result.response_bytes)
    read = client.read(prepared.context, confirmation=authorized.confirmation,
        authority_context=authority_context, previous_response=result.response_bytes)
    retried = client.finalize(authorized.confirmation, authority_context=authority_context,
        original_finalization_request=result.original_finalization_request, previous_response=result.response_bytes)
    assert recovered.response_bytes == read.response_bytes == retried.response_bytes == result.response_bytes
    assert clock[0] > NOW + timedelta(minutes=30)
    assert len(challenges) == 6 and all(consumed for _, consumed in challenges.values())
    assert len(requests) == 12 and len(client.key_authority.messages) == 6


@pytest.mark.parametrize("status,reason", [(401, "proof_invalid"), (409, "request_conflict"),
                                         (503, "temporarily_unavailable"), (409, "unsupported_release")])
def test_prepare_denial_preserves_original_operation_without_retry(scenario, status, reason):
    client, basis, _, requests, _, mode, _ = scenario
    mode["error"] = (status, {"profile": PROFILE, "reason": reason})
    with pytest.raises(TextAnalysisReissueError) as raised:
        client.prepare(basis)
    assert raised.value.code == reason and len(requests) == 2
    assert requests[-1][1]["request"] == json.loads(basis.original_prepare)


def test_completed_read_requires_retained_confirmation_and_context(scenario):
    client, basis, authority_context, _, _, mode, _ = scenario
    prepared = client.prepare(basis)
    authorized = client.read(prepared.context)
    client.finalize(authorized.confirmation, authority_context=authority_context)
    with pytest.raises(TextAnalysisReissueError) as raised:
        client.read(prepared.context)
    assert raised.value.code == "confirmation_unavailable"


def test_fresh_challenge_does_not_allow_expired_finalization_or_key_change(scenario):
    client, basis, authority_context, requests, _, _, clock = scenario
    prepared = client.prepare(basis)
    authorized = client.read(prepared.context)
    clock[0] = NOW + timedelta(minutes=5)
    before = len(requests)
    with pytest.raises(TextAnalysisReissueError) as raised:
        client.finalize(authorized.confirmation, authority_context=authority_context)
    assert raised.value.code == "authorization_expired" and len(requests) == before


def test_rate_limit_is_typed_and_never_renews_operation(scenario):
    client, basis, _, requests, _, mode, _ = scenario
    mode["error"] = (429, {"profile": PROFILE, "reason": "rate_limited", "retry_at": "2026-10-26T00:00:10.000Z"})
    with pytest.raises(TextAnalysisReissueError) as raised:
        client.prepare(basis)
    assert raised.value.code == "rate_limited" and raised.value.retry_at == "2026-10-26T00:00:10.000Z"
    assert len(requests) == 2 and basis.operation_expires_at is None


def test_owner_fence_after_reply_prevents_result_return(scenario):
    client, basis, _, requests, _, _, _ = scenario
    def current():
        if len(requests) >= 2:
            raise ValueError("owner_changed")
    with pytest.raises(ValueError, match="^owner_changed$"):
        client.prepare(basis, before_send=current)
    assert len(requests) == 2


def test_challenge_expires_during_final_key_reopen(scenario):
    client, basis, _, requests, _, _, clock = scenario
    original = client.key_authority.reopen_existing
    calls = 0
    def reopen():
        nonlocal calls
        calls += 1
        if calls == 3:
            clock[0] += timedelta(seconds=60)
        return original()
    client.key_authority.reopen_existing = reopen
    with pytest.raises(ValueError):
        client.prepare(basis)
    assert len(requests) == 1


def test_final_key_reopen_refuses_changed_binding(scenario):
    client, basis, _, requests, _, _, _ = scenario
    client.key_authority.mode = "changed_after_sign"
    with pytest.raises(ValueError):
        client.prepare(basis)
    assert len(requests) == 1


NATIVE_ADMISSION = [case for case in json.loads((Path(__file__).resolve().parents[1] /
    "contracts/text-analysis-license-reissue-v2/admission-cases.json").read_bytes())
    if case["input"].get("action") in {"challenge", "prepare", "read", "finalize", "recover"}]


@pytest.mark.parametrize("case", NATIVE_ADMISSION, ids=lambda case: case["case_id"])
def test_native_admission_response_observation(case, tmp_path, local_signer):
    """Observe native requests and supplied outcomes, not provider decisions."""
    from app.security.text_analysis_reissue import build_read_request, build_recover_request, build_finalize_request
    from tests.text_analysis_reissue_fixture import make_reissue_fixture
    action, expected = case["input"]["action"], case["expected"]
    x = make_reissue_fixture(tmp_path, local_signer, reason=case["input"].get("reason"))
    client, basis, authority_context = x.client, x.context, x.authority_context
    requests, challenges, mode, clock = x.requests, x.challenges, x.mode, x.clock
    store, schemas = x.service.store, client.schemas
    original = replace(basis, operation_expires_at=None)
    record = store.begin(origin=x.service.origin, context=original,
        provider_name=x.key.provider_name, provider_key_name=x.key.provider_key_name,
        read_request=build_read_request(original, schemas=schemas),
        recover_request=build_recover_request(original, schemas=schemas))
    def adopt(result):
        return store.adopt(record, result, authority_store=x.service.authorities,
            authority_context=authority_context, trust=client.trust, schemas=schemas)
    prepared = authorized = previous = None
    if action not in {"prepare", "challenge"}:
        prepared = client.prepare(basis)
        record = store.prepared(record, prepared)
    if action in {"finalize", "recover"} or expected == "exact-result":
        authorized = client.read(prepared.context)
        record = store.authorized(record, authorized)
        record = store.finalization(record, build_finalize_request(authorized.confirmation,
            schemas=schemas, now=clock[0]))
    if expected == "exact-result":
        previous = client.finalize(authorized.confirmation, authority_context=authority_context)
        record = adopt(previous)
        clock[0] += timedelta(minutes=31)
    if action == "read" and expected == "awaiting-authorization":
        mode["state"] = "awaiting-authorization"
    successes = {"awaiting-authorization", "challenge", "completed", "exact-result"}
    if expected not in successes:
        status = {"invalid/400": 400, "invalid/413": 413, "rate_limited": 429,
            "temporarily_unavailable": 503, "unsupported_profile": 409,
            "unsupported_release": 409, "proof_invalid": 401, "request_conflict": 409,
            "authority_unavailable": 403, "not_completed": 409}[expected]
        reason = expected.split("/", 1)[0]
        body = {"profile": PROOF_PROFILE if action == "challenge" else PROFILE, "reason": reason}
        if reason == "rate_limited":
            body["retry_at"] = (clock[0] + timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        mode["replies"][action] = [(status, body)]
    before, signed = len(requests), len(client.key_authority.messages)
    epoch, ensure_calls = x.auth.read_epoch(), client.key_authority.ensure_calls
    with x.auth.database.read() as db:
        authorities_before = db.execute('SELECT count(*) FROM text_analysis_authority_references').fetchone()[0]
    def invoke():
        if action in {"prepare", "challenge"}:
            return client.prepare(basis)
        options = dict(confirmation=None if authorized is None else authorized.confirmation,
            authority_context=authority_context,
            original_finalization_request=None if previous is None else previous.original_finalization_request,
            previous_response=None if previous is None else previous.response_bytes)
        if action == "finalize":
            return client.finalize(options.pop("confirmation"), **options)
        return getattr(client, action)(prepared.context, **options)
    if expected not in successes:
        with pytest.raises(TextAnalysisReissueError) as raised:
            invoke()
        assert raised.value.code == reason
        assert raised.value.retry_at == body.get("retry_at")
        assert mode["replies"][action] == []
        assert store.get(record.flow_id) == record and x.auth.read_epoch() == epoch
    else:
        result = invoke()
        if expected in {"awaiting-authorization", "challenge"}:
            assert result.state == "awaiting-authorization"
            assert result.context.original_prepare == basis.original_prepare
            if action in {"prepare", "challenge"}:
                record = store.prepared(record, result)
            assert store.get(record.flow_id) == record and x.auth.read_epoch() == epoch
        else:
            assert isinstance(result, ValidatedReissueResult)
            assert result.response_bytes == mode["result_raw"]
            if previous is not None:
                assert result == previous
            assert json.loads(result.confirmation.confirmation_bytes)["reissue_reason"] == (
                case["input"].get("reason") or RESPONSES[0]["confirmation"]["reissue_reason"])
            retained = adopt(result)
            assert retained.bodies["result"] == result.response_bytes and retained.state == "adopted"
            assert x.auth.read_epoch().value == epoch.value + (previous is None)
    count = 1 if action == "challenge" and expected != "challenge" else 2
    assert len(requests) - before == count
    assert len(client.key_authority.messages) - signed == (count - 1)
    assert requests[before][0] == CHALLENGE_PATH
    if count == 2:
        sent = requests[-1][1]
        assert requests[-1][0] == PATH_PREFIX + ("prepare" if action == "challenge" else action)
        assert sent["request"]["operation_id"] == basis.operation_id
        assert sent["request"]["binding"] == basis.binding
        if action in {"prepare", "challenge"}:
            assert sent["request"] == json.loads(basis.original_prepare)
        elif action == "finalize":
            assert sent["request"]["confirmation_digest"] == authorized.confirmation.confirmation_digest
        else:
            assert set(sent["request"]) == {"profile", "operation_id", "binding"}
    assert len({value["proof"]["challenge_id"] for path, value in requests if path != CHALLENGE_PATH}) == sum(
        path != CHALLENGE_PATH for path, _ in requests)
    assert client.key_authority.ensure_calls == ensure_calls
    with x.auth.database.read() as db:
        expected_authorities = 1 if expected in {"completed", "exact-result"} else authorities_before
        assert db.execute('SELECT count(*) FROM text_analysis_authority_references').fetchone()[0] == expected_authorities
