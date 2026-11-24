"""Published proof vectors and closed installation authority recovery replies."""
import copy
import hashlib
import json
import logging
from dataclasses import replace
from datetime import timedelta

import pytest

from app.onboarding import timing
from app.security.hosted_grants import (
    HostedFailureStage, HostedGrantUnavailable, HostedTransportFailure, TransportResponse,
)
from app.security.installation_authority_recovery import (
    PROFILE, InstallationAuthorityRecoveryClient, AuthorityRecoveryRefused, exact_request,
    challenge_target, scope_digest, validate, contract, WITHDRAWALS,
)
from app.security.installation_continuation import canonical_request
from test_source_replacement_client import ROOT, NOW, Key, REGISTERED, Transport

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]
VECTORS = json.loads((ROOT / "installation-authority-recovery-v1/cases.json").read_text("utf-8"))


def wire_time(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def response_for(verb, request):
    value = {"profile": PROFILE, "recovery_id": request["recovery_id"], "scope_digest": scope_digest(request["scope"])}
    if verb == "prepare":
        return dict(value, status="prepared", source_reference="A"*43, expires_at=wire_time(NOW+timedelta(minutes=20)))
    scope = request["scope"]
    creator = {key: v for key, v in scope["evidence"]["creator_scope"].items() if key != "binding_jti"}
    # Transport deliberately does not verify JWS claims; these structurally
    # valid fixture strings must be independently verified by the owner.
    grants = {"installation_grant": "eyJhbGciOiJFUzI1NiJ9.e30.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "membership_snapshot": "eyJhbGciOiJFUzI1NiJ9.e30.BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"}
    if creator["kind"] == "approved":
        grants["creator_account_binding"] = "eyJhbGciOiJFUzI1NiJ9.e30.CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    return dict(value, status="committed", enrolled_at=wire_time(NOW-timedelta(days=40)), committed_at=wire_time(NOW),
        creator_scope=creator, grants=grants, **{k: scope[k] for k in ("organization_id", "installation_id",
            "installation_key_id", "installation_key_jkt", "enrollment_transaction_id")})


@pytest.fixture(params=VECTORS["proof_cases"], ids=lambda v: v["case_id"])
def exchange(request):
    vector = request.param
    verb = vector["operation"].removeprefix("authority-recovery-")
    payload = vector["request"]
    challenge = {"profile": contract()[2]["profile"], "challenge": vector["proof"]["challenge"],
        "request_digest": hashlib.sha256(canonical_request(payload)).hexdigest(), "expires_at": wire_time(NOW+timedelta(seconds=60))}
    transport = Transport(challenge, response_for(verb, payload), challenge_status=201)
    key = Key(REGISTERED, vector)
    return verb, vector, transport, key, InstallationAuthorityRecoveryClient(transport, key, clock=lambda: NOW)


def send(exchange, guard=lambda: None):
    verb, vector, _, _, client = exchange
    return getattr(client, verb)(vector["request"], before_send=guard)


def test_exact_published_proof_and_path(exchange):
    verb, vector, transport, key, client = exchange
    request = vector["request"]
    assert exact_request(request["recovery_id"], request["scope"]) == request
    assert scope_digest(request["scope"]) == vector["scope_digest"]
    assert send(exchange) == transport.result
    path = f"/v1/installations/{request['scope']['installation_id']}/authority-recovery"
    assert transport.calls == [("POST", path+"-proof-challenges", challenge_target(verb, request)),
        ("POST", path+":"+verb, {"request": request, "proof": vector["proof"]})]
    assert key.signed == 1


@pytest.mark.parametrize(("reply", "request_count", "signature_count"), [("challenge", 1, 0), ("operation", 2, 1)])
def test_challenge_and_operation_require_their_own_success_status(exchange, reply, request_count, signature_count):
    _, _, transport, key, _ = exchange
    if reply == "challenge":
        transport.challenge_status = 200
    else:
        transport.status = 201
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)
    assert len(transport.calls) == request_count
    assert key.signed == signature_count


@pytest.mark.parametrize("case", VECTORS["schema_cases"], ids=lambda c: c["case_id"])
def test_published_schema_vectors(case):
    schema = case["schema"].rsplit("/", 1)[-1].removesuffix(".schema.json")
    if case["valid"]:
        assert validate(schema, case["value"]) == case["value"]
    else:
        with pytest.raises(HostedGrantUnavailable):
            validate(schema, case["value"])


@pytest.mark.parametrize("field", ["installation_key_id", "installation_key_jkt"])
def test_wrong_key_before_transport(exchange, field):
    _, _, transport, key, _ = exchange
    key.reference = replace(key.reference, **{field: "different"})
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)
    assert not transport.calls and not key.signed


def test_changed_challenge_before_signing(exchange):
    _, _, transport, key, _ = exchange
    transport.challenge["request_digest"] = "0"*64
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)
    assert len(transport.calls) == 1 and not key.signed


@pytest.mark.parametrize("after", [1, 2, 3])
def test_rechecks_guard_before_key_challenge_signature_and_send(exchange, after):
    _, _, transport, key, _ = exchange
    calls = []
    def guard():
        calls.append(True)
        if len(calls) > after:
            raise HostedGrantUnavailable("Changed")
    with pytest.raises(HostedGrantUnavailable):
        send(exchange, guard)
    assert len(transport.calls) <= 1
    assert key.signed == (1 if after == 3 else 0)


@pytest.mark.parametrize("field", ["recovery_id", "scope_digest", "extra"])
def test_changed_reply_is_not_success(exchange, field):
    _, _, transport, _, _ = exchange
    transport.result[field] = "0"*64 if field == "scope_digest" else "changed"
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)


@pytest.mark.parametrize("reason", sorted(contract()[1]["refusal_status"]))
def test_closed_refusal_or_uncertainty(exchange, reason):
    _, _, transport, _, _ = exchange
    transport.result = {"profile": PROFILE, "reason": reason}
    transport.status = contract()[1]["refusal_status"][reason]
    if reason in {"rate_limited", "temporarily_unavailable"}:
        with pytest.raises(HostedGrantUnavailable):
            send(exchange)
    else:
        with pytest.raises(AuthorityRecoveryRefused) as exc:
            send(exchange)
        assert exc.value.reason == reason and exc.value.withdrawal == (reason in WITHDRAWALS)


def test_wrong_status_and_challenge_refusal_are_not_withdrawal(exchange):
    _, _, transport, _, client = exchange
    transport.result = {"profile": PROFILE, "reason": "grant_withdrawn"}
    transport.status = 409
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)
    client.transport = type("Refusal", (), {"request": lambda *_, **__: TransportResponse(
        403, json.dumps(transport.result).encode(), "application/json")})()
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)


def test_duplicate_reply_members_are_rejected(exchange):
    _, _, transport, _, _ = exchange
    transport.result = json.dumps(transport.result).replace('"profile":', '"profile": "duplicate", "profile":', 1).encode()
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)


@pytest.mark.parametrize("exchange", [
    vector for vector in VECTORS["proof_cases"] if vector["operation"] == "authority-recovery-prepare"
], indirect=True)
@pytest.mark.parametrize(("fault", "stage", "category", "request_count", "signature_count"), [
    ("guard", "challenge", "guard_refused", 0, 0),
    ("connect", "challenge", "connection_setup", 1, 0),
    ("http", "challenge", "http_failure", 1, 0),
    ("digest", "challenge", "invalid_response", 1, 0),
    ("budget", "challenge", "proof_budget_exhausted", 1, 0),
    ("sign", "proof", "proof_unavailable", 1, 0),
    ("timeout", "submit", "response_timeout", 2, 1),
    ("result", "result", "invalid_response", 2, 1),
])
def test_prepare_records_only_the_failed_stage_and_safe_category(
    exchange, fault, stage, category, request_count, signature_count, caplog, monkeypatch, tmp_path
):
    _, _, transport, key, client = exchange
    private_detail = "raw transport or proof detail"
    sent = []
    underlying_request = transport.request

    def request(method, path, *, json_body):
        sent.append(path)
        if fault == "connect":
            raise HostedTransportFailure(private_detail, stage=HostedFailureStage.CONNECTION_SETUP)
        if fault == "http":
            return TransportResponse(503, private_detail.encode(), "text/html")
        if fault == "timeout" and len(sent) == 2:
            raise HostedTransportFailure(private_detail, stage=HostedFailureStage.RESPONSE_TIMEOUT)
        return underlying_request(method, path, json_body=json_body)

    guard_calls = 0

    def guard():
        nonlocal guard_calls
        guard_calls += 1
        if fault == "guard" and guard_calls == 2:
            raise HostedGrantUnavailable(private_detail)

    def fail_to_sign(_):
        raise HostedGrantUnavailable(private_detail)

    monkeypatch.setattr(transport, "request", request)
    if fault == "digest":
        transport.challenge["request_digest"] = "0" * 64
    if fault == "budget":
        monkeypatch.setattr(client, "monotonic", iter([0, 600]).__next__)
    if fault == "sign":
        monkeypatch.setattr(key, "sign_challenge", fail_to_sign)
    if fault == "result":
        transport.result["scope_digest"] = "0" * 64

    monkeypatch.setattr(timing, "_sink", None)
    timing.record_to(tmp_path)
    caplog.set_level(logging.INFO, logger=timing.__name__)
    with pytest.raises(HostedGrantUnavailable):
        send(exchange, guard)
    assert len(sent) == request_count
    assert key.signed == signature_count
    expected = f"authority_recovery_prepare_failure stage={stage} category={category}"
    assert caplog.messages == [f"event={expected}"]
    lines = (tmp_path / timing.SINK_DIRECTORY / timing.SINK_FILENAME).read_text("ascii").splitlines()
    assert len(lines) == 1 and lines[0].split(" ", 1)[1] == expected


def test_successful_recovery_does_not_log_a_preparation_failure(exchange, caplog):
    caplog.set_level(logging.INFO, logger=timing.__name__)
    assert send(exchange) == exchange[2].result
    assert not caplog.messages


@pytest.mark.parametrize("exchange", [vector for vector in VECTORS["proof_cases"]
    if vector["operation"].removeprefix("authority-recovery-") == "deliver"], ids=lambda v: v["case_id"], indirect=True)
def test_delivery_cannot_change_creator_or_installation(exchange):
    verb, _, transport, _, _ = exchange
    assert verb == "deliver"
    result = copy.deepcopy(transport.result)
    for field in ("installation_id", "enrollment_transaction_id", "organization_id", "installation_key_id"):
        transport.result = {**result, field: "different"}
        with pytest.raises(HostedGrantUnavailable):
            send(exchange)
    transport.result = {**result, "creator_scope": {"kind": "none"},
        "grants": {k: v for k, v in result["grants"].items() if k != "creator_account_binding"}}
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)
