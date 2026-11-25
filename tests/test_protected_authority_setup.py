"""Real desktop coordinator admission of the protected recovery parent."""
from __future__ import annotations

import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.onboarding.admission import ScopedAdmissions
from app.onboarding.coordinator import CommandRefused, DesktopOnboardingCoordinator, OperationResult
from app.onboarding.protected_authority_setup import EXPIRY_READ_MARGIN, ProtectedAuthoritySetupOwner
from app.onboarding.protected_browser_recovery import ProtectedBrowserRecoveryEffects
from app.persistence.auth import SQLiteAuthenticationStore, RevocationKey, RevocationScopeType
from app.persistence.protected_browser_recovery import ProtectedBrowserRecoveries
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.installation_authority_recovery import AuthorityRecoveryRefused
from test_desktop_coordinator import Owner, command
from test_installation_authority_recovery_client import response_for
from test_installation_authority_recovery_owner import installed, journal, signed_delivery
from test_protected_browser_recovery_client import ceremony_response, auth_response

pytestmark=[pytest.mark.ci_tier("integration"),pytest.mark.windows_compat]


class Base(Owner):
    async def stop(self):
        pass


def build(case, *, store=None, state=None, stage="setup", enabled=True):
    store=store or case.store
    state=state if state is not None else {"calls":[]}
    base=Base()
    recovery=journal(case,store)
    class Client:
        def prepare(self,request,*,before_send):
            before_send()
            state["calls"].append(("authority","prepare",copy.deepcopy(request)))
            if state.pop("lose_prepare",False):
                raise HostedGrantUnavailable("Lost prepare")
            if state.get("refusal"):
                raise AuthorityRecoveryRefused(state["refusal"])
            if state.get("committed_prepare"):
                result=response_for("prepare",request)
                result.pop("source_reference")
                result.pop("expires_at")
                result.update(status="committed",committed_at=case.clock[0].isoformat(timespec="milliseconds").replace("+00:00","Z"))
                return result
            return response_for("prepare",request)
        def deliver(self,request,*,before_send):
            before_send()
            state["calls"].append(("authority","deliver",copy.deepcopy(request)))
            if state.pop("lose_delivery",False):
                raise HostedGrantUnavailable("Lost delivery")
            return state["result"]
    class Ceremony:
        def execute(self,verb,request,*,before_send,expires_at):
            before_send()
            state["calls"].append(("ceremony",verb,copy.deepcopy(request)))
            if verb=="prepare" and state.pop("lose_ceremony",False):
                raise HostedGrantUnavailable("Lost ceremony")
            if verb=="read" and state.pop("lose_ceremony_read",False):
                raise HostedGrantUnavailable("Lost ceremony read")
            if verb=="read" and state.get("ceremony_unavailable"):
                return {"profile":request["profile"],"operation_id":request["operation_id"],"status":"unavailable"}
            domain=state.get("ceremony_domain","committed" if state.get("committed") else "awaiting-authentication")
            presentation=state.get("ceremony_presentation","active")
            return ceremony_response(verb,request,domain=domain,presentation=presentation)
    class Auth:
        def execute(self,verb,request,*,before_send,expires_at):
            before_send()
            state["calls"].append(("auth",verb,copy.deepcopy(request)))
            if verb=="select":
                state["child"]=request["authentication_attempt_id"]
                state["auth_state"]="selected"
                if state.pop("lose_select",False):
                    raise HostedGrantUnavailable("Lost selection")
            elif verb=="cancel":
                assert request["authentication_attempt_id"]==state["child"]
                state["auth_state"]="admitted" if state.get("commit_on_cancel") else "retired"
                if state.pop("lose_cancel",False):
                    raise HostedGrantUnavailable("Lost cancellation")
            elif verb=="read" and state.pop("lose_auth_read",False):
                raise HostedGrantUnavailable("Lost authentication read")
            return auth_response(request,state.get("auth_state","selection-required"),child=state.get("child"))
    class Verifier:
        def _verified_reference(self,*args,**kwargs):
            return state["verifier"]._verified_reference(*args,**kwargs)
    browser=ProtectedBrowserRecoveryEffects(ProtectedBrowserRecoveries(recovery),Ceremony(),Auth())
    owner=ProtectedAuthoritySetupOwner(base,store,**(dict(recovery=recovery,client=Client(),browser=browser,verifier=Verifier()) if enabled else {}))
    admissions=ScopedAdmissions(store,stage=stage)
    coordinator=DesktopOnboardingCoordinator(admissions,owner)
    return coordinator,owner,base,state


def connect(coordinator):
    return coordinator.admissions.consume(coordinator.admissions.prepare_desktop(),role="desktop")


def recovery_command(coordinator,session):
    snapshot=coordinator.read(session)
    assert snapshot["authority_recovery"]=="available"
    assert snapshot["allowed_actions"]==["account.connect"]
    assert snapshot["next_task"]=="account-connection" and snapshot["authentication"] is None
    return command(snapshot,"account.connect")|{"recover_authority":True}


def saved(coordinator,command_id):
    with coordinator.store.database.read() as c:
        return dict(c.execute("SELECT * FROM desktop_setup_operations WHERE operation_id=?",(command_id,)).fetchone())


def rid(operation):
    return json.loads(operation["owner_scope_json"])["protected_recovery_intent"]["recovery_id"]


@pytest.mark.parametrize("stage",["setup","runtime"])
@pytest.mark.asyncio
async def test_exact_desktop_admission_requires_explicit_recovery_and_read_is_pure(installed,stage):
    case=installed
    coordinator,owner,base,state=build(case,stage=stage)
    session=connect(coordinator)
    request=recovery_command(coordinator,session)
    with case.store.database.read() as c:
        assert c.execute("SELECT count(*) FROM installation_authority_recoveries").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM protected_recovery_desktop_admissions").fetchone()[0]==0
    with pytest.raises(CommandRefused):
        coordinator._accept(session,{k:v for k,v in request.items() if k!="recover_authority"})
    with pytest.raises(ValueError):
        await coordinator.command(session,{**request,"provider":"default"})
    operation,fresh=coordinator._accept(session,request)
    assert fresh and not state["calls"] and not base.admissions
    with case.store.database.read() as c:
        original=owner.recovery.row(c,rid(operation))
        assert original["state"]=="reserved"
        assert c.execute("SELECT count(*) FROM protected_recovery_desktop_admissions").fetchone()[0]==1
    assert coordinator._accept(session,request)[1] is False
    assert coordinator.read(session)["authority_recovery"]=="preparing"
    assert not coordinator.read(session)["allowed_actions"]
    result=await owner.execute(operation,reconcile=False)
    assert result.state=="waiting"
    coordinator._settle(operation,result)
    assert coordinator.read(session)["authority_recovery"]=="sign-in-required"
    assert [item[:2] for item in state["calls"]]==[("authority","prepare"),("ceremony","prepare"),("auth","read")]
    assert not owner.automatic_recovery(operation)
    assert coordinator.read(session)["allowed_actions"]==["auth.select"]
    assert not base.calls
    with case.store.database.read() as c:
        assert c.execute("SELECT count(*) FROM protected_recovery_authentication_children").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM desktop_browser_tasks").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0]==len(case.grants)
    await owner.stop()


@pytest.mark.asyncio
async def test_restart_after_lost_prepare_and_ceremony_keeps_exact_desktop_and_hosted_ids(installed):
    case=installed
    coordinator,owner,_,state=build(case)
    state.update(lose_prepare=True,lose_ceremony=True)
    session=connect(coordinator)
    request=recovery_command(coordinator,session)
    op,_=coordinator._accept(session,request)
    result=await owner.execute(op,reconcile=False)
    assert result.state=="outcome_unknown"
    coordinator._settle(op,result)
    await owner.stop()
    case.clock[0]+=timedelta(seconds=16)
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    coordinator,owner,base,state=build(case,store=reopened,state=state)
    session=connect(coordinator)
    assert coordinator.read(session)["authority_recovery"]=="preparing"
    assert coordinator.receipt(session,op["operation_id"])["state"]=="outcome_unknown"
    result=await owner.execute(saved(coordinator,op["operation_id"]),reconcile=True)
    assert result.state=="outcome_unknown"
    assert state["calls"][0][2]==state["calls"][1][2]
    await owner.stop()
    coordinator,owner,base,state=build(case,store=reopened,state=state)
    op=saved(coordinator,op["operation_id"])
    result=await owner.execute(op,reconcile=True)
    assert result.state=="waiting"
    coordinator._settle(op,result)
    assert [item[:2] for item in state["calls"]]==[("authority","prepare"),("authority","prepare"),("ceremony","prepare"),("ceremony","read")]
    assert state["calls"][2][2]==state["calls"][3][2]
    case.clock[0]+=timedelta(seconds=16)
    assert (await owner.execute(op,reconcile=True)).state=="waiting"
    assert coordinator.read(session)["authority_recovery"]=="sign-in-required"
    assert not owner.automatic_recovery(op) and not base.calls
    await owner.stop()


@pytest.mark.asyncio
async def test_exhausted_preparation_exposes_blocked_with_the_original_command_pending(installed):
    case = installed
    coordinator, owner, _, state = build(case)
    session = connect(coordinator)
    command = recovery_command(coordinator, session)
    operation, _ = coordinator._accept(session, command)
    original_deadline = operation["expires_at"]
    assert coordinator.read(session)["authority_recovery"] == "preparing"

    for reconcile in (False, True):
        state["lose_prepare"] = True
        result = await owner.execute(operation, reconcile=reconcile)
        assert result.state == "outcome_unknown"
        coordinator._settle(operation, result)
        operation = saved(coordinator, command["operation_id"])
        case.clock[0] += timedelta(seconds=16)

    snapshot = coordinator.read(session)
    assert snapshot["authority_recovery"] == "blocked"
    assert snapshot["allowed_actions"] == []
    assert snapshot["pending_operations"] == [{
        "operation_id": command["operation_id"],
        "action": "account.connect",
        "state": "outcome_unknown",
        "reason": "operation_unconfirmed",
    }]
    assert operation["expires_at"] == original_deadline
    assert state["calls"] == [
        ("authority", "prepare", state["calls"][0][2]),
        ("authority", "prepare", state["calls"][0][2]),
    ]
    await owner.stop()


@pytest.mark.asyncio
async def test_original_commit_delivers_verified_grants_through_coordinator_and_settles(installed):
    case=installed
    coordinator,owner,_,state=build(case)
    session=connect(coordinator)
    request=recovery_command(coordinator,session)
    op,_=coordinator._accept(session,request)
    assert (await owner.execute(op,reconcile=False)).state=="waiting"
    with case.store.database.transaction() as c:
        # The protected browser service has already independently verified
        # a current child's admission. This test injects the validated
        # committed ceremony fact; actual browser dispatch remains unwired.
        original=owner.recovery.row(c,rid(op))
        hosted=json.loads(original["request_json"])
        guard=lambda **kw:owner._authorize(op,**kw)
        child_request=owner.browser.journal.claim_select(c,rid(op),authorize=guard)
        aid=child_request["authentication_attempt_id"]
        owner.browser.journal.record_authentication(c,rid(op),"select",child_request,auth_response(child_request,"selected",child=aid),authorize=guard)
        current=owner.browser.journal.auth_read(c,rid(op),authorize=guard)
        owner.browser.journal.record_authentication(c,rid(op),"read",current,auth_response(current,"admitted",child=aid),authorize=guard)
    case.clock[0]+=timedelta(seconds=16)
    state["verifier"],state["result"]=signed_delivery(case,hosted)
    state["lose_delivery"]=True
    assert (await owner.execute(op,reconcile=True)).state=="outcome_unknown"
    await owner.stop()
    case.clock[0]+=timedelta(seconds=16)
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    coordinator,owner,base,state=build(case,store=reopened,state=state)
    session=connect(coordinator)
    op=saved(coordinator,op["operation_id"])
    result=await owner.execute(op,reconcile=True)
    assert result.state=="completed"
    coordinator._settle(op,result)
    assert coordinator.receipt(session,op["operation_id"])["state"]=="completed"
    calls=[item[2] for item in state["calls"] if item[:2]==("authority","deliver")]
    assert calls==[hosted,hosted]
    assert "authority_recovery" not in coordinator.read(session)
    with reopened.database.read() as c:
        assert owner.recovery.row(c,rid(op))["state"]=="admitted"
        assert c.execute("SELECT count(*) FROM verified_grant_references WHERE revoked_at IS NULL").fetchone()[0]==len(case.grants)
    assert not base.calls
    await owner.stop()


@pytest.mark.asyncio
async def test_disabled_restart_fences_retained_work_and_no_fallback(installed):
    case=installed
    coordinator,owner,_,state=build(case)
    session=connect(coordinator)
    request=recovery_command(coordinator,session)
    op,_=coordinator._accept(session,request)
    await owner.stop()
    coordinator,owner,base,state=build(case,state=state,enabled=False)
    session=connect(coordinator)
    assert coordinator.read(session)["authority_recovery"]=="blocked"
    assert not coordinator.read(session)["allowed_actions"]
    assert not owner.automatic_recovery(op)
    assert (await owner.execute(op,reconcile=True)).state=="outcome_unknown"
    assert not base.calls and not state["calls"]
    await owner.stop()


@pytest.mark.asyncio
async def test_withdrawal_blocks_replacement_and_ordinary_enrollment(installed):
    case=installed
    coordinator,owner,base,state=build(case)
    state["refusal"]="installation_revoked"
    session=connect(coordinator)
    request=recovery_command(coordinator,session)
    op,_=coordinator._accept(session,request)
    result=await owner.execute(op,reconcile=False)
    assert result==OperationResult("refused","authority_unavailable")
    coordinator._settle(op,result)
    snapshot=coordinator.read(session)
    assert snapshot["authority_recovery"]=="blocked" and not snapshot["allowed_actions"]
    assert not owner.automatic_recovery(op)
    with pytest.raises(CommandRefused):
        coordinator._accept(session,command(snapshot,"account.connect"))
    assert not base.admissions and not base.calls and len(state["calls"])==1
    await owner.stop()


@pytest.mark.asyncio
async def test_changed_local_grant_before_first_send_is_fenced(installed):
    case=installed
    coordinator,owner,_,state=build(case)
    session=connect(coordinator)
    request=recovery_command(coordinator,session)
    op,_=coordinator._accept(session,request)
    with case.store.database.transaction() as c:
        case.store._revoke_in_transaction(c,RevocationKey(RevocationScopeType.INSTALLATION,case.scope["installation_id"]),reason="test")
    assert (await owner.execute(op,reconcile=False)).state=="outcome_unknown"
    assert coordinator.read(session)["authority_recovery"]=="blocked"
    assert not state["calls"]
    await owner.stop()

@pytest.mark.asyncio
async def test_actual_coordinator_worker_resumes_accepted_command_after_restart(installed):
    case=installed
    coordinator,owner,_,state=build(case,stage="runtime")
    session=connect(coordinator)
    request=recovery_command(coordinator,session)
    operation,_=coordinator._accept(session,request)
    assert not state["calls"]
    await owner.stop()
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    coordinator,owner,base,state=build(case,store=reopened,state=state,stage="runtime")
    session=connect(coordinator)
    coordinator.start()
    await coordinator.drain()
    assert coordinator.receipt(session,operation["operation_id"])["state"]=="waiting"
    assert coordinator.read(session)["authority_recovery"]=="sign-in-required"
    assert [x[:2] for x in state["calls"]]==[("authority","prepare"),("ceremony","prepare"),("auth","read")]
    # Duplicate after restart returns the existing receipt and cannot start a
    # second worker or change the already accepted immutable operation.
    receipt=await coordinator.command(session,request)
    assert receipt["state"]=="waiting"
    await coordinator.drain()
    assert len(state["calls"])==3 and not base.calls
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
async def test_enabled_composition_is_explicit_and_disabled_boot_retains_fence(installed):
    from app.onboarding.composition import build_desktop_setup
    from app.onboarding.runtime import RuntimeSetupOwner
    case=installed
    kwargs=dict(store=case.store,stage="runtime",extension_id=None,
        hosted_origin="https://provider.example",authentication_providers=("default",),
        finalize=lambda **kw:None,completion_ready=lambda:True,
        adapt_owner=lambda setup:RuntimeSetupOwner(case.store,browser_tasks=setup.browser_tasks,activation=None,setup_owner=setup))
    normal=build_desktop_setup(**kwargs)
    assert isinstance(normal.coordinator.owner,RuntimeSetupOwner)
    await normal.close()
    qualified=build_desktop_setup(**kwargs,protected_recovery_enabled=True)
    assert isinstance(qualified.coordinator.owner,ProtectedAuthoritySetupOwner)
    assert qualified.coordinator.owner.enabled
    session=connect(qualified.coordinator)
    request=recovery_command(qualified.coordinator,session)
    operation,_=qualified.coordinator._accept(session,request)
    assert qualified.coordinator.receipt(session,operation["operation_id"])["state"]=="accepted"
    await qualified.close()
    normal=build_desktop_setup(**kwargs)
    assert isinstance(normal.coordinator.owner,ProtectedAuthoritySetupOwner)
    assert not normal.coordinator.owner.enabled
    session=connect(normal.coordinator)
    assert normal.coordinator.read(session)["authority_recovery"]=="blocked"
    assert not normal.coordinator.read(session)["allowed_actions"]
    await normal.close()

async def prepared_parent(case, *, stage="setup"):
    coordinator,owner,base,state=build(case,stage=stage)
    session=connect(coordinator)
    receipt=await coordinator.command(session,recovery_command(coordinator,session))
    await coordinator.drain()
    parent=saved(coordinator,receipt["operation_id"])
    assert parent["state"]=="waiting"
    return coordinator,owner,base,state,session,parent


def child_command(coordinator,session,action="auth.select"):
    snapshot=coordinator.read(session)
    assert snapshot["authority_recovery"]=="sign-in-required"
    assert action in snapshot["allowed_actions"]
    return command(snapshot,action)|{"recover_authority":True}|({"provider":"default"} if action=="auth.select" else {})


def child_intent(operation):
    return json.loads(operation["owner_scope_json"])["protected_recovery_child"]


def auth_calls(state,verb):
    return [request for family,method,request in state["calls"] if family=="auth" and method==verb]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage",["setup","runtime"])
async def test_child_commands_are_explicit_exact_and_never_dispatch(installed,stage):
    case=installed
    coordinator,owner,base,state,session,parent=await prepared_parent(case,stage=stage)
    snapshot=coordinator.read(session)
    auth=snapshot["authentication"]
    assert auth["operation_id"]==parent["operation_id"] and auth["browser_operation_id"] is None
    assert auth["choices"]["default"] and sum(auth["choices"].values())==1
    assert auth["actions"]=={"select":True,"cancel":False,"open_browser":False,"switch_account":False}
    request=child_command(coordinator,session)
    with pytest.raises(CommandRefused):
        await coordinator.command(session,{k:v for k,v in request.items() if k!="recover_authority"})
    with pytest.raises(ValueError):
        await coordinator.command(session,{**request,"provider":"google"})
    with pytest.raises(ValueError):
        await coordinator.command(session,{**request,"action":"auth.switch"})
    operation,fresh=coordinator._accept(session,request)
    assert fresh
    plan=child_intent(operation)
    assert plan["parent_operation_id"]==parent["operation_id"]
    assert plan["recovery_id"]==rid(parent) and plan["request"]["expected_previous_attempt_id"] is None
    assert not auth_calls(state,"select") and not base.admissions
    from app.onboarding.admission import AdmissionRefused
    for protected in (parent,operation):
        with pytest.raises(AdmissionRefused,match="task_scope_invalid"):
            coordinator.admissions.prepare_accepted_task(protected["operation_id"],purpose="browser.authentication",
                deadline=protected["expires_at"])
        with pytest.raises(AdmissionRefused,match="task_scope_invalid"):
            coordinator.admissions.prepare_task(session.identifier,generation=session.generation,
                journey_id=protected["journey_id"],operation_id=protected["operation_id"],purpose="browser.authentication",
                scope_digest=protected["scope_digest"],source_revision=protected["source_revision"],deadline=protected["expires_at"])
    assert coordinator._accept(session,request)[1] is False
    assert coordinator.read(session)["authority_recovery"]=="checking"
    assert not coordinator.read(session)["allowed_actions"]
    assert not owner.automatic_recovery(parent)
    assert (await owner.execute(parent,reconcile=True)).state=="waiting"
    result=await owner.execute(operation,reconcile=False)
    assert result.state=="completed"
    coordinator._settle(operation,result)
    assert auth_calls(state,"select")==[plan["request"]]
    assert auth_calls(state,"read")[-1]["authentication_attempt_id"] is None
    selected=coordinator.read(session)
    assert selected["authentication"]["state"]=="selected"
    assert selected["authentication"]["authentication_attempt"]["authentication_attempt_id"]==plan["request"]["authentication_attempt_id"]
    assert selected["allowed_actions"]==["auth.cancel"]
    assert selected["authentication"]["actions"]["open_browser"]
    assert selected["authentication"]["browser_operation_id"] is None
    cancel=child_command(coordinator,session,"auth.cancel")
    receipt=await coordinator.command(session,cancel)
    await coordinator.drain()
    cancelled=saved(coordinator,receipt["operation_id"])
    assert cancelled["state"]=="completed"
    assert auth_calls(state,"cancel")==[child_intent(cancelled)["request"]]
    retired=coordinator.read(session)
    assert retired["authentication"]["state"]=="retired"
    assert retired["allowed_actions"]==["auth.select"]
    with pytest.raises(CommandRefused):
        await coordinator.command(session,{**request,"operation_id":cancel["operation_id"]})
    next_request=child_command(coordinator,session)
    receipt=await coordinator.command(session,next_request)
    await coordinator.drain()
    successor=saved(coordinator,receipt["operation_id"])
    assert successor["state"]=="completed"
    next_plan=child_intent(successor)["request"]
    assert next_plan["expected_previous_attempt_id"]==plan["request"]["authentication_attempt_id"]
    assert next_plan["authentication_attempt_id"]!=plan["request"]["authentication_attempt_id"]
    assert len(auth_calls(state,"select"))==2 and len(auth_calls(state,"cancel"))==1
    with case.store.database.read() as c:
        assert c.execute("SELECT count(*) FROM desktop_browser_tasks").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM protected_recovery_authentication_children").fetchone()[0]==2
        assert c.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0]==len(case.grants)
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
async def test_restart_before_send_does_not_select_and_fresh_click_can_continue(installed):
    case=installed
    coordinator,owner,_,state,session,parent=await prepared_parent(case)
    request=child_command(coordinator,session)
    operation,_=coordinator._accept(session,request)
    assert not auth_calls(state,"select")
    await coordinator.stop()
    await owner.stop()
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    coordinator,owner,base,state=build(case,store=reopened,state=state)
    session=connect(coordinator)
    coordinator.start()
    await coordinator.drain()
    assert coordinator.receipt(session,operation["operation_id"])["state"]=="refused"
    assert not auth_calls(state,"select") and not base.calls
    assert coordinator.read(session)["allowed_actions"]==["auth.select"]
    again=child_command(coordinator,session)
    receipt=await coordinator.command(session,again)
    await coordinator.drain()
    op=saved(coordinator,receipt["operation_id"])
    assert op["state"]=="completed"
    assert len(auth_calls(state,"select"))==1
    assert child_intent(op)["request"]["authentication_attempt_id"]!=child_intent(operation)["request"]["authentication_attempt_id"]
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("verb",["select","cancel"])
async def test_lost_mutation_restarts_exact_read_then_current_without_resend(installed,verb):
    case=installed
    coordinator,owner,_,state,session,parent=await prepared_parent(case)
    if verb=="cancel":
        receipt=await coordinator.command(session,child_command(coordinator,session))
        await coordinator.drain()
        assert coordinator.receipt(session,receipt["operation_id"])["state"]=="completed"
    state["lose_"+verb]=True
    request=child_command(coordinator,session,"auth."+verb)
    receipt=await coordinator.command(session,request)
    await coordinator.drain()
    op=saved(coordinator,receipt["operation_id"])
    assert op["state"]=="outcome_unknown"
    original=child_intent(op)["request"]
    assert auth_calls(state,verb)==[original]
    assert not coordinator.read(session)["allowed_actions"]
    await coordinator.stop()
    await owner.stop()
    case.clock[0]+=timedelta(seconds=16)
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    coordinator,owner,base,state=build(case,store=reopened,state=state)
    session=connect(coordinator)
    coordinator.start()
    await coordinator.drain()
    assert coordinator.receipt(session,op["operation_id"])["state"]=="completed"
    assert auth_calls(state,verb)==[original]
    reads=auth_calls(state,"read")
    assert reads[-2]["authentication_attempt_id"]==original["authentication_attempt_id"]
    assert reads[-1]["authentication_attempt_id"] is None
    assert coordinator.read(session)["allowed_actions"]==(["auth.cancel"] if verb=="select" else ["auth.select"])
    assert not base.calls
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
async def test_cancel_that_observes_commit_is_refused_and_parent_adopts_original_grants(installed):
    case=installed
    coordinator,owner,_,state,session,parent=await prepared_parent(case)
    receipt=await coordinator.command(session,child_command(coordinator,session))
    await coordinator.drain()
    assert coordinator.receipt(session,receipt["operation_id"])["state"]=="completed"
    with case.store.database.read() as c:
        hosted=json.loads(owner.recovery.row(c,rid(parent))["request_json"])
    case.clock[0]+=timedelta(seconds=16)
    state["verifier"],state["result"]=signed_delivery(case,hosted)
    state["commit_on_cancel"]=True
    cancelled=await coordinator.command(session,child_command(coordinator,session,"auth.cancel"))
    await coordinator.drain()
    assert coordinator.receipt(session,cancelled["operation_id"])["state"]=="refused"
    assert coordinator.receipt(session,parent["operation_id"])["state"]=="completed"
    assert [request for family,verb,request in state["calls"] if family=="authority" and verb=="deliver"]==[hosted]
    assert "authority_recovery" not in coordinator.read(session)
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
async def test_changed_local_authority_blocks_admitted_child_before_send(installed):
    case=installed
    coordinator,owner,_,state,session,parent=await prepared_parent(case)
    operation,_=coordinator._accept(session,child_command(coordinator,session))
    with case.store.database.transaction() as c:
        case.store._revoke_in_transaction(c,RevocationKey(RevocationScopeType.INSTALLATION,case.scope["installation_id"]),reason="test")
    assert (await owner.execute(operation,reconcile=False)).state=="outcome_unknown"
    assert not auth_calls(state,"select")
    assert coordinator.read(session)["authority_recovery"]=="blocked"
    assert not owner.automatic_recovery(operation)
    await coordinator.stop()
    await owner.stop()

def test_http_native_admission_is_required_for_protected_child_commands(installed):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.onboarding.routes import desktop_router
    from app.onboarding.runtime import RuntimeActor
    from app.onboarding.admission_routes import ORIGIN, CSRF
    from test_desktop_browser_routes import headers

    case=installed
    coordinator,owner,_,state=build(case,stage="runtime")
    session=connect(coordinator)
    def no_operator_session():
        pytest.fail("Protected recovery incorrectly requested local operator authority")
    app=FastAPI()
    app.include_router(desktop_router(coordinator,actor=lambda *_:RuntimeActor(no_operator_session)))
    with TestClient(app,base_url=ORIGIN) as client:
        path="/api/v1/desktop/setup/commands"
        parent=recovery_command(coordinator,session)
        assert client.post(path,json=parent,headers=headers(session)).status_code==202
        client.portal.call(coordinator.drain)
        select=child_command(coordinator,session)
        missing_csrf={key:value for key,value in headers(session).items() if key!=CSRF}
        assert client.post(path,json=select,headers=missing_csrf).status_code==403
        assert client.post(path,json=select,headers={**headers(session),"Origin":"https://foreign.invalid"}).status_code==403
        assert client.post(path,json=select,headers={**headers(session),"Cookie":"unrelated=1"}).status_code==401
        unmarked={key:value for key,value in select.items() if key!="recover_authority"}
        assert client.post(path,json=unmarked,headers=headers(session)).status_code==409
        assert not auth_calls(state,"select")
        admitted=client.post(path,json=select,headers=headers(session))
        assert admitted.status_code==202
        client.portal.call(coordinator.drain)
        assert coordinator.receipt(session,select["operation_id"])["state"]=="completed"
        assert len(auth_calls(state,"select"))==1
        assert client.post(path,json=select,headers=headers(session)).status_code==202
        client.portal.call(coordinator.drain)
        assert len(auth_calls(state,"select"))==1
        client.portal.call(coordinator.stop)
        client.portal.call(owner.stop)

@pytest.mark.asyncio
async def test_expired_child_send_claim_cannot_send_but_retained_reads_stay_possible(installed):
    case=installed
    coordinator,owner,_,state,session,parent=await prepared_parent(case)
    operation,_=coordinator._accept(session,child_command(coordinator,session))
    # Simulate the command expiring while a worker waits before its first send.
    # An expired desktop command cannot be treated as a fresh click.
    from datetime import datetime,timezone
    case.clock[0]=datetime.fromtimestamp(operation["expires_at"],tz=timezone.utc)
    with case.store.database.read() as c:
        with pytest.raises(HostedGrantUnavailable):
            owner._authorize_child(operation,connection=c,mutation=True)
        owner._authorize_child(operation,connection=c)
    assert not auth_calls(state,"select")
    await coordinator.stop()
    await owner.stop()

def launch_request(coordinator,session):
    snapshot=coordinator.read(session)
    auth=snapshot["authentication"]
    assert snapshot["authority_recovery"]=="sign-in-required" and auth["actions"]["open_browser"]
    assert snapshot["authority_recovery_browser"]=="ready"
    return {"parent_operation_id":auth["operation_id"],
        "attempt_id":auth["authentication_attempt"]["authentication_attempt_id"],
        "expected":{key:snapshot[key] for key in ("generation","revision","scope_digest")}}


async def selected_parent(case, *, stage="setup"):
    coordinator,owner,base,state,session,parent=await prepared_parent(case,stage=stage)
    receipt=await coordinator.command(session,child_command(coordinator,session))
    await coordinator.drain()
    assert coordinator.receipt(session,receipt["operation_id"])["state"]=="completed"
    from app.onboarding.browser_tasks import BrowserTasks
    tasks=BrowserTasks(coordinator.admissions,extension_id=None)
    return coordinator,owner,base,state,session,parent,tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("stage",["setup","runtime"])
async def test_one_use_protected_native_entry_and_original_signed_delivery(installed,stage):
    from app.onboarding.admission import AdmissionRefused
    from app.security.browser_ceremony import browser_entry_url
    case=installed
    coordinator,owner,base,state,session,parent,tasks=await selected_parent(case,stage=stage)
    launch=launch_request(coordinator,session)
    before=len(state["calls"])
    with pytest.raises(CommandRefused):
        coordinator.open_protected_browser(session,{**launch,"expected":{**launch["expected"],"revision":0}},tasks=tasks)
    code=coordinator.open_protected_browser(session,launch,tasks=tasks)
    assert len(code)==43 and len(state["calls"])==before
    with pytest.raises(CommandRefused):
        coordinator.open_protected_browser(session,launch,tasks=tasks)
    with case.store.database.read() as c:
        dispatch=dict(c.execute("SELECT * FROM protected_recovery_browser_dispatches").fetchone())
        assert dispatch["attempt_id"]==launch["attempt_id"] and dispatch["recovery_id"]==rid(parent)
        child=owner.browser.journal.child(c,rid(parent))
        assert child["open_claimed"]
        task=dict(c.execute("SELECT * FROM desktop_browser_tasks WHERE task_id=?",(dispatch["task_id"],)).fetchone())
        assert task["state"]=="pending"
        ceremony=owner.browser.journal.row(c,rid(parent))
        expected=browser_entry_url(owner.recovery.origin.rstrip("/"),ceremony["entry_reference"])
        hosted=json.loads(owner.recovery.row(c,rid(parent))["request_json"])
    # No ordinary task, helper, paired session or reissue can consume the code.
    assert tasks.current_native(session)==[]
    with pytest.raises(AdmissionRefused):
        coordinator.admissions.consume(code,role="browser_task")
    for reissue in (coordinator.admissions.reissue_task_entry,coordinator.admissions.rebind_paired_task):
        with pytest.raises(AdmissionRefused):
            reissue(session.identifier,generation=session.generation,task_id=task["task_id"])
    assert not coordinator.read(session)["authentication"]["actions"]["open_browser"]
    assert coordinator.read(session)["allowed_actions"]==["auth.cancel"]
    assert coordinator.read(session)["authority_recovery_browser"]=="launch-claimed"
    # A parent worker cannot clear the projection while the local entry awaits consumption.
    case.clock[0]+=timedelta(seconds=16)
    assert not owner.automatic_recovery(parent)
    assert owner.confirmation_deadline(parent)==task["expires_at"]
    assert (await owner.execute(parent,reconcile=True)).state=="waiting"
    assert len(state["calls"])==before
    assert tasks.consume_protected(code,resolve=owner.resolve_browser_launch)==expected
    assert tasks.consume_protected(code,resolve=owner.resolve_browser_launch) is None
    assert coordinator.read(session)["authority_recovery_browser"]=="handed-off"
    with pytest.raises(AdmissionRefused):
        coordinator.admissions.consume(code,role="browser_task")
    with case.store.database.read() as c:
        assert c.execute("SELECT state FROM desktop_browser_tasks WHERE task_id=?",(task["task_id"],)).fetchone()[0]=="completed"
        assert c.execute("SELECT count(*) FROM desktop_admission_sessions WHERE role='browser_task'").fetchone()[0]==0
        assert len(c.execute("SELECT * FROM verified_grant_references").fetchall())==len(case.grants)
    # Browser consumption is not authentication. Only a verified proof-bound
    # current Provider read followed by independent delivery completes recovery.
    assert coordinator.receipt(session,parent["operation_id"])["state"]=="waiting"
    state["auth_state"]="admitted"
    state["verifier"],state["result"]=signed_delivery(case,hosted)
    assert owner.automatic_recovery(parent)
    await coordinator.reconcile(session,parent["operation_id"])
    await coordinator.drain()
    assert coordinator.receipt(session,parent["operation_id"])["state"]=="completed"
    assert [request for family,verb,request in state["calls"] if family=="authority" and verb=="deliver"]==[hosted]
    assert "authority_recovery" not in coordinator.read(session)
    assert not base.calls
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("change",["cancel","revoke","expire","reattach","disabled"])
async def test_old_protected_entry_is_fenced_without_reissue(installed,change):
    from app.onboarding.admission import AdmissionRefused
    from app.onboarding.browser_tasks import BrowserTasks
    case=installed
    coordinator,owner,_,state,session,parent,tasks=await selected_parent(case)
    launch=launch_request(coordinator,session)
    code=coordinator.open_protected_browser(session,launch,tasks=tasks)
    if change=="cancel":
        receipt=await coordinator.command(session,child_command(coordinator,session,"auth.cancel"))
        await coordinator.drain()
        assert coordinator.receipt(session,receipt["operation_id"])["state"]=="completed"
    elif change=="revoke":
        with case.store.database.transaction() as c:
            case.store._revoke_in_transaction(c,RevocationKey(RevocationScopeType.INSTALLATION,case.scope["installation_id"]),reason="test")
    elif change=="expire":
        case.clock[0]+=timedelta(seconds=301)
    elif change=="reattach":
        await coordinator.stop()
        await owner.stop()
        reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
        coordinator,owner,_,state=build(case,store=reopened,state=state)
        session=connect(coordinator)
        tasks=BrowserTasks(coordinator.admissions,extension_id=None)
    else:
        await coordinator.stop()
        await owner.stop()
        coordinator,owner,_,state=build(case,state=state,enabled=False)
        session=connect(coordinator)
        tasks=BrowserTasks(coordinator.admissions,extension_id=None)
    if change=="cancel":
        assert tasks.consume_protected(code,resolve=owner.resolve_browser_launch) is None
    else:
        with pytest.raises((AdmissionRefused,HostedGrantUnavailable)):
            tasks.consume_protected(code,resolve=owner.resolve_browser_launch)
    with pytest.raises(AdmissionRefused):
        coordinator.admissions.consume(code,role="browser_task")
    with pytest.raises(CommandRefused):
        coordinator.open_protected_browser(session,launch,tasks=tasks)
    with coordinator.store.database.read() as c:
        assert c.execute("SELECT count(*) FROM desktop_admission_sessions WHERE role='browser_task'").fetchone()[0]==0
        assert c.execute("SELECT open_claimed FROM protected_recovery_authentication_children").fetchone()[0]==1
    assert len(auth_calls(state,"select"))==1
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
async def test_cancelled_launch_allows_only_fresh_successor_entry(installed):
    case=installed
    coordinator,owner,_,state,session,parent,tasks=await selected_parent(case)
    first=launch_request(coordinator,session)
    old=coordinator.open_protected_browser(session,first,tasks=tasks)
    await coordinator.command(session,child_command(coordinator,session,"auth.cancel"))
    await coordinator.drain()
    await coordinator.command(session,child_command(coordinator,session))
    await coordinator.drain()
    successor=launch_request(coordinator,session)
    assert successor["attempt_id"]!=first["attempt_id"]
    code=coordinator.open_protected_browser(session,successor,tasks=tasks)
    assert old!=code
    assert tasks.consume_protected(old,resolve=owner.resolve_browser_launch) is None
    assert tasks.consume_protected(code,resolve=owner.resolve_browser_launch).startswith("https://fixture.invalid/")
    with case.store.database.read() as c:
        assert c.execute("SELECT count(*) FROM protected_recovery_browser_dispatches").fetchone()[0]==2
    await coordinator.stop()
    await owner.stop()


def test_http_protected_browser_entry_uses_native_admission_without_helper_cookie(installed):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.onboarding.admission_routes import admission_router, ORIGIN, CSRF
    from app.onboarding.browser_tasks import BrowserTasks
    from app.onboarding.browser_routes import browser_router
    from app.onboarding.routes import desktop_router
    from test_desktop_browser_routes import headers
    case=installed
    coordinator,owner,_,state=build(case,stage="runtime")
    session=connect(coordinator)
    tasks=BrowserTasks(coordinator.admissions,extension_id=None)
    app=FastAPI()
    app.include_router(desktop_router(coordinator))
    app.include_router(browser_router(coordinator,tasks,None))
    app.include_router(admission_router(coordinator.admissions,launcher_token="T"*43,
        protected_entry=lambda code:tasks.consume_protected(code,resolve=owner.resolve_browser_launch)))
    with TestClient(app,base_url=ORIGIN) as client:
        command_path="/api/v1/desktop/setup/commands"
        parent=recovery_command(coordinator,session)
        assert client.post(command_path,json=parent,headers=headers(session)).status_code==202
        client.portal.call(coordinator.drain)
        select=child_command(coordinator,session)
        assert client.post(command_path,json=select,headers=headers(session)).status_code==202
        client.portal.call(coordinator.drain)
        launch=launch_request(coordinator,session)
        path="/api/v1/desktop/setup/protected-browser/open"
        assert client.post(path,json=launch,headers={k:v for k,v in headers(session).items() if k!=CSRF}).status_code==403
        assert client.post(path,json=launch,headers={**headers(session),"Origin":"https://foreign.invalid"}).status_code==403
        opened=client.post(path,json=launch,headers=headers(session))
        assert opened.status_code==200 and set(opened.json())=={"entry_code"}
        assert client.post(path,json=launch,headers=headers(session)).status_code==409
        code=opened.json()["entry_code"]
        with case.store.database.read() as c:
            task=c.execute("SELECT task_id FROM protected_recovery_browser_dispatches").fetchone()[0]
        assert client.get("/api/v1/desktop/browser-tasks",headers=headers(session)).json()=={"tasks":[]}
        assert client.post(f"/api/v1/desktop/browser-tasks/{task}/open",json={},headers=headers(session)).json()=={"state":"unavailable"}
        assert client.post(f"/api/v1/desktop/browser-tasks/{task}/entry",json={},headers=headers(session)).status_code==409
        consume="/api/v1/browser-tasks/entry/consume"
        assert client.post(consume,json={"code":code},headers={"Origin":"https://foreign.invalid"}).status_code==403
        entry=client.post(consume,json={"code":code},headers={"Origin":ORIGIN})
        assert entry.status_code==200 and set(entry.json())=={"protected_browser_url"}
        assert entry.json()["protected_browser_url"].startswith("https://fixture.invalid/onboarding/browser-ceremonies/")
        assert "set-cookie" not in entry.headers
        assert client.post(consume,json={"code":code},headers={"Origin":ORIGIN}).status_code==401
        assert client.get("/api/v1/browser-tasks/payload",headers=headers(session)).status_code==401
        client.portal.call(coordinator.stop)
        client.portal.call(owner.stop)

@pytest.mark.asyncio
async def test_consumed_entry_restart_observes_original_commit_and_lost_delivery(installed):
    from app.onboarding.browser_tasks import BrowserTasks
    case=installed
    coordinator,owner,_,state,session,parent,tasks=await selected_parent(case,stage="runtime")
    launch=launch_request(coordinator,session)
    code=coordinator.open_protected_browser(session,launch,tasks=tasks)
    assert tasks.consume_protected(code,resolve=owner.resolve_browser_launch)
    with case.store.database.read() as c:
        hosted=json.loads(owner.recovery.row(c,rid(parent))["request_json"])
    await coordinator.stop()
    await owner.stop()
    case.clock[0]+=timedelta(seconds=16)
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    coordinator,owner,base,state=build(case,store=reopened,state=state,stage="runtime")
    session=connect(coordinator)
    tasks=BrowserTasks(coordinator.admissions,extension_id=None)
    assert tasks.consume_protected(code,resolve=owner.resolve_browser_launch) is None
    with pytest.raises(CommandRefused):
        coordinator.open_protected_browser(session,launch,tasks=tasks)
    assert not coordinator.read(session)["authentication"]["actions"]["open_browser"]
    state["auth_state"]="admitted"
    state["verifier"],state["result"]=signed_delivery(case,hosted)
    state["lose_delivery"]=True
    coordinator.start()
    await coordinator.drain()
    assert coordinator.receipt(session,parent["operation_id"])["state"]=="outcome_unknown"
    restoring=coordinator.read(session)
    assert restoring["authority_recovery"]=="restoring" and restoring["allowed_actions"]==[]
    assert "authority_recovery_browser" not in restoring
    await coordinator.stop()
    await owner.stop()
    case.clock[0]+=timedelta(seconds=16)
    coordinator,owner,base,state=build(case,store=reopened,state=state,stage="runtime")
    session=connect(coordinator)
    coordinator.start()
    await coordinator.drain()
    assert coordinator.receipt(session,parent["operation_id"])["state"]=="completed"
    assert [request for family,verb,request in state["calls"] if family=="authority" and verb=="deliver"]==[hosted,hosted]
    assert len(auth_calls(state,"select"))==1 and not base.calls
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
async def test_missing_child_projection_observes_original_ceremony_within_budget(installed):
    case=installed
    coordinator,owner,_,state,session,parent,tasks=await selected_parent(case)
    launch=launch_request(coordinator,session)
    code=coordinator.open_protected_browser(session,launch,tasks=tasks)
    assert tasks.consume_protected(code,resolve=owner.resolve_browser_launch)
    with case.store.database.read() as c:
        hosted=json.loads(owner.recovery.row(c,rid(parent))["request_json"])
    case.clock[0]+=timedelta(seconds=16)
    # A missing child projection is not an explicit retirement or noncommit.
    state["auth_state"]="unavailable"
    state["child"]=None
    assert (await owner.execute(parent,reconcile=True)).state=="waiting"
    with case.store.database.read() as c:
        binding=owner.recovery.row(c,rid(parent))
        assert binding["state"]=="prepared"
        assert owner.browser.journal.row(c,rid(parent))["projection_json"] is None
        attempts=c.execute("SELECT read_attempts FROM protected_recovery_desktop_admissions").fetchone()[0]
    state["committed"]=True
    state["verifier"],state["result"]=signed_delivery(case,hosted)
    for _ in range(3):
        case.clock[0]+=timedelta(seconds=61)
        outcome=await owner.execute(parent,reconcile=True)
        if outcome.state=="completed":
            break
    assert outcome.state=="completed"
    assert [request for family,verb,request in state["calls"] if family=="authority" and verb=="deliver"]==[hosted]
    with case.store.database.read() as c:
        assert c.execute("SELECT read_attempts FROM protected_recovery_desktop_admissions").fetchone()[0]<=attempts+3
    await coordinator.stop()
    await owner.stop()


def _exhaust_reads(store, recovery_id):
    with store.database.transaction() as c:
        current=c.execute("SELECT read_attempts FROM protected_recovery_desktop_admissions WHERE recovery_id=?",
            (recovery_id,)).fetchone()[0]
        for n in range(current+1,13):
            c.execute("UPDATE protected_recovery_desktop_admissions SET read_attempts=? WHERE recovery_id=?",
                (n,recovery_id))


def _recovery_counts(store):
    with store.database.read() as c:
        return {table:c.execute("SELECT count(*) FROM "+table).fetchone()[0] for table in (
            "installation_authority_recoveries","protected_recovery_desktop_admissions",
            "protected_recovery_ceremonies","protected_recovery_authentication_children",
            "protected_recovery_browser_dispatches")}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["reserved","before-launch","pending-child"])
async def test_unreached_abandonment_retires_past_window_and_allows_fresh_admission(installed,mode):
    import asyncio
    from app.provisioning.events import events
    case=installed
    if mode=="reserved":
        coordinator,owner,base,state=build(case)
        session=connect(coordinator)
        with case.store.database.read() as c:
            initial_digest=owner._offer(c)[1]
        request=recovery_command(coordinator,session)
        parent,_=coordinator._accept(session,request)
        with case.store.database.read() as c:
            created_at=owner.recovery.row(c,rid(parent))["created_at"]
        case.clock[0]+=timedelta(seconds=1800)
        assert not owner.automatic_recovery(parent)
        assert owner.confirmation_deadline(parent)==created_at+1860
    elif mode=="before-launch":
        coordinator,owner,base,state,session,parent,_=await selected_parent(case)
        # Cancel child 1 and select child 2, leaving both unlaunched (open_claimed=0)
        # to exercise multi-child self-FK deletion as well.
        await coordinator.command(session,child_command(coordinator,session,"auth.cancel"))
        await coordinator.drain()
        await coordinator.command(session,child_command(coordinator,session))
        await coordinator.drain()
        with case.store.database.read() as c:
            created_at=owner.recovery.row(c,rid(parent))["created_at"]
            initial_digest=owner.recovery.row(c,rid(parent))["scope_digest"]
            assert c.execute("SELECT count(*) FROM protected_recovery_authentication_children").fetchone()[0]==2
        assert not owner.automatic_recovery(parent)
        assert owner.confirmation_deadline(parent)==created_at+1860
    else:
        coordinator,owner,base,state,session,parent=await prepared_parent(case)
        with case.store.database.read() as c:
            created_at=owner.recovery.row(c,rid(parent))["created_at"]
            initial_digest=owner.recovery.row(c,rid(parent))["scope_digest"]
        state["lose_select"]=True
        child_receipt=await coordinator.command(session,child_command(coordinator,session))
        await coordinator.drain()
        child_op=saved(coordinator,child_receipt["operation_id"])
        assert child_op["state"]=="outcome_unknown"
        assert owner.confirmation_deadline(parent)==created_at+1860
    retired_rid=rid(parent)
    calls_before=len(state["calls"])
    # Start the real coordinator watcher before the deadline, then cross
    # created_at+1860 so _watch fires through confirmation_deadline.
    coordinator.start()
    for _ in range(1000):
        if parent["operation_id"] in coordinator._confirmation_at:
            break
        await asyncio.sleep(0.01)
    assert coordinator._confirmation_at.get(parent["operation_id"])==created_at+1860
    assert _recovery_counts(case.store)["installation_authority_recoveries"]==1
    case.clock[0]=case.clock[0]+timedelta(seconds=max(0.0,created_at+1860-case.clock[0].timestamp()))
    events.publish()
    for _ in range(1000):
        if saved(coordinator,parent["operation_id"])["state"]=="refused":
            break
        await asyncio.sleep(0.01)
    await coordinator.drain()
    fresh_session=connect(coordinator)
    receipt=coordinator.receipt(fresh_session,parent["operation_id"])
    assert receipt["state"]=="refused" and receipt["reason"]=="operation_expired"
    if mode=="pending-child":
        child_final=coordinator.receipt(fresh_session,child_op["operation_id"])
        assert child_final["state"]=="refused" and child_final["reason"]=="operation_expired"
    assert all(count==0 for count in _recovery_counts(case.store).values())
    assert len(state["calls"])==calls_before
    # Re-running execute for the retired parent never calls the client.
    assert await owner.execute(parent,reconcile=True)==OperationResult("refused","operation_expired")
    assert len(state["calls"])==calls_before
    with case.store.database.read() as c:
        assert owner._offer(c)[1]==initial_digest
    snap=coordinator.read(fresh_session)
    assert snap["authority_recovery"]=="available" and snap["allowed_actions"]==["account.connect"]
    admitted,is_new=coordinator._accept(fresh_session,recovery_command(coordinator,fresh_session))
    assert is_new and rid(admitted)!=retired_rid
    assert not base.calls
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
async def test_observed_child_expiry_rechecks_parent_after_its_deadline_worker(installed, monkeypatch):
    import asyncio
    from threading import Event

    from app.provisioning.events import events

    case = installed
    coordinator, owner, _, state = build(case)
    session = connect(coordinator)
    parent_id = "ffffffff-ffff-4fff-bfff-ffffffffffff"
    child_id = "00000000-0000-4000-8000-000000000001"
    await coordinator.command(session, recovery_command(coordinator, session) | {"operation_id": parent_id})
    await coordinator.drain()
    parent = saved(coordinator, parent_id)
    state["lose_select"] = True
    await coordinator.command(session, child_command(coordinator, session) | {"operation_id": child_id})
    await coordinator.drain()
    assert parent["state"] == "waiting"
    assert saved(coordinator, child_id)["state"] == "outcome_unknown"
    # Both commands have the same fixture timestamp. The watcher reads the child first.
    assert [op["operation_id"] for op in coordinator._pending()] == [child_id, parent_id]
    deadline = owner.confirmation_deadline(parent)
    assert deadline is not None
    cutoff = case.clock[0] + timedelta(seconds=deadline - case.clock[0].timestamp())
    calls_before = list(state["calls"])
    advance_clock, crossed_deadline, first_parent_pass = Event(), Event(), Event()
    before_deadline = []
    parent_results = []
    observe, execute = owner.observe, owner.execute

    def observe_across_deadline(operation):
        if operation["operation_id"] != child_id:
            return observe(operation)
        if crossed_deadline.is_set():
            first_parent_pass.wait(10)
            return observe(operation)
        outcome = observe(operation)
        if advance_clock.is_set():
            before_deadline.append(outcome)
            case.clock[0] = cutoff
            crossed_deadline.set()
        return outcome

    async def execute_at_deadline(operation, *, reconcile, actor=None):
        result = await execute(operation, reconcile=reconcile, actor=actor)
        if operation["operation_id"] == parent_id and crossed_deadline.is_set():
            parent_results.append(result.state)
            first_parent_pass.set()
        return result

    monkeypatch.setattr(owner, "observe", observe_across_deadline)
    monkeypatch.setattr(owner, "execute", execute_at_deadline)
    coordinator.start()
    try:
        await coordinator.drain()
        async with asyncio.timeout(10):
            while coordinator._confirmation_at.get(parent_id) != deadline:
                await asyncio.sleep(0.01)
            advance_clock.set()
            events.publish()
            while saved(coordinator, child_id)["state"] != "refused":
                await asyncio.sleep(0.01)
        await coordinator.drain()

        assert before_deadline == [None]
        assert parent_results[:1] == ["waiting"]
        fresh_session = connect(coordinator)
        for operation_id in (child_id, parent_id):
            receipt = coordinator.receipt(fresh_session, operation_id)
            assert (receipt["state"], receipt["reason"]) == ("refused", "operation_expired")
        assert parent_results == ["waiting", "refused"]
        assert not any(_recovery_counts(case.store).values())
        assert state["calls"] == calls_before
    finally:
        first_parent_pass.set()
        await coordinator.stop()
        await owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("variant",["pending-entry","earlier-child-only"])
async def test_reached_post_expiry_read_without_commit_retires_and_allows_fresh_admission(installed,variant):
    case=installed
    coordinator,owner,base,state,session,parent,tasks=await selected_parent(case)
    first_launch=launch_request(coordinator,session)
    coordinator.open_protected_browser(session,first_launch,tasks=tasks)
    if variant=="earlier-child-only":
        # Child 1 was open-claimed then cancelled; child 2 is selected without
        # open-claiming, proving retirement inspects every child, not only the latest.
        await coordinator.command(session,child_command(coordinator,session,"auth.cancel"))
        await coordinator.drain()
        await coordinator.command(session,child_command(coordinator,session))
        await coordinator.drain()
    with case.store.database.read() as c:
        row=owner.recovery.row(c,rid(parent))
        initial_digest=row["scope_digest"]
        ceremony=owner.browser.journal.row(c,rid(parent))
        assert c.execute("SELECT count(*) FROM protected_recovery_browser_dispatches").fetchone()[0]==1
        if variant=="pending-entry":
            assert c.execute("SELECT count(*) FROM desktop_browser_tasks WHERE state='pending'").fetchone()[0]==1
        else:
            assert owner.browser.journal.child(c,rid(parent))["open_claimed"]==0
    # Exhaust the active 12-read budget before expiry to prove the post-expiry
    # read sits outside the active budget.
    _exhaust_reads(case.store,rid(parent))
    from app.security.initial_handoff import timestamp
    expiry=timestamp(ceremony["expires_at"])
    # Advance past the 300s browser entry deadline, still before ceremony expiry.
    case.clock[0]+=timedelta(seconds=301)
    assert not owner.automatic_recovery(parent)
    assert owner.confirmation_deadline(parent)==expiry.timestamp()+EXPIRY_READ_MARGIN
    if variant=="pending-entry":
        # Nothing is read before the clock margin past the ceremony expiry.
        case.clock[0]=expiry
        assert not owner.automatic_recovery(parent)
        assert owner.confirmation_deadline(parent)==expiry.timestamp()+EXPIRY_READ_MARGIN
    # Advance past ceremony expiry (and for earlier-child-only, past created_at+1860
    # to prove it still requires the post-expiry read rather than unreached retirement).
    case.clock[0]=(expiry+timedelta(seconds=EXPIRY_READ_MARGIN) if variant=="pending-entry"
        else case.clock[0]+timedelta(seconds=1860))
    state["ceremony_presentation"]="expired" if variant=="pending-entry" else "retired"
    state["ceremony_domain"]="awaiting-authentication"
    calls_before=len(state["calls"])
    assert owner.automatic_recovery(parent)
    coordinator.start()
    await coordinator.drain()
    fresh=connect(coordinator)
    receipt=coordinator.receipt(fresh,parent["operation_id"])
    assert receipt["state"]=="refused" and receipt["reason"]=="operation_expired"
    new_calls=[item[:2] for item in state["calls"][calls_before:]]
    assert new_calls==[("ceremony","read")]
    assert all(count==0 for count in _recovery_counts(case.store).values())
    with case.store.database.read() as c:
        assert c.execute("SELECT count(*) FROM desktop_admission_entries WHERE role='browser_task'").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM desktop_browser_tasks WHERE state='pending'").fetchone()[0]==0
        assert owner._offer(c)[1]==initial_digest
    retired_rid=rid(parent)
    assert await owner.execute(parent,reconcile=True)==OperationResult("refused","operation_expired")
    assert len(state["calls"])==calls_before+1
    assert coordinator.read(fresh)["authority_recovery"]=="available"
    admitted,is_new=coordinator._accept(fresh,recovery_command(coordinator,fresh))
    assert is_new and rid(admitted)!=retired_rid and not base.calls
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
async def test_reached_post_expiry_read_with_commit_continues_to_delivery(installed):
    from app.security.initial_handoff import timestamp
    case=installed
    coordinator,owner,base,state,session,parent,tasks=await selected_parent(case)
    code=coordinator.open_protected_browser(session,launch_request(coordinator,session),tasks=tasks)
    assert tasks.consume_protected(code,resolve=owner.resolve_browser_launch)
    with case.store.database.read() as c:
        hosted=json.loads(owner.recovery.row(c,rid(parent))["request_json"])
        ceremony=owner.browser.journal.row(c,rid(parent))
    _exhaust_reads(case.store,rid(parent))
    case.clock[0]=timestamp(ceremony["expires_at"])+timedelta(seconds=EXPIRY_READ_MARGIN)
    state["ceremony_presentation"]="expired"
    state["committed"]=True
    state["verifier"],state["result"]=signed_delivery(case,hosted)
    calls_before=len(state["calls"])
    assert owner.automatic_recovery(parent)
    coordinator.start()
    await coordinator.drain()
    assert coordinator.receipt(session,parent["operation_id"])["state"]=="completed"
    assert [item[:2] for item in state["calls"][calls_before:]]==[("ceremony","read"),("authority","deliver")]
    with case.store.database.read() as c:
        assert owner.recovery.row(c,rid(parent))["state"]=="admitted"
        assert c.execute("SELECT count(*) FROM verified_grant_references WHERE revoked_at IS NULL").fetchone()[0]==len(case.grants)
    assert not base.calls
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome",["error","status-unavailable","domain-unavailable","revoked","active"])
async def test_reached_post_expiry_read_unavailable_or_uncertain_stays_blocked(installed,outcome):
    from app.security.initial_handoff import timestamp
    case=installed
    coordinator,owner,base,state,session,parent,tasks=await selected_parent(case)
    code=coordinator.open_protected_browser(session,launch_request(coordinator,session),tasks=tasks)
    assert tasks.consume_protected(code,resolve=owner.resolve_browser_launch)
    with case.store.database.read() as c:
        ceremony=owner.browser.journal.row(c,rid(parent))
    _exhaust_reads(case.store,rid(parent))
    case.clock[0]=timestamp(ceremony["expires_at"])+timedelta(seconds=EXPIRY_READ_MARGIN)
    if outcome=="error":
        state["lose_ceremony_read"]=True
    elif outcome=="status-unavailable":
        state["ceremony_unavailable"]=True
    elif outcome=="domain-unavailable":
        state["ceremony_presentation"]="expired"
        state["ceremony_domain"]="unavailable"
    elif outcome=="revoked":
        state["ceremony_presentation"]="expired"
        state["ceremony_domain"]="revoked"
    else:
        state["ceremony_presentation"]="active"
        state["ceremony_domain"]="unavailable"
    calls_before=len(state["calls"])
    coordinator.start()
    await coordinator.drain()
    assert [item[:2] for item in state["calls"][calls_before:]]==[("ceremony","read")]
    assert coordinator.read(session)["authority_recovery"]=="blocked"
    assert not coordinator.read(session)["allowed_actions"]
    assert not owner.automatic_recovery(parent)
    assert owner.confirmation_deadline(parent) is None
    with case.store.database.read() as c:
        assert c.execute("SELECT expiry_read_claimed FROM protected_recovery_desktop_admissions WHERE recovery_id=?",
            (rid(parent),)).fetchone()[0]==1
        assert owner.recovery.row(c,rid(parent))["state"]=="prepared"
    await coordinator.stop()
    await owner.stop()
    # After restart, the single post-expiry read is not repeated and stays blocked.
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    coordinator,owner,base,state=build(case,store=reopened,state=state)
    session=connect(coordinator)
    coordinator.start()
    await coordinator.drain()
    assert len(state["calls"])==calls_before+1
    assert coordinator.read(session)["authority_recovery"]=="blocked"
    assert _recovery_counts(reopened)["installation_authority_recoveries"]==1
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["committed","delivery-attempted","active-work"])
async def test_committed_and_delivery_attempted_recoveries_are_never_retired(installed,kind):
    from app.persistence import sqlite_api as sqlite3
    case=installed
    if kind=="committed":
        coordinator,owner,_,state=build(case)
        state["committed_prepare"]=True
        state["lose_delivery"]=True
        session=connect(coordinator)
        parent,_=coordinator._accept(session,recovery_command(coordinator,session))
        assert (await owner.execute(parent,reconcile=False)).state=="outcome_unknown"
        case.clock[0]+=timedelta(hours=2)
        with case.store.database.read() as c:
            hosted=json.loads(owner.recovery.row(c,rid(parent))["request_json"])
            assert owner.recovery.row(c,rid(parent))["state"]=="committed"
        assert owner._retire(parent) is False
        with pytest.raises(HostedGrantUnavailable):
            with case.store.database.transaction() as c:
                owner.recovery.retire(c,hosted,authorize=lambda **_:None)
        state["lose_delivery"]=True
        assert (await owner.execute(parent,reconcile=True)).state=="outcome_unknown"
        with case.store.database.read() as c:
            assert owner.recovery.row(c,rid(parent))["state"]=="committed"
    elif kind=="delivery-attempted":
        coordinator,owner,_,state,session,parent=await prepared_parent(case)
        case.clock[0]+=timedelta(seconds=16)
        with case.store.database.transaction() as c:
            hosted=json.loads(owner.recovery.row(c,rid(parent))["request_json"])
            attempt=owner.recovery.claim(c,hosted,"deliver",authorize=lambda **_:None)
            assert attempt==1
            owner.recovery.record_refusal(c,hosted,"deliver",attempt,AuthorityRecoveryRefused("not_ready"),authorize=lambda **_:None)
        case.clock[0]+=timedelta(hours=2)
        assert owner._retire(parent) is False
        assert not owner.automatic_recovery(parent)
        assert owner.confirmation_deadline(parent) is None
        with pytest.raises(HostedGrantUnavailable):
            with case.store.database.transaction() as c:
                owner.recovery.retire(c,hosted,authorize=lambda **_:None)
        assert (await owner.execute(parent,reconcile=True)).state=="waiting"
        with case.store.database.read() as c:
            row=owner.recovery.row(c,rid(parent))
            assert row["state"]=="prepared" and row["delivery_attempts"]==1
    else:
        coordinator,owner,_,state=build(case)
        session=connect(coordinator)
        parent,_=coordinator._accept(session,recovery_command(coordinator,session))
        case.clock[0]+=timedelta(seconds=60)
        assert owner.automatic_recovery(parent)
        assert owner.confirmation_deadline(parent) is None
        assert owner._retire(parent) is False
        assert _recovery_counts(case.store)["installation_authority_recoveries"]==1
    await coordinator.stop()
    await owner.stop()


@pytest.mark.asyncio
async def test_crash_after_retirement_deletion_settles_parent_on_restart(installed):
    case=installed
    coordinator,owner,base,state,session,parent=await prepared_parent(case)
    with case.store.database.read() as c:
        row=owner.recovery.row(c,rid(parent))
        created_at,initial_digest=row["created_at"],row["scope_digest"]
    case.clock[0]+=timedelta(seconds=max(0.0,created_at+1860-case.clock[0].timestamp()))
    # Commit the deletion transaction directly and skip coordinator._settle to
    # simulate a crash between retirement deletion and parent settlement.
    assert owner._retire(parent) is True
    assert all(count==0 for count in _recovery_counts(case.store).values())
    assert saved(coordinator,parent["operation_id"])["state"]=="waiting"
    calls_before=len(state["calls"])
    await coordinator.stop()
    await owner.stop()
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    coordinator,owner,base,state=build(case,store=reopened,state=state)
    session=connect(coordinator)
    coordinator.start()
    await coordinator.drain()
    receipt=coordinator.receipt(session,parent["operation_id"])
    assert receipt["state"]=="refused" and receipt["reason"]=="operation_expired"
    assert len(state["calls"])==calls_before
    snap=coordinator.read(session)
    assert snap["authority_recovery"]=="available" and snap["allowed_actions"]==["account.connect"]
    with reopened.database.read() as c:
        assert owner._offer(c)[1]==initial_digest
    assert not base.calls
    await coordinator.stop()
    await owner.stop()
