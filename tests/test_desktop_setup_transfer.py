"""Exact production relay/proof checks. Browser/TPM effects are explicit fixtures."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.persistence.auth import SQLiteAuthenticationStore
from app.persistence.onboarding import OnboardingJourneyStore, OnboardingJourneyUnavailable
from app.provisioning.setup_transfer import DesktopSetupTransfer,ENTRY_COOKIE
from app.provisioning.receiving import COOKIE as RECEIVING_COOKIE, install_receiving
from app.onboarding.admission import ScopedAdmissions
from app.onboarding.coordinator import DesktopOnboardingCoordinator
from app.persistence.installation_continuation import InstallationContinuationStore
from app.security.initial_handoff import canonical
from test_webauthn_routes import INSTANT

pytestmark=[pytest.mark.ci_tier("integration")]


@pytest.fixture
def receiver(tmp_path):
    clock=[INSTANT]
    store=SQLiteAuthenticationStore(tmp_path/"auth.sqlite3",clock=lambda:clock[0])
    journeys=OnboardingJourneyStore(store)
    keydata={"crv":"P-256","kty":"EC","x":"A"*43,"y":"A"*43}
    jkt=base64.urlsafe_b64encode(hashlib.sha256(canonical(keydata)).digest()).rstrip(b"=").decode()
    signed=[]
    key=SimpleNamespace(ensure_ready=lambda:SimpleNamespace(installation_key_id="ik1.test",installation_key_jkt=jkt,public_key_jwk=json.dumps(keydata)),
        sign_challenge=lambda data:(signed.append(data) or SimpleNamespace(installation_key_id="ik1.test",algorithm="ES256",signature=b"s"*64)))
    async def pause(_): pass
    enrollment=SimpleNamespace(store=store,journeys=journeys,client=SimpleNamespace(key=key),
        hosted_origin="https://setup.example",pause_for_transfer=pause)
    transfer=DesktopSetupTransfer(enrollment,hosted_setup_url="https://setup.example/public/onboarding/setup")
    admissions=ScopedAdmissions(store,stage="setup")
    native=admissions.consume(admissions.prepare_desktop(),role="desktop")
    owner=SimpleNamespace(contexts=InstallationContinuationStore(store), key=SimpleNamespace(reopen_existing=lambda:None))
    coordinator=DesktopOnboardingCoordinator(admissions,owner)
    app=FastAPI()
    install_receiving(app,coordinator,transfer)
    sessions=app.state.receiving_sessions
    session=sessions.consume(sessions.issue(native))
    client=TestClient(app,base_url="http://bridge.localhost:17871")
    headers={"cookie":f"{RECEIVING_COOKIE}={session.identifier}","origin":"http://bridge.localhost:17871","x-csrf-token":session.csrf_token}
    return SimpleNamespace(store=store,journeys=journeys,transfer=transfer,sessions=sessions,session=session,
        client=client,headers=headers,clock=clock,signed=signed,native=native,coordinator=coordinator)


def test_preparation_needs_current_cookie_origin_and_csrf_and_retains_exact_intent(receiver):
    path="/api/v1/provisioning/setup-transfer/prepare"
    assert receiver.client.post(path,json={"setup_code":"0123456789AB"},headers={"origin":"http://bridge.localhost:17871"}).status_code==401
    bad={**receiver.headers,"origin":"https://setup.example"}
    assert receiver.client.post(path,json={"setup_code":"0123456789AB"},headers=bad).status_code==403
    response=receiver.client.post(path,json={"setup_code":"0123456789AB"},headers=receiver.headers)
    assert response.status_code==200
    first=response.json()
    again=receiver.client.post(path,json={"setup_code":"0123456789AB"},headers=receiver.headers).json()
    assert first==again and first["journey_id"]==receiver.session.journey_id
    assert first["hosted_start_url"]=="https://setup.example/public/onboarding/setup/receive"
    assert receiver.client.post(path,content='{"setup_code":"0123456789AB","setup_code":"0123456789AB"}',headers={**receiver.headers,"content-type":"application/json"}).status_code==400


def test_public_relay_never_signs_without_original_local_cookie(receiver):
    prepared=receiver.client.post("/api/v1/provisioning/setup-transfer/prepare",json={"setup_code":"0123456789AB"},headers=receiver.headers).json()
    request=prepared["request"]
    challenge={"profile":"urn:bridge-clean:onboarding-proof:v1","challenge":"A"*43,"request_digest":hashlib.sha256(canonical(request)).hexdigest(),
        "expires_at":(INSTANT+timedelta(seconds=60)).isoformat(timespec="milliseconds").replace("+00:00","Z")}
    posted=receiver.client.post("/provisioning",data={"journey_id":receiver.session.journey_id,"request":json.dumps(request),"challenge":json.dumps(challenge)},headers={"origin":"https://setup.example"},follow_redirects=False)
    assert posted.status_code==303 and posted.headers["location"]=="/setup-code#journey="+receiver.session.journey_id
    assert not receiver.signed
    entry=posted.cookies[ENTRY_COOKIE]
    assert receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers={"cookie":f"{ENTRY_COOKIE}={entry}"}).status_code==401
    headers={**receiver.headers,"cookie":receiver.headers["cookie"]+f"; {ENTRY_COOKIE}={entry}"}
    context=receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=headers).json()
    assert context["state"]=="proof" and context["csrf_token"]==receiver.session.csrf_token
    proof=receiver.client.post("/api/v1/provisioning/setup-transfer/sign",json={"request":request,"challenge":challenge},headers=headers)
    assert proof.status_code==200 and len(receiver.signed)==1
    changed={**request,"setup_code":"1123456789AB"}
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/sign",json={"request":changed,"challenge":challenge},headers=headers).status_code==409
    receiver.clock[0]+=timedelta(seconds=60)
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/sign",json={"request":request,"challenge":challenge},headers=headers).status_code==409
    assert len(receiver.signed)==1


def test_relay_cannot_cross_journeys_or_accept_credentials_in_url(receiver):
    form={"journey_id":receiver.session.journey_id,"setup_code":"0123456789AB"}
    assert receiver.client.post("/provisioning?code=anything",data=form,headers={"origin":"https://setup.example"}).status_code==400
    assert receiver.client.post("/provisioning",data=form,headers={"origin":"https://other.example"}).status_code==400
    posted=receiver.client.post("/provisioning",data=form,headers={"origin":"https://setup.example"},follow_redirects=False)
    # A different source can never borrow the native presentation's receiver.
    from dataclasses import replace
    other=replace(receiver.session, identifier="Z"*43, journey_id=str(uuid4()))
    receiver.sessions._sessions[other.identifier]=other
    response=receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers={"cookie":f"{RECEIVING_COOKIE}={other.identifier}; {ENTRY_COOKIE}={posted.cookies[ENTRY_COOKIE]}"})
    assert response.status_code==401


def test_expired_workspaces_cleanup_does_not_exhaust_capacity_or_extend_session(receiver):
    old=receiver.session.journey_id
    for _ in range(127):
        receiver.journeys.open(str(uuid4()))
    receiver.clock[0]+=timedelta(minutes=30)
    assert receiver.journeys.get(old) is None
    fresh=receiver.journeys.open()
    assert fresh["journey_id"]!=old
    assert receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=receiver.headers).status_code==401
    with receiver.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM onboarding_journeys").fetchone()[0]==1
        assert connection.execute("SELECT count(*) FROM provisioning_browser_sessions").fetchone()[0]==0


def test_unknown_completion_preserves_only_bounded_receipt_provenance(receiver):
    journey=receiver.session.journey_id
    row=receiver.journeys.get(journey)
    receiver.journeys.update(journey,state="unknown",operation_id=receiver.journeys.new_operation(),scope_json='{"fixture":"scope"}',handoff_reference="A"*43,prepare_json='{"device":"unneeded"}')
    receiver.clock[0]+=timedelta(minutes=30)
    assert receiver.journeys.get(journey) is None
    with pytest.raises(OnboardingJourneyUnavailable):
        receiver.journeys.open()
    selected=receiver.journeys.renew_native_session(previous_journey_id=journey,
        identifier="r"*43, csrf="c"*43, existing_identifier=None, ttl_seconds=1800,
        authorize=lambda: None)
    new=receiver.journeys.require_current(selected["journey_id"])
    assert new["state"]=="unknown" and new["installation_id"]==row["installation_id"]
    assert new["handoff_reference"] is None and new["prepare_json"] is None
    assert new["recovery_deadline"]==(INSTANT+timedelta(minutes=60)).isoformat()
    receiver.clock[0]+=timedelta(minutes=30)
    assert receiver.journeys.get(new["journey_id"]) is None
    assert receiver.journeys.open()["state"]=="new"


def prepared_transfer(receiver):
    response=receiver.client.post("/api/v1/provisioning/setup-transfer/prepare",json={"setup_code":"0123456789AB"},headers=receiver.headers)
    assert response.status_code == 200, response.text
    return response.json()["request"]


def staged(receiver, payload):
    form={"journey_id":receiver.session.journey_id,**{key:value if isinstance(value,str) else json.dumps(value) for key,value in payload.items()}}
    result=receiver.client.post("/provisioning",data=form,headers={"origin":"https://setup.example"},follow_redirects=False)
    assert result.status_code == 303, result.text
    return {**receiver.headers,"cookie":receiver.headers["cookie"]+f"; {ENTRY_COOKIE}={result.cookies[ENTRY_COOKIE]}"}


def transfer_challenge(receiver,request,token="A"*43):
    return {"profile":"urn:bridge-clean:onboarding-proof:v1","challenge":token,
        "request_digest":hashlib.sha256(canonical(request)).hexdigest(),
        "expires_at":(receiver.clock[0]+timedelta(seconds=60)).isoformat(timespec="milliseconds").replace("+00:00","Z")}


def test_proof_response_loss_and_repeated_startup_never_resign_same_challenge(receiver):
    request=prepared_transfer(receiver)
    challenge=transfer_challenge(receiver,request)
    headers=staged(receiver,{"request":request,"challenge":challenge})
    signed=receiver.client.post("/api/v1/provisioning/setup-transfer/sign",json={"request":request,"challenge":challenge},headers=headers)
    assert signed.status_code==200 and "Max-Age=0" in signed.headers["set-cookie"]
    # Simulate loss of response, including Set-Cookie; old entry cookie survives.
    for _ in range(2):
        projected=receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=headers)
        assert projected.json()=={"state":"unconfirmed","journey_id":receiver.session.journey_id,"hosted_return_url":"https://setup.example/public/onboarding/setup"}
        replay=receiver.client.post("/api/v1/provisioning/setup-transfer/sign",json={"request":request,"challenge":challenge},headers=headers)
        assert replay.status_code==409
    # Re-posting public material cannot obtain a fresh signing operation.
    headers=staged(receiver,{"request":request,"challenge":challenge})
    assert receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=headers).json()["state"]=="unconfirmed"
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/sign",json={"request":request,"challenge":challenge},headers=headers).status_code==409
    assert len(receiver.signed)==1
    # A process restart retains the receipt even though transient relay keys rotate.
    receiver.transfer._entry_key=b"x"*32
    assert receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=headers).json()["state"]=="unconfirmed"
    # Explicit hosted recovery gets a fresh challenge for the SAME redemption.
    fresh=transfer_challenge(receiver,request,"B"*42+"A")
    headers=staged(receiver,{"request":request,"challenge":fresh})
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/sign",json={"request":request,"challenge":fresh},headers=headers).status_code==200
    assert len(receiver.signed)==2


def test_complete_local_hosted_relay_cycle_does_not_reenter_after_final_return(receiver):
    # Browser navigation/hosted authority are explicit fixtures here; every local
    # request below executes the production HTTP routes and durable store.
    headers=staged(receiver,{"setup_code":"0123456789AB"})
    assert receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=headers).json()["state"]=="code_entry"
    prepared=receiver.client.post("/api/v1/provisioning/setup-transfer/prepare",json={"setup_code":"0123456789AB"},headers=headers)
    assert prepared.status_code==200 and "Max-Age=0" in prepared.headers["set-cookie"]
    request=prepared.json()["request"]
    challenge=transfer_challenge(receiver,request)
    headers=staged(receiver,{"request":request,"challenge":challenge})
    assert receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=headers).json()["state"]=="proof"
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/sign",json={"request":request,"challenge":challenge},headers=headers).status_code==200
    value={"profile":"urn:bridge-clean:onboarding-continuation:v1","reference":"C"*42+"A","return_target":"desktop-setup",
        "expires_at":(INSTANT+timedelta(minutes=30)).isoformat(timespec="milliseconds").replace("+00:00","Z")}
    headers=staged(receiver,{"continuation":value})
    calls=[]
    async def prepare(journey):
        calls.append(journey)
        receiver.journeys.update(journey,state="waiting",handoff_reference="D"*43)
        return {"state":"waiting","journey_id":journey,"handoff_reference":"D"*43,"hosted_start_url":"https://setup.example/public/onboarding/setup/start"}
    receiver.transfer.enrollment.prepare=prepare
    assert receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=headers).json()["state"]=="continue"
    forwarded=receiver.client.post("/api/v1/provisioning/setup-transfer/continue",json={"continuation":value},headers=headers)
    assert forwarded.status_code==200 and "Max-Age=0" in forwarded.headers["set-cookie"]
    assert forwarded.json()["hosted_start_url"]=="https://setup.example/public/onboarding/setup/attach"
    workspace=forwarded.json()["workspace_journey_id"]
    assert workspace!=receiver.session.journey_id
    with receiver.store.database.read() as connection:
        assert connection.execute("SELECT source_journey_id FROM desktop_setup_journeys WHERE journey_id=?", (workspace,)).fetchone()[0]==receiver.session.journey_id
        assert connection.execute("SELECT count(*) FROM desktop_setup_operations").fetchone()[0]==0
    # Lost reply can be read and forwarded explicitly without preparing again.
    reread=receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=headers).json()
    assert reread=={"state":"continue_ready","journey_id":receiver.session.journey_id,"result":forwarded.json()}
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/continue",json={"continuation":value},headers=headers).status_code==409
    assert len(calls)==1
    receiver.journeys.update(receiver.session.journey_id,state="completed",scope_json='{"fixture":"independently-authorized"}')
    # Final local return and repeated application startup retain the normal state.
    for _ in range(2):
        assert receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=headers).json()=={"state":"none"}
        assert receiver.client.post("/api/v1/provisioning/setup-transfer/continue",json={"continuation":value},headers=headers).status_code==409
    assert len(calls)==1 and len(receiver.signed)==1


def test_lost_initial_prepare_preserves_continuation_without_automatic_replay(receiver):
    from app.security.hosted_grants import HostedGrantUnavailable
    prepared_transfer(receiver)
    value={"profile":"urn:bridge-clean:onboarding-continuation:v1","reference":"C"*42+"A","return_target":"desktop-setup",
        "expires_at":(INSTANT+timedelta(minutes=30)).isoformat(timespec="milliseconds").replace("+00:00","Z")}
    headers=staged(receiver,{"continuation":value})
    calls=[]
    async def lost_prepare(journey):
        calls.append(journey)
        receiver.journeys.update(journey,state="prepare-unknown")
        raise HostedGrantUnavailable("Response lost")
    receiver.transfer.enrollment.prepare=lost_prepare
    response=receiver.client.post("/api/v1/provisioning/setup-transfer/continue",json={"continuation":value},headers=headers)
    assert response.status_code==409 and "Max-Age=0" in response.headers["set-cookie"]
    assert json.loads(receiver.transfer._intent(receiver.session.journey_id)["continuation_json"])==value
    for _ in range(2):
        result=receiver.client.get("/api/v1/provisioning/setup-transfer/context",headers=receiver.headers)
        assert result.json()["state"]=="continue_pending"
    assert len(calls)==1

    async def recovered_prepare(journey):
        calls.append(journey)
        receiver.journeys.update(journey,state="waiting",handoff_reference="D"*43)
        return {"handoff_reference":"D"*43}
    receiver.transfer.enrollment.prepare=recovered_prepare
    response=receiver.client.post("/api/v1/provisioning/setup-transfer/resume",json={},headers=receiver.headers)
    assert response.status_code==200
    assert response.json()["continuation_reference"]==value["reference"]
    assert calls==[receiver.session.journey_id]*2
    assert json.loads(receiver.transfer._intent(receiver.session.journey_id)["continuation_json"])==value


def test_attachment_mapping_conflict_rolls_back_relay_consumption(receiver):
    prepared_transfer(receiver)
    value={"profile":"urn:bridge-clean:onboarding-continuation:v1","reference":"C"*42+"A","return_target":"desktop-setup",
        "expires_at":(INSTANT+timedelta(minutes=30)).isoformat(timespec="milliseconds").replace("+00:00","Z")}
    headers=staged(receiver,{"continuation":value})
    with receiver.store.database.transaction() as connection:
        connection.execute("INSERT INTO desktop_setup_journeys (journey_id,revision,created_at,source_journey_id) VALUES (?,0,?,?)",
            (str(uuid4()),INSTANT.timestamp(),str(uuid4())))
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/continue",json={"continuation":value},headers=headers).status_code==409
    with receiver.store.database.read() as connection:
        assert connection.execute("SELECT consumed_at FROM onboarding_transfer_relays").fetchone()[0] is None
        assert connection.execute("SELECT continuation_json FROM onboarding_transfer_intents").fetchone()[0] is None


def test_attachment_resume_keeps_deadline_and_requires_original_local_authority(receiver):
    prepared_transfer(receiver)
    value={"profile":"urn:bridge-clean:onboarding-continuation:v1","reference":"C"*42+"A","return_target":"desktop-setup",
        "expires_at":(INSTANT+timedelta(seconds=30)).isoformat(timespec="milliseconds").replace("+00:00","Z")}
    headers=staged(receiver,{"continuation":value})
    from app.security.hosted_grants import HostedGrantUnavailable
    async def lost(_):
        raise HostedGrantUnavailable("lost")
    receiver.transfer.enrollment.prepare=lost
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/continue",json={"continuation":value},headers=headers).status_code==409
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/resume",json={},headers={"origin":"http://bridge.localhost:17871"}).status_code==401
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/resume",json={},headers={**receiver.headers,"origin":"https://setup.example"}).status_code==403
    receiver.clock[0]+=timedelta(seconds=30)
    assert receiver.client.post("/api/v1/provisioning/setup-transfer/resume",json={},headers=receiver.headers).status_code==409


def test_receiving_entry_is_one_use_and_subordinate_to_current_native_presentation(receiver):
    from app.onboarding.admission import csrf_token
    from app.onboarding.admission_routes import COOKIE_NAMES, ORIGIN
    native = receiver.native
    headers = {"cookie": f"{COOKIE_NAMES['desktop']}={native.identifier}", "origin": ORIGIN,
        "x-desktop-generation": native.generation, "x-desktop-csrf": csrf_token(native.identifier)}
    path = "/api/v1/desktop/setup-transfer/entry"
    assert receiver.client.post(path, json={}, headers=receiver.headers).status_code == 401
    assert receiver.client.post(path, json={}, headers=headers | {"origin": "https://setup.example"}).status_code == 403
    code = receiver.client.post(path, json={}, headers=headers).json()["entry_code"]
    # Opening a replacement receiving page retires its previous cookie immediately.
    assert receiver.client.get("/api/v1/setup-code/session", headers=receiver.headers).status_code == 401
    consumed = receiver.client.post("/api/v1/setup-code/consume", json={"code": code}, headers={"origin": ORIGIN})
    assert consumed.status_code == 200
    cookie = consumed.headers["set-cookie"]
    assert all(value in cookie for value in ["HttpOnly", "Secure", "SameSite=strict"])
    assert receiver.client.post("/api/v1/setup-code/consume", json={"code": code}, headers={"origin": ORIGIN}).status_code == 401
    receiving_headers = {"cookie": cookie.split(";", 1)[0]}
    assert receiver.client.get("/api/v1/setup-code/session", headers=receiving_headers).status_code == 200
    receiver.coordinator.admissions.consume(receiver.coordinator.admissions.prepare_desktop(), role="desktop")
    assert receiver.client.get("/api/v1/setup-code/session", headers=receiving_headers).status_code == 401


def test_receiving_entry_and_session_deadlines_do_not_slide(receiver):
    from fastapi import HTTPException
    code = receiver.sessions.issue(receiver.native)
    receiver.clock[0] += timedelta(seconds=60)
    with pytest.raises(HTTPException):
        receiver.sessions.consume(code)
    new_code = receiver.sessions.issue(receiver.native)
    session = receiver.sessions.consume(new_code)
    assert session.expires_at == receiver.session.expires_at
    receiver.clock[0] += timedelta(minutes=30)
    assert receiver.client.get("/api/v1/setup-code/session",
        headers={"cookie": f"{RECEIVING_COOKIE}={session.identifier}"}).status_code == 401


def test_receiving_rejects_duplicate_cookies_queries_and_hostile_browser_context(receiver):
    path = "/api/v1/setup-code/session"
    assert receiver.client.get(path, headers=receiver.headers | {
        "cookie": receiver.headers["cookie"] + "; " + receiver.headers["cookie"]}).status_code == 401
    assert receiver.client.get(path + "?code=untrusted", headers=receiver.headers).status_code == 400
    assert receiver.client.get(path, headers=receiver.headers | {"host": "localhost:17871"}).status_code == 421
    assert receiver.client.get(path, headers=receiver.headers | {"sec-fetch-site": "cross-site"}).status_code == 403


def test_receiving_rechecks_native_generation_after_pausing_before_key_access(receiver):
    async def pause(_):
        receiver.coordinator.admissions.consume(receiver.coordinator.admissions.prepare_desktop(), role="desktop")
    receiver.transfer.enrollment.pause_for_transfer = pause
    receiver.transfer.enrollment.client.key.ensure_ready = lambda: pytest.fail("Retired receiving page reached hardware")
    response = receiver.client.post("/api/v1/provisioning/setup-transfer/prepare",
        json={"setup_code": "0123456789AB"}, headers=receiver.headers)
    assert response.status_code == 401
    assert receiver.transfer._intent(receiver.session.journey_id) is None


def test_transfer_pins_browser_release_origin_independently_of_api_origin(receiver):
    receiver.transfer.enrollment.hosted_origin = "https://api.example"
    transfer = DesktopSetupTransfer(receiver.transfer.enrollment,
        hosted_setup_url="https://browser.example/public/onboarding/setup")
    assert transfer.return_url == "https://browser.example/public/onboarding/setup"
    assert transfer.hosted_origin == "https://browser.example"
    with pytest.raises(ValueError):
        DesktopSetupTransfer(receiver.transfer.enrollment, hosted_setup_url="https://browser.example/retired")
