"""Real desktop coordinator admission of the protected recovery parent."""
from __future__ import annotations

import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.onboarding.admission import ScopedAdmissions
from app.onboarding.coordinator import CommandRefused, DesktopOnboardingCoordinator, OperationResult
from app.onboarding.protected_authority_setup import ProtectedAuthoritySetupOwner
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
            return ceremony_response(verb,request,domain="committed" if state.get("committed") else "awaiting-authentication")
    class Auth:
        def execute(self,verb,request,*,before_send,expires_at):
            before_send()
            state["calls"].append(("auth",verb,copy.deepcopy(request)))
            assert verb=="read"
            return auth_response(request,"selection-required")
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
