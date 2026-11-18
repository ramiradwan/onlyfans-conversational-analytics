"""Exercise the published v2/v3 schemas with independently constructed ES256 proofs."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature

from app.persistence.auth import InstallationKeyReference
from app.security.hosted_grants import HostedGrantUnavailable, TransportResponse
from app.security.installation_key import InstallationProof
from app.security.protected_browser_recovery import (
    CEREMONY, AUTHENTICATION, CEREMONY_PROFILE, AUTHENTICATION_PROFILE,
    ProtectedCeremonyClient, ProtectedAuthenticationClient, ProtectedBrowserRefused,
    recovery_binding, ceremony_request, authentication_request, contract,
)
from test_installation_authority_recovery_client import VECTORS, wire_time
from test_source_replacement_client import NOW

pytestmark = [pytest.mark.ci_tier("fast"), pytest.mark.contract_integrity]
P256_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
CEREMONY_ID = "0198a1b2-c3d4-7300-8000-000000000061"
OPERATION_ID = "0198a1b2-c3d4-7300-8000-000000000062"
CHILD = "0198a1b2-c3d4-7300-8000-000000000063"
PREVIOUS = "0198a1b2-c3d4-7300-8000-000000000064"
EXPIRY = wire_time(NOW + timedelta(minutes=20))
CHALLENGE = bytes(range(32))


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class Key:
    def __init__(self):
        self.private = ec.generate_private_key(ec.SECP256R1())
        numbers = self.private.public_key().public_numbers()
        jwk = {"crv": "P-256", "kty": "EC", "x": b64(numbers.x.to_bytes(32, "big")),
            "y": b64(numbers.y.to_bytes(32, "big"))}
        raw = hashlib.sha256(canonical(jwk)).digest()
        kid = "ik1." + b64(raw[:16])
        self.reference = InstallationKeyReference("fixture", "fixture", "ES256", kid, b64(raw),
            json.dumps({**jwk, "kid": kid}), NOW, NOW)
        self.signed = []

    def ensure_ready(self):
        pytest.fail("Protected proof attempted key creation")

    def reopen_existing(self):
        return self.reference

    def sign_challenge(self, message):
        self.signed.append(message)
        r, s = decode_dss_signature(self.private.sign(message, ec.ECDSA(hashes.SHA256())))
        return InstallationProof(self.reference.installation_key_id, "ES256",
            r.to_bytes(32, "big")+min(s, P256_ORDER-s).to_bytes(32, "big"))


def binding_for(key):
    request = copy.deepcopy(VECTORS["proof_cases"][0]["request"])
    request["scope"].update(installation_key_id=key.reference.installation_key_id,
        installation_key_jkt=key.reference.installation_key_jkt)
    return recovery_binding(request, {"reference": "A"*43, "expires_at": EXPIRY},
        attempt_id=b64(b"b"*32), completion={"mode": "system-browser"})


def ceremony_response(verb, request, *, domain="awaiting-authentication", presentation="active"):
    base = {"profile": CEREMONY_PROFILE, "operation_id": request["operation_id"]}
    common = {"ceremony_id": CEREMONY_ID, "binding_digest": digest(request["binding"]), "expires_at": EXPIRY}
    entry = {"entry_reference": b64(b"e"*32), "completion": {"mode": "system-browser"}}
    if verb == "prepare":
        return {**base, **common, **entry}
    view = {**base, "status": "found", "snapshot": {**common, "operation_id": request["operation_id"],
        "attempt_id": request["binding"]["attempt_id"], "revision": 1, "presentation_state": presentation,
        "domain_status": domain}}
    if presentation == "active" and domain == "awaiting-authentication":
        view["entry"] = entry
    return view


def auth_response(request, state, *, child=None, actionable=True):
    return {"profile": AUTHENTICATION_PROFILE, "operation_id": request["operation_id"],
        "ceremony_id": request["ceremony_id"], "binding_digest": digest(request["binding"]), "expires_at": EXPIRY,
        "state": state, "choices": {k: k=="default" for k in ("google","microsoft-personal","default","telegram","tiktok")},
        "actions": {"select": actionable and state in {"selection-required", "retired"},
            "cancel": actionable and state=="selected", "open_browser": actionable and state=="selected",
            "switch_account": False}, "authentication_attempt": None if child is None else {
                "authentication_attempt_id": child, "provider": "default", "mode": "sign-in"}}


class Transport:
    def __init__(self, family, verb, key, request, result):
        self.family, self.verb, self.key, self.request_value = family, verb, key, request
        self.result, self.status = result, 200
        self.calls = []
        self.challenge_change = None
        if family == CEREMONY:
            self.challenge_path = "/v2/onboarding/browser-ceremony-proof-challenges"
            self.path = "/v2/onboarding/browser-ceremonies:"+verb
            self.domain = b"BRIDGE-CLEAN-BROWSER-CEREMONY-PROOF-V2\x00"
            self.profile = "urn:bridge-clean:browser-ceremony-proof:v2"
            self.purpose = "browser-ceremony-"+verb
            self.audience = b"urn:bridge-clean:commercial-control-plane:browser-ceremony"
        else:
            self.challenge_path = "/v3/onboarding/browser-authentication-selection-proof-challenges"
            self.path = "/v3/onboarding/browser-authentication-selections:"+verb
            self.domain = b"BRIDGE-CLEAN-BROWSER-AUTHENTICATION-SELECTION-PROOF-V3\x00"
            self.profile = "urn:bridge-clean:browser-authentication-selection-proof:v3"
            self.purpose = "browser-authentication-"+verb
            self.audience = b"urn:bridge-clean:commercial-control-plane:browser-authentication-selection"

    def request(self, method, path, *, json_body):
        self.calls.append((method, path, copy.deepcopy(json_body)))
        assert method == "POST"
        if path == self.challenge_path:
            assert json_body == {"profile": self.profile,
                "target": {"operation": self.purpose, "request": self.request_value}}
            result = {"profile": self.profile, "challenge": b64(CHALLENGE),
                "request_digest": digest(self.request_value), "expires_at": wire_time(NOW+timedelta(seconds=60))}
            if self.challenge_change:
                result.update(self.challenge_change)
            return TransportResponse(200, json.dumps(result).encode(), "application/json")
        assert path == self.path and json_body["request"] == self.request_value
        proof = json_body["proof"]
        assert set(proof)=={"signature","challenge"} and proof["challenge"] == b64(CHALLENGE)
        fields = (CHALLENGE, b"POST", path.encode(), hashlib.sha256(canonical(self.request_value)).digest(),
            self.purpose.encode(), base64.urlsafe_b64decode(self.key.reference.installation_key_jkt+"="), self.audience)
        expected = self.domain + b"".join(len(f).to_bytes(4,"big")+f for f in fields)
        assert self.key.signed[-1] == expected
        signature = base64.urlsafe_b64decode(proof["signature"]+"==")
        r,s = int.from_bytes(signature[:32],"big"),int.from_bytes(signature[32:],"big")
        assert 0<s<=P256_ORDER//2
        self.key.private.public_key().verify(encode_dss_signature(r,s), expected, ec.ECDSA(hashes.SHA256()))
        if isinstance(self.result, Exception):
            raise self.result
        raw = self.result if isinstance(self.result, bytes) else json.dumps(self.result).encode()
        return TransportResponse(self.status, raw, "application/json")


@pytest.fixture(params=[(CEREMONY,v) for v in ("prepare","read","recover","cancel")]+[
    (AUTHENTICATION,v) for v in ("read","select","cancel")], ids=lambda item: item[0]+"-"+item[1])
def exchange(request):
    family, verb = request.param
    key = Key()
    ceremony = ceremony_request(OPERATION_ID, binding_for(key),
        ceremony_id=None if family==CEREMONY and verb=="prepare" else CEREMONY_ID)
    if family==CEREMONY:
        value=ceremony
        result=ceremony_response(verb,value,presentation="retired" if verb=="cancel" else "active")
        client=ProtectedCeremonyClient
    else:
        value=authentication_request(verb,ceremony,attempt_id=None if verb=="read" else CHILD)
        result=auth_response(value,"selection-required" if verb=="read" else "retired" if verb=="cancel" else "selected",
            child=None if verb=="read" else CHILD)
        client=ProtectedAuthenticationClient
    transport=Transport(family,verb,key,value,result)
    return family,verb,value,transport,key,client(transport,key,clock=lambda:NOW)


def send(exchange, *, guard=lambda:None, request=None, expiry=EXPIRY):
    family,verb,value,_,_,client=exchange
    return client.execute(verb, value if request is None else request, before_send=guard,
        expires_at=None if family==CEREMONY and verb=="prepare" else expiry)


def test_exact_protected_proofs(exchange):
    _,_,_,transport,key,_=exchange
    assert send(exchange)==transport.result
    assert len(transport.calls)==2 and len(key.signed)==1


@pytest.mark.parametrize("field",["request_digest","scope_digest","installation_id","reference","purpose","actor"])
def test_reject_changed_binding_before_network(exchange,field):
    _,_,value,transport,key,_=exchange
    value=copy.deepcopy(value)
    if field in {"request_digest","scope_digest","reference"}:
        value["binding"]["source"][field]="bad"
    else:
        value["binding"][field]="bad"
    with pytest.raises(HostedGrantUnavailable):
        send(exchange,request=value)
    assert not transport.calls and not key.signed


def test_wrong_installed_key_and_missing_guard(exchange):
    _,_,_,transport,key,_=exchange
    with pytest.raises(HostedGrantUnavailable):
        send(exchange,guard=None)
    key.reference=replace(key.reference,installation_key_jkt="A"*43)
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)
    assert not transport.calls and not key.signed


def test_lost_reply_never_retries_or_dispatches(exchange):
    _,_,_,transport,_,_=exchange
    transport.result=TimeoutError()
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)
    assert len(transport.calls)==2


def test_guard_rechecked_before_signature_and_send(exchange):
    _,_,_,transport,key,_=exchange
    seen=[]
    def guard():
        seen.append(True)
        if len(seen)>3:
            raise HostedGrantUnavailable("Changed")
    with pytest.raises(HostedGrantUnavailable):
        send(exchange,guard=guard)
    assert len(transport.calls)==1 and len(key.signed)==1


def test_changed_challenge_rejected_before_signing(exchange):
    _,_,_,transport,key,_=exchange
    transport.challenge_change={"request_digest":"0"*64}
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)
    assert len(transport.calls)==1 and not key.signed


@pytest.mark.parametrize("field",["operation_id","binding_digest","extra"])
def test_response_scope_changes_rejected(exchange,field):
    family,verb,_,transport,_,_=exchange
    target=transport.result if family==AUTHENTICATION or verb=="prepare" or field=="operation_id" else transport.result["snapshot"]
    target[field]="0"*64 if field=="binding_digest" else PREVIOUS
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)


def test_refusal_requires_correct_family_and_status(exchange):
    family,_,_,transport,_,_=exchange
    transport.result={"profile":contract(family).profile["profile"],"reason":"authorization_denied"}
    transport.status=403
    with pytest.raises(ProtectedBrowserRefused) as err:
        send(exchange)
    assert err.value.reason=="authorization_denied"
    transport.status=409
    with pytest.raises(HostedGrantUnavailable):
        send(exchange)


def test_protected_selection_never_switches_or_chooses_federated_route():
    key=Key()
    ceremony=ceremony_request(OPERATION_ID,binding_for(key),ceremony_id=CEREMONY_ID)
    request=authentication_request("select",ceremony,attempt_id=CHILD,previous_id=PREVIOUS)
    transport=Transport(AUTHENTICATION,"select",key,request,auth_response(request,"selected",child=CHILD))
    client=ProtectedAuthenticationClient(transport,key,clock=lambda:NOW)
    with pytest.raises(ValueError):
        client.execute("switch",request,before_send=lambda:None,expires_at=EXPIRY)
    for change in ({"provider":"google"},{"expected_previous_attempt_id":CHILD},{"actor":"caller"}):
        with pytest.raises(HostedGrantUnavailable):
            client.execute("select",{**request,**change},before_send=lambda:None,expires_at=EXPIRY)
    assert not transport.calls


def test_ceremony_commit_is_fact_only_and_retirement_cannot_return_entry():
    key=Key()
    request=ceremony_request(OPERATION_ID,binding_for(key),ceremony_id=CEREMONY_ID)
    transport=Transport(CEREMONY,"read",key,request,ceremony_response("read",request,domain="committed",presentation="retired"))
    client=ProtectedCeremonyClient(transport,key,clock=lambda:NOW+timedelta(minutes=40))
    assert client.execute("read",request,before_send=lambda:None,expires_at=EXPIRY)["snapshot"]["domain_status"]=="committed"
    transport.result["snapshot"]["result"]={"grants":{}}
    with pytest.raises(HostedGrantUnavailable):
        client.execute("read",request,before_send=lambda:None,expires_at=EXPIRY)


def test_protected_selection_wrong_child_and_action_state():
    key=Key()
    ceremony=ceremony_request(OPERATION_ID,binding_for(key),ceremony_id=CEREMONY_ID)
    request=authentication_request("select",ceremony,attempt_id=CHILD)
    transport=Transport(AUTHENTICATION,"select",key,request,auth_response(request,"selected",child=PREVIOUS))
    client=ProtectedAuthenticationClient(transport,key,clock=lambda:NOW)
    with pytest.raises(HostedGrantUnavailable):
        client.execute("select",request,before_send=lambda:None,expires_at=EXPIRY)
    transport.result=auth_response(request,"admitted",child=CHILD)
    transport.result["actions"]["open_browser"]=True
    with pytest.raises(HostedGrantUnavailable):
        client.execute("select",request,before_send=lambda:None,expires_at=EXPIRY)
