"""Encrypted protected ceremony and fresh child-attempt recovery."""
from __future__ import annotations

from datetime import timedelta
import copy
import json

import pytest

from app.persistence.auth import SQLiteAuthenticationStore, RevocationKey, RevocationScopeType
from app.persistence import sqlite_api as sqlite3
from app.persistence.protected_browser_recovery import ProtectedBrowserRecoveries
from app.security.hosted_grants import HostedGrantUnavailable
from app.security.protected_browser_recovery import authentication_request
from test_installation_authority_recovery_client import response_for, wire_time
from test_installation_authority_recovery_owner import installed, journal, reserve, allow
from test_protected_browser_recovery_client import ceremony_response, auth_response, CHILD, PREVIOUS, EXPIRY

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


def setup(case):
    recovery = journal(case)
    request = reserve(case, recovery)
    with case.store.database.transaction() as c:
        attempt = recovery.claim(c, request, "prepare", authorize=allow)
        recovery.record_preparation(c, request, attempt, response_for("prepare", request), authorize=allow)
        owner = ProtectedBrowserRecoveries(recovery)
        row = owner.reserve(c, request, authorize=allow)
        assert not row["prepare_claimed"]
        assert c.execute("SELECT count(*) FROM protected_recovery_authentication_children").fetchone()[0]==0
    return request, owner


def read_current(owner, c, recovery_id, state, child=None, **kw):
    request = owner.auth_read(c, recovery_id, authorize=allow)
    result = auth_response(request, state, child=child, **kw)
    owner.record_authentication(c, recovery_id, "read", request, result, authorize=allow)
    return result


def known(case):
    request, owner = setup(case)
    rid = request["recovery_id"]
    with case.store.database.transaction() as c:
        prepare = owner.claim_prepare(c, rid, authorize=allow)
        result = ceremony_response("prepare", prepare)
        owner.record_ceremony(c, rid, "prepare", prepare, result, authorize=allow)
        read_current(owner, c, rid, "selection-required")
    return rid, owner


def selected(case):
    rid, owner = known(case)
    with case.store.database.transaction() as c:
        request = owner.claim_select(c, rid, authorize=allow)
        aid = request["authentication_attempt_id"]
        result = auth_response(request, "selected", child=aid)
        owner.record_authentication(c, rid, "select", request, result, authorize=allow)
        read_current(owner, c, rid, "selected", aid)
    return rid, aid, owner


def test_lost_prepare_reconciles_exact_identity_without_second_send(installed):
    case = installed
    request, owner = setup(case)
    rid = request["recovery_id"]
    with case.store.database.transaction() as c:
        with pytest.raises(HostedGrantUnavailable):
            owner.read_request(c, rid, authorize=allow)
        prepare = owner.claim_prepare(c, rid, authorize=allow)
        assert prepare["binding"]["source"]["request"] == request
        assert prepare["binding"]["completion"] == {"mode":"system-browser"}
        assert owner.claim_prepare(c, rid, authorize=allow) is None
        with pytest.raises(HostedGrantUnavailable):
            owner.reserve(c, request, authorize=allow)
    reopened = SQLiteAuthenticationStore(case.path, clock=lambda: case.clock[0])
    owner = ProtectedBrowserRecoveries(journal(case,reopened))
    with reopened.database.transaction() as c:
        assert owner.claim_prepare(c,rid,authorize=allow) is None
        read = owner.read_request(c,rid,authorize=allow)
        assert read == prepare
        owner.record_ceremony(c,rid,"read",read,ceremony_response("read",read),authorize=allow)
        known_read = owner.read_request(c,rid,authorize=allow)
        assert "ceremony_id" in known_read
        with pytest.raises(HostedGrantUnavailable):
            owner.record_ceremony(c,rid,"prepare",prepare,ceremony_response("prepare",prepare),authorize=allow)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE protected_recovery_ceremonies SET binding_digest=?",("0"*64,))
        assert c.execute("SELECT count(*) FROM protected_recovery_ceremonies").fetchone()[0]==1


def test_missing_ceremony_result_is_not_permission_to_reprepare(installed):
    case=installed
    request,owner=setup(case)
    rid=request["recovery_id"]
    with case.store.database.transaction() as c:
        prepare=owner.claim_prepare(c,rid,authorize=allow)
        owner.record_ceremony(c,rid,"read",prepare,{"profile":prepare["profile"],"operation_id":prepare["operation_id"],
            "status":"unavailable"},authorize=allow)
        assert owner.claim_prepare(c,rid,authorize=allow) is None
        assert owner.row(c,rid)["ceremony_id"] is None
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_select(c,rid,authorize=allow)


def test_explicit_child_selection_and_dispatch_are_separate(installed):
    case=installed
    rid,owner=known(case)
    with case.store.database.transaction() as c:
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_browser_open(c,rid,authorize=allow)
        request=owner.claim_select(c,rid,authorize=allow)
        aid=request["authentication_attempt_id"]
        assert request["provider"]=="default" and request["expected_previous_attempt_id"] is None
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_select(c,rid,authorize=allow)
        owner.record_authentication(c,rid,"select",request,auth_response(request,"selected",child=aid),authorize=allow)
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_browser_open(c,rid,authorize=allow)
        read_current(owner,c,rid,"selected",aid)
        opened=owner.claim_browser_open(c,rid,authorize=allow)
        assert opened["attempt_id"]==aid and opened["url"].startswith("https://fixture.invalid/onboarding/browser-ceremonies/")
        assert owner._guard_browser_open(c,rid,aid,authorize=allow)==opened["url"]
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_browser_open(c,rid,authorize=allow)
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    owner=ProtectedBrowserRecoveries(journal(case,reopened))
    with reopened.database.transaction() as c:
        read_current(owner,c,rid,"selected",aid)
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_browser_open(c,rid,authorize=allow)


def test_lost_selection_requires_exact_observation_then_current_projection(installed):
    case=installed
    rid,owner=known(case)
    with case.store.database.transaction() as c:
        request=owner.claim_select(c,rid,authorize=allow)
        aid=request["authentication_attempt_id"]
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    owner=ProtectedBrowserRecoveries(journal(case,reopened))
    with reopened.database.transaction() as c:
        read=owner.auth_read(c,rid,authorize=allow,exact_child=True)
        assert read["authentication_attempt_id"]==aid
        owner.record_authentication(c,rid,"read",read,auth_response(read,"selected",child=aid),authorize=allow)
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_browser_open(c,rid,authorize=allow)
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_select(c,rid,authorize=allow)
        read_current(owner,c,rid,"selected",aid)
        assert owner.claim_browser_open(c,rid,authorize=allow)["attempt_id"]==aid
        assert c.execute("SELECT count(*) FROM protected_recovery_authentication_children").fetchone()[0]==1


def test_missing_child_does_not_authorize_reselection(installed):
    case=installed
    rid,owner=known(case)
    with case.store.database.transaction() as c:
        request=owner.claim_select(c,rid,authorize=allow)
        read=owner.auth_read(c,rid,authorize=allow,exact_child=True)
        owner.record_authentication(c,rid,"read",read,auth_response(read,"unavailable"),authorize=allow)
        assert owner.child(c,rid)["state"]=="select-claimed"
        with pytest.raises(HostedGrantUnavailable):
            read_current(owner,c,rid,"selection-required")
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_select(c,rid,authorize=allow)
        assert owner.child(c,rid)["attempt_id"]==request["authentication_attempt_id"]


def test_cancel_child_retires_only_exact_attempt_and_successor_is_explicit(installed):
    case=installed
    rid,aid,owner=selected(case)
    with case.store.database.transaction() as c:
        cancel=owner.claim_child_cancel(c,rid,authorize=allow)
        assert cancel["authentication_attempt_id"]==aid
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_browser_open(c,rid,authorize=allow)
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_child_cancel(c,rid,authorize=allow)
        owner.record_authentication(c,rid,"cancel",cancel,auth_response(cancel,"retired",child=aid),authorize=allow)
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_select(c,rid,authorize=allow)
        read_current(owner,c,rid,"retired",aid)
        successor=owner.claim_select(c,rid,authorize=allow)
        assert successor["expected_previous_attempt_id"]==aid
        assert successor["authentication_attempt_id"]!=aid
        assert owner.child(c,rid)["sequence"]==2
        with pytest.raises(HostedGrantUnavailable):
            owner.record_authentication(c,rid,"cancel",cancel,auth_response(cancel,"retired",child=aid),authorize=allow)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE protected_recovery_authentication_children SET previous_attempt_id=NULL WHERE sequence=2")
        assert owner.row(c,rid)["ceremony_id"]==successor["ceremony_id"]


def test_whole_ceremony_cancel_never_creates_replacement_and_commit_still_observable(installed):
    case=installed
    rid,aid,owner=selected(case)
    with case.store.database.transaction() as c:
        cancel=owner.claim_cancel(c,rid,authorize=allow)
        assert owner.row(c,rid)["entry_reference"] is None
        owner.record_ceremony(c,rid,"cancel",cancel,ceremony_response("cancel",cancel,presentation="retired"),authorize=allow)
        assert owner.row(c,rid)["presentation_state"]=="retired"
        read=owner.read_request(c,rid,authorize=allow)
        result=ceremony_response("read",read,presentation="retired",domain="committed")
        owner.record_ceremony(c,rid,"read",read,result,authorize=allow)
        assert owner.row(c,rid)["committed_seen"]
        assert owner.recovery.row(c,rid)["state"]=="prepared"
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_select(c,rid,authorize=allow)
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_browser_open(c,rid,authorize=allow)
        with pytest.raises(HostedGrantUnavailable):
            owner.record_ceremony(c,rid,"read",read,ceremony_response("read",read),authorize=allow)
        assert c.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0]==len(case.grants)


def test_protected_admitted_child_clears_entry_without_admitting_grants(installed):
    case=installed
    rid,aid,owner=selected(case)
    with case.store.database.transaction() as c:
        read_current(owner,c,rid,"admitted",aid)
        row=owner.row(c,rid)
        assert row["committed_seen"] and row["entry_reference"] is None and row["projection_json"] is None
        assert owner.child(c,rid)["state"]=="admitted"
        assert owner.recovery.row(c,rid)["state"]=="prepared"
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_browser_open(c,rid,authorize=allow)
        assert c.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0]==len(case.grants)


def test_expired_or_revoked_source_cannot_dispatch(installed):
    case=installed
    rid,aid,owner=selected(case)
    case.clock[0]+=timedelta(minutes=21)
    with case.store.database.transaction() as c:
        with pytest.raises(HostedGrantUnavailable):
            owner.claim_browser_open(c,rid,authorize=allow)
        read=owner.read_request(c,rid,authorize=allow)
        result=ceremony_response("read",read,presentation="expired",domain="committed")
        owner.record_ceremony(c,rid,"read",read,result,authorize=allow)
        assert owner.row(c,rid)["entry_reference"] is None
        case.store._revoke_in_transaction(c,RevocationKey(RevocationScopeType.INSTALLATION,case.scope["installation_id"]),reason="test")
        with pytest.raises(HostedGrantUnavailable):
            owner.read_request(c,rid,authorize=allow)

@pytest.mark.asyncio
async def test_effects_restart_reads_lost_mutations_and_never_reopens_browser(installed):
    from uuid import uuid4
    from app.onboarding.protected_browser_recovery import ProtectedBrowserRecoveryEffects

    case=installed
    request,owner=setup(case)
    rid=request["recovery_id"]
    journey=str(uuid4())
    calls=[]
    state={"child":None,"lost_prepare":True,"lost_select":True,"lost_cancel":True}

    class Ceremony:
        def execute(self, verb, request, *, before_send, expires_at):
            before_send()
            calls.append(("ceremony",verb,copy.deepcopy(request)))
            if verb=="prepare" and state.pop("lost_prepare",False):
                raise HostedGrantUnavailable("Lost prepare")
            before_send()
            return ceremony_response(verb,request)

    class Authentication:
        def execute(self, verb, request, *, before_send, expires_at):
            before_send()
            calls.append(("authentication",verb,copy.deepcopy(request)))
            if verb=="select":
                state["child"]=request["authentication_attempt_id"]
                state["status"]="selected"
                if state.pop("lost_select",False):
                    raise HostedGrantUnavailable("Lost selection")
            elif verb=="cancel":
                state["status"]="retired"
                if state.pop("lost_cancel",False):
                    raise HostedGrantUnavailable("Lost cancellation")
            before_send()
            return auth_response(request,state.get("status","selection-required"),child=state["child"])

    effects=ProtectedBrowserRecoveryEffects(owner,Ceremony(),Authentication())
    with pytest.raises(HostedGrantUnavailable):
        await effects.prepare(request,journey_id=journey,authorize=allow)
    reopened=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    owner=ProtectedBrowserRecoveries(journal(case,reopened))
    effects=ProtectedBrowserRecoveryEffects(owner,Ceremony(),Authentication())
    await effects.prepare(request,journey_id=journey,authorize=allow)
    assert [verb for family,verb,_ in calls if family=="ceremony"]==["prepare","read"]
    assert calls[0][2]==calls[1][2]
    await effects.observe_authentication(rid,journey_id=journey,authorize=allow)
    with pytest.raises(HostedGrantUnavailable):
        await effects.select(rid,journey_id=journey,authorize=allow)
    aid=state["child"]
    effects=ProtectedBrowserRecoveryEffects(owner,Ceremony(),Authentication())
    await effects.observe_authentication(rid,journey_id=journey,authorize=allow,exact_child=True)
    assert calls[-1][2]["authentication_attempt_id"]==aid
    with pytest.raises(HostedGrantUnavailable):
        await effects.select(rid,journey_id=journey,authorize=allow)
    opened=[]
    def dispatch(child,url,*,before_dispatch):
        before_dispatch()
        opened.append((child,url))
    with pytest.raises(HostedGrantUnavailable):
        await effects.dispatch(rid,journey_id=journey,authorize=allow,dispatch=dispatch)
    await effects.observe_authentication(rid,journey_id=journey,authorize=allow)
    await effects.dispatch(rid,journey_id=journey,authorize=allow,dispatch=dispatch)
    assert len(opened)==1 and opened[0][0]==aid
    effects=ProtectedBrowserRecoveryEffects(owner,Ceremony(),Authentication())
    await effects.observe_authentication(rid,journey_id=journey,authorize=allow)
    with pytest.raises(HostedGrantUnavailable):
        await effects.dispatch(rid,journey_id=journey,authorize=allow,dispatch=dispatch)
    assert len(opened)==1
    with pytest.raises(HostedGrantUnavailable):
        await effects.cancel_child(rid,journey_id=journey,authorize=allow)
    effects=ProtectedBrowserRecoveryEffects(owner,Ceremony(),Authentication())
    with pytest.raises(HostedGrantUnavailable):
        await effects.cancel_child(rid,journey_id=journey,authorize=allow)
    await effects.observe_authentication(rid,journey_id=journey,authorize=allow,exact_child=True)
    await effects.observe_authentication(rid,journey_id=journey,authorize=allow)
    await effects.select(rid,journey_id=journey,authorize=allow)
    assert state["child"]!=aid
    selects=[command for family,verb,command in calls if family=="authentication" and verb=="select"]
    assert len(selects)==2 and selects[1]["expected_previous_attempt_id"]==aid
    assert sum(family=="authentication" and verb=="cancel" for family,verb,_ in calls)==1
    assert len(opened)==1


def test_cleared_entry_locator_cannot_be_replaced_by_a_different_reference(installed):
    case=installed
    rid,owner=known(case)
    with case.store.database.transaction() as c:
        read=owner.read_request(c,rid,authorize=allow)
        owner.record_ceremony(c,rid,"read",read,{"profile":read["profile"],"operation_id":read["operation_id"],
            "status":"unavailable"},authorize=allow)
        assert owner.row(c,rid)["entry_reference"] is None
        response=ceremony_response("read",read)
        response["entry"]["entry_reference"]="A"*43
        with pytest.raises(HostedGrantUnavailable):
            owner.record_ceremony(c,rid,"read",read,response,authorize=allow)


def test_ceremony_delete_guard_refuses_committed_seen_ceremony(installed):
    case=installed
    rid,aid,owner=selected(case)
    with case.store.database.transaction() as c:
        read_current(owner,c,rid,"admitted",aid)
        assert owner.row(c,rid)["committed_seen"]==1
        c.execute("DELETE FROM protected_recovery_authentication_children WHERE recovery_id=?",(rid,))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("DELETE FROM protected_recovery_ceremonies WHERE recovery_id=?",(rid,))
        assert owner.row(c,rid) is not None
