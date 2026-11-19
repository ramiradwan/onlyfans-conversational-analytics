"""Proved consumed receipt, not inference from absent grants."""
from datetime import timedelta
from types import SimpleNamespace
import json

import pytest

from app.persistence.auth import (SQLiteAuthenticationStore, InstallationKeyReservation, ClaimSubmission,
    RevocationKey, RevocationScopeType, ProvisioningCandidate, ProvisioningCandidateState)
from app.persistence.lost_enrollment_evidence import LostEnrollmentEvidence, BOOTSTRAP_WINDOW
from app.persistence import sqlite_api as sqlite3
from app.security.hosted_grants import CLAIM_PROFILE_V2, HostedGrantUnavailable, BootstrapRecoveryRefused
from app.security.installation_authority_recovery import exact_request
from test_installation_authority_recovery_client import VECTORS, wire_time
from test_source_replacement_client import Key, REGISTERED, NOW
from test_onboarding_enrollment_runtime import _enrollment_worker, _HandoffFixture

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


def allow(**_):
    pass


@pytest.fixture
def case(tmp_path):
    vector=VECTORS["proof_cases"][0]
    key=Key(REGISTERED,vector).reference
    clock=[NOW]
    path=tmp_path/"auth.sqlite3"
    store=SQLiteAuthenticationStore(path,clock=lambda:clock[0])
    store.reserve_installation_key(InstallationKeyReservation(key.provider_name,key.provider_key_name,key.algorithm,key.created_at))
    store.activate_installation_key(key)
    coords=vector["request"]["scope"]
    receipt={"claim_id":"0198a1b2-c3d4-7300-8000-000000000006",
        "operation_id":"0198a1b2-c3d4-7300-8000-000000000063",
        "installation_id":coords["installation_id"],"organization_id":coords["organization_id"],
        "onboarding_transaction_id":coords["enrollment_transaction_id"],"installation_key_id":key.installation_key_id,
        "installation_key_jkt":key.installation_key_jkt,"consumed_at":wire_time(NOW)}
    scope={name:receipt[name] for name in ("installation_id","organization_id","onboarding_transaction_id","installation_key_jkt")}
    scope.update(intended_creator_id="creator.test",association_request_id="0198a1b2-c3d4-7300-8000-000000000070",
        action_digests={"issue-installation-claim":"a"*64,"approve-creator-association":"b"*64},authorization_revision=1)
    return SimpleNamespace(store=store,clock=clock,path=path,key=key,scope=scope,receipt=receipt)


def owner(case):
    return LostEnrollmentEvidence(case.store,"https://fixture.invalid")


def retain(case, connection, *, authorize=allow):
    owner(case).retain(connection,case.receipt,scope=case.scope,operation_id=case.receipt["operation_id"],authorize=authorize)


def eligible(case):
    with case.store.database.read() as connection:
        return owner(case).eligible(connection)


def test_receipt_and_claim_commit_together_and_survive_original_source_cleanup(case):
    with case.store.database.transaction() as connection:
        retain(case,connection)
        saved=dict(connection.execute("SELECT * FROM lost_enrollment_evidence").fetchone())
        retain(case,connection)
        assert dict(connection.execute("SELECT * FROM lost_enrollment_evidence").fetchone())==saved
        assert connection.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0]==0
    assert case.store.claim_submission(case.receipt["claim_id"]).state.value=="submitted"
    case.clock[0]=NOW+BOOTSTRAP_WINDOW-timedelta(milliseconds=1)
    with pytest.raises(HostedGrantUnavailable):
        eligible(case)
    case.clock[0]=NOW+BOOTSTRAP_WINDOW
    with case.store.database.transaction() as connection:
        from app.persistence.source_preparations import SourcePreparations
        SourcePreparations.cleanup(connection,case.clock[0])
    case.store=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    evidence=eligible(case)
    assert evidence.enrolled_at==NOW and evidence.operation_id==case.receipt["operation_id"]
    expected={"kind":"lost-enrollment","claim_id":case.receipt["claim_id"],"creator_scope":{"kind":"none"}}
    assert evidence.scope["evidence"]==expected
    request=exact_request("0198a1b2-c3d4-7300-8000-000000000003",evidence.scope)
    assert request["scope"]["evidence"]==expected
    with case.store.database.read() as connection:
        with pytest.raises(HostedGrantUnavailable):
            owner(case).require_bootstrap(connection,operation_id=evidence.operation_id,authorize=allow)
    assert not case.store.consumed_claim_submissions()


def test_guard_failure_rolls_back_claim_and_receipt(case):
    calls=[]
    def authorize(**_):
        calls.append(True)
        if len(calls)==2:
            raise HostedGrantUnavailable("stale owner")
    with pytest.raises(HostedGrantUnavailable):
        with case.store.database.transaction() as connection:
            retain(case,connection,authorize=authorize)
    with case.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM lost_enrollment_evidence").fetchone()[0]==0
        assert connection.execute("SELECT count(*) FROM provisioning_claim_submissions").fetchone()[0]==0
        assert connection.execute("SELECT count(*) FROM onboarding_progress_outbox").fetchone()[0]==0


@pytest.mark.parametrize("field,value",[("operation_id","0198a1b2-c3d4-7300-8000-000000000090"),
    ("organization_id","different"),("installation_id","different"),("installation_key_id","ik1.AAECAwQFBgcICQoLDA0ODw"),
    ("consumed_at","2030-01-01T00:00:00.000Z")])
def test_wrong_receipt_does_not_create_claim(case,field,value):
    receipt=case.receipt|{field:value}
    with pytest.raises(HostedGrantUnavailable):
        with case.store.database.transaction() as connection:
            owner(case).retain(connection,receipt,scope=case.scope,operation_id=case.receipt["operation_id"],authorize=allow)
    assert not case.store.unresolved_claim_submissions()


@pytest.mark.parametrize("fault",["no-receipt","consumed","withdrawn","binding-mismatch","revoked","different-creator","different-origin"])
def test_absence_of_grants_is_not_enough(case,fault):
    if fault=="no-receipt":
        r=case.receipt
        case.store.record_claim_submission(ClaimSubmission(r["claim_id"],r["onboarding_transaction_id"],r["organization_id"],
            r["installation_id"],NOW,claim_profile=CLAIM_PROFILE_V2))
    else:
        with case.store.database.transaction() as connection:
            retain(case,connection)
            if fault in {"withdrawn","binding-mismatch"}:
                owner(case).record_refusal(connection,operation_id=case.receipt["operation_id"],authorize=allow,
                    reason="bootstrap_authority_withdrawn" if fault=="withdrawn" else "installation_binding_mismatch")
            elif fault=="revoked":
                case.store._revoke_in_transaction(connection,RevocationKey(RevocationScopeType.INSTALLATION,
                    case.receipt["installation_id"]),reason="test")
        if fault=="consumed":
            case.store.resolve_claim_submission(case.receipt["claim_id"],outcome=None,resolved_at=NOW,enrolled_at=NOW)
        elif fault=="different-creator":
            s=case.scope
            case.store.record_provisioning_candidate(ProvisioningCandidate(s["association_request_id"],s["installation_id"],
                s["onboarding_transaction_id"],s["organization_id"],"another",ProvisioningCandidateState.PENDING,NOW))
    case.clock[0]+=BOOTSTRAP_WINDOW
    with pytest.raises(HostedGrantUnavailable):
        if fault=="different-origin":
            with case.store.database.read() as connection:
                LostEnrollmentEvidence(case.store,"https://other.invalid").eligible(connection)
        else:
            eligible(case)


def test_immutable_receipt_and_closed_refusal(case):
    with case.store.database.transaction() as connection:
        retain(case,connection)
        owner(case).record_refusal(connection,operation_id=case.receipt["operation_id"],reason="proof_challenge_refused",authorize=allow)
        assert connection.execute("SELECT bootstrap_refusal FROM lost_enrollment_evidence").fetchone()[0] is None
        owner(case).record_refusal(connection,operation_id=case.receipt["operation_id"],reason="bootstrap_authentication_required",authorize=allow)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE lost_enrollment_evidence SET receipt_json='{}'")
        with pytest.raises(HostedGrantUnavailable):
            owner(case).record_refusal(connection,operation_id=case.receipt["operation_id"],reason="bootstrap_authority_withdrawn",authorize=allow)
    case.clock[0]+=BOOTSTRAP_WINDOW
    assert eligible(case).scope["evidence"]["kind"]=="lost-enrollment"


@pytest.mark.asyncio
async def test_actual_initial_owner_retains_before_bootstrap_and_does_not_send_past_deadline(case):
    client=_HandoffFixture()
    client.key=SimpleNamespace(reopen_existing=lambda:case.key)
    worker=_enrollment_worker(case.store,client)
    row=worker.journeys.open()
    case.receipt["installation_id"]=case.scope["installation_id"]=row["installation_id"]
    case.receipt["operation_id"]=worker.journeys.new_operation()
    row=worker.journeys.update(row["journey_id"],state="unknown",operation_id=case.receipt["operation_id"],
        scope_json=json.dumps(case.scope))
    reads=[]; sends=[]
    def receipt(request,*,before_send,key):
        before_send()
        assert key==case.key and request["operation_id"]==case.receipt["operation_id"]
        reads.append(request)
        return dict(case.receipt)
    def bootstrap(**kwargs):
        kwargs["before_send"]()
        sends.append(kwargs["recovery_request_id"])
        with case.store.database.read() as connection:
            assert owner(case).exact_receipt(connection)[1]==case.receipt
        raise BootstrapRecoveryRefused("bootstrap_authentication_required")
    client.receipt=receipt
    worker.grants.recover_bootstrap_v3=bootstrap
    with pytest.raises(BootstrapRecoveryRefused):
        await worker._recover(row)
    with case.store.database.read() as connection:
        assert owner(case).exact_receipt(connection)[0]["bootstrap_refusal"]=="bootstrap_authentication_required"
    with pytest.raises(BootstrapRecoveryRefused):
        await worker._recover(row)
    assert len(reads)==2 and sends==[case.receipt["operation_id"]]
    case.clock[0]+=BOOTSTRAP_WINDOW
    assert eligible(case).scope["evidence"]["kind"]=="lost-enrollment"
    await worker.stop()

def test_any_prior_grant_history_disqualifies_lost_enrollment(case):
    import hashlib
    from app.persistence.auth import VerifiedGrantReference
    r=case.receipt
    with case.store.database.transaction() as connection:
        retain(case,connection)
        grant=VerifiedGrantReference(reference_id="prior-installation",grant_identifier="0198a1b2-c3d4-7300-8000-000000000091",
            grant_type="installation_grant",grant_digest=hashlib.sha256(b"prior").hexdigest(),
            issuer="issuer",subject="subject",installation_id=r["installation_id"],creator_account_id=None,
            valid_from=NOW,expires_at=NOW+timedelta(days=30),verified_at=NOW,
            organization_id=r["organization_id"],installation_key_id=r["installation_key_id"],
            installation_key_jkt=r["installation_key_jkt"])
        case.store._insert_verified_grant(connection,grant)
        case.store._revoke_in_transaction(connection,RevocationKey(RevocationScopeType.VERIFIED_GRANT,grant.reference_id),reason="test")
    case.clock[0]+=BOOTSTRAP_WINDOW
    with pytest.raises(HostedGrantUnavailable):
        eligible(case)


def test_other_claim_and_scope_cannot_replace_retained_receipt(case):
    with case.store.database.transaction() as connection:
        retain(case,connection)
        with pytest.raises(HostedGrantUnavailable):
            owner(case).retain(connection,case.receipt,scope=case.scope|{"intended_creator_id":"different"},
                operation_id=case.receipt["operation_id"],authorize=allow)
    r=case.receipt
    case.store.record_claim_submission(ClaimSubmission("0198a1b2-c3d4-7300-8000-000000000099",
        r["onboarding_transaction_id"],r["organization_id"],r["installation_id"],NOW,claim_profile=CLAIM_PROFILE_V2))
    case.clock[0]+=BOOTSTRAP_WINDOW
    with pytest.raises(HostedGrantUnavailable):
        eligible(case)


@pytest.mark.asyncio
async def test_actual_initial_owner_fences_key_only_send_at_original_deadline(case):
    client=_HandoffFixture()
    client.key=SimpleNamespace(reopen_existing=lambda:case.key)
    worker=_enrollment_worker(case.store,client)
    row=worker.journeys.open()
    case.receipt["installation_id"]=case.scope["installation_id"]=row["installation_id"]
    case.receipt["operation_id"]=worker.journeys.new_operation()
    row=worker.journeys.update(row["journey_id"],state="unknown",operation_id=case.receipt["operation_id"],
        scope_json=json.dumps(case.scope))
    def receipt(request,*,before_send,key):
        before_send()
        return dict(case.receipt)
    sent=[]
    def bootstrap(**kwargs):
        # Simulate a proof exchange that crossed the boundary before mutation.
        kwargs["before_send"]()
        case.clock[0]+=BOOTSTRAP_WINDOW
        kwargs["before_send"]()
        sent.append(True)
    client.receipt=receipt
    worker.grants.recover_bootstrap_v3=bootstrap
    with pytest.raises(HostedGrantUnavailable):
        await worker._recover(row)
    assert not sent
    assert eligible(case).claim_id==case.receipt["claim_id"]
    await worker.stop()

def reserved_lost(case):
    from app.persistence.installation_authority_recovery import InstallationAuthorityRecoveries
    from test_installation_authority_recovery_client import response_for
    with case.store.database.transaction() as connection:
        retain(case,connection)
    case.clock[0]+=BOOTSTRAP_WINDOW
    journal=InstallationAuthorityRecoveries(case.store,origin="https://fixture.invalid")
    with case.store.database.transaction() as connection:
        with pytest.raises(HostedGrantUnavailable):
            journal.reserve(connection,authorize=allow)
        request=journal.reserve_lost(connection,authorize=allow)
        assert request["scope"]["evidence"]["kind"]=="lost-enrollment"
        with pytest.raises(HostedGrantUnavailable):
            journal.reserve_lost(connection,authorize=allow)
        attempt=journal.claim(connection,request,"prepare",authorize=allow)
        journal.record_preparation(connection,request,attempt,response_for("prepare",request)|{"expires_at":wire_time(case.clock[0]+timedelta(minutes=20))},authorize=allow)
    case.clock[0]+=timedelta(seconds=16)
    with case.store.database.transaction() as connection:
        attempt=journal.claim(connection,request,"deliver",authorize=allow)
        assert attempt==1
    return journal,request,attempt


def lost_delivery(case,request,*,change=None):
    # The Provider fixture is the signer and enrollment-actor authority, not
    # an invented prior local grant. No prior references exist in the database.
    from test_installation_authority_recovery_owner import signed_delivery
    signing_case=SimpleNamespace(clock=case.clock,store=case.store,creator=False,
        grants=(None,SimpleNamespace(issuer="https://fixture.invalid/identity",subject="original-actor",
            membership_id="membership.fixture")))
    verifier,result=signed_delivery(signing_case,request,change=change)
    result["enrolled_at"]=case.receipt["consumed_at"]
    return verifier,result


def test_protected_lost_delivery_atomically_completes_original_claim(case):
    journal,request,attempt=reserved_lost(case)
    verifier,result=lost_delivery(case,request)
    before=case.store.read_epoch()
    references=journal.adopt_delivery(request,attempt,result,verifier=verifier,authorize=allow)
    assert len(references)==2
    assert case.store.read_epoch().value==before.value+1
    assert case.store.claim_submission(case.receipt["claim_id"]).state.value=="consumed"
    assert case.store.claim_submission(case.receipt["claim_id"]).enrolled_at==NOW
    candidate=case.store.provisioning_candidate(case.scope["association_request_id"])
    assert candidate.creator_account_id==case.scope["intended_creator_id"] and candidate.state.value=="pending"
    with case.store.database.read() as connection:
        saved=journal.row(connection,request["recovery_id"])
        assert saved["state"]=="admitted" and saved["source_reference"] is None
        assert all(token not in saved["admitted_grants_json"] for token in result["grants"].values())
        assert connection.execute("SELECT count(*) FROM authorized_account_bindings").fetchone()[0]==0
        assert connection.execute("SELECT count(*) FROM webauthn_credentials").fetchone()[0]==0
        assert {row["grant_type"] for row in connection.execute("SELECT * FROM verified_grant_references")}=={
            "installation_grant","membership_snapshot"}
        assert connection.execute("SELECT count(*) FROM onboarding_progress_outbox WHERE milestone='enrolled'").fetchone()[0]==1
        with pytest.raises(HostedGrantUnavailable):
            journal.eligible_lost(connection)
    case.store=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    from app.persistence.installation_authority_recovery import InstallationAuthorityRecoveries
    reopened=InstallationAuthorityRecoveries(case.store,origin="https://fixture.invalid")
    with case.store.database.transaction() as connection:
        assert reopened.row(connection,request["recovery_id"])["state"]=="admitted"
        with pytest.raises(HostedGrantUnavailable):
            reopened.reserve_lost(connection,authorize=allow)


@pytest.mark.parametrize("change",["signature","key","accounts","time","enrolled-at","scope-digest","wrong-recovery","creator",
    "local-withdrawal","late-guard"])
def test_protected_lost_delivery_rejects_changed_result_and_preserves_original_claim(case,change):
    journal,request,attempt=reserved_lost(case)
    verifier,result=lost_delivery(case,request,change=change)
    if change=="enrolled-at":
        result["enrolled_at"]=wire_time(NOW-timedelta(days=1))
    if change=="scope-digest":
        result["scope_digest"]="0"*64
    if change=="wrong-recovery":
        result["recovery_id"]="0198a1b2-c3d4-7300-8000-000000000090"
    if change=="creator":
        result["creator_scope"]={"kind":"approved","creator_account_id":"creator.test",
            "approval_id":"0198a1b2-c3d4-7300-8000-000000000090","approval_revision":1}
    if change=="local-withdrawal":
        with case.store.database.transaction() as connection:
            case.store._revoke_in_transaction(connection,RevocationKey(RevocationScopeType.INSTALLATION,
                case.receipt["installation_id"]),reason="test")
    def guard(*,connection):
        if change=="late-guard" and connection.execute("SELECT 1 FROM installation_authority_recoveries WHERE state='admitted'").fetchone():
            raise HostedGrantUnavailable("Changed before commit")
    with pytest.raises(HostedGrantUnavailable):
        journal.adopt_delivery(request,attempt,result,verifier=verifier,authorize=guard)
    assert case.store.claim_submission(case.receipt["claim_id"]).state.value=="submitted"
    assert case.store.provisioning_candidate(case.scope["association_request_id"]) is None
    with case.store.database.read() as connection:
        assert journal.row(connection,request["recovery_id"])["state"]=="prepared"
        assert connection.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0]==0
        assert connection.execute("SELECT count(*) FROM onboarding_progress_outbox WHERE milestone='enrolled'").fetchone()[0]==0


def test_lost_recovery_original_delivery_and_refusal_survive_restart(case):
    from app.persistence.installation_authority_recovery import InstallationAuthorityRecoveries
    from app.security.installation_authority_recovery import AuthorityRecoveryRefused
    journal,request,attempt=reserved_lost(case)
    with case.store.database.transaction() as connection:
        journal.record_refusal(connection,request,"deliver",attempt,AuthorityRecoveryRefused("not_ready"),authorize=allow)
    case.clock[0]+=timedelta(seconds=16)
    case.store=SQLiteAuthenticationStore(case.path,clock=lambda:case.clock[0])
    journal=InstallationAuthorityRecoveries(case.store,origin="https://fixture.invalid")
    with case.store.database.transaction() as connection:
        with pytest.raises(HostedGrantUnavailable):
            journal.reserve_lost(connection,authorize=allow)
        assert journal.claim(connection,request,"deliver",authorize=allow)==2
        journal.record_refusal(connection,request,"deliver",2,AuthorityRecoveryRefused("grant_withdrawn"),authorize=allow)
    case.clock[0]+=timedelta(days=1)
    with case.store.database.transaction() as connection:
        assert journal.row(connection,request["recovery_id"])["refusal_reason"]=="grant_withdrawn"
        with pytest.raises(HostedGrantUnavailable):
            journal.reserve_lost(connection,authorize=allow)
        assert journal.claim(connection,request,"deliver",authorize=allow) is None


def test_lost_recovery_cannot_change_immutable_scope_or_admit_competing_branch(case):
    from app.security.installation_authority_recovery import exact_request
    journal,request,attempt=reserved_lost(case)
    scope=request["scope"]|{"evidence":{"kind":"lost-enrollment",
        "claim_id":"0198a1b2-c3d4-7300-8000-000000000099","creator_scope":{"kind":"none"}}}
    changed=exact_request(request["recovery_id"],scope)
    with case.store.database.read() as connection:
        with pytest.raises(HostedGrantUnavailable):
            journal.guard(connection,changed,"deliver",attempt,authorize=allow)
        with pytest.raises(HostedGrantUnavailable):
            journal.guard(connection,request,"deliver",attempt+1,authorize=allow)


def past_boundary(case):
    """Retain a receipt whose key-only bootstrap window closed five minutes before NOW."""
    case.receipt["consumed_at"]=wire_time(NOW-BOOTSTRAP_WINDOW-timedelta(minutes=5))
    case.clock[0]=NOW-timedelta(minutes=10)
    with case.store.database.transaction() as connection:
        retain(case,connection)


@pytest.mark.asyncio
async def test_protected_owner_admits_lost_enrollment_after_the_boundary_and_completes_the_claim(case):
    from test_protected_authority_setup import build, connect, recovery_command, rid
    from test_protected_browser_recovery_client import auth_response
    past_boundary(case)
    coordinator,owner,base,state=build(case)
    # Key-only bootstrap still owns the original enrollment before its boundary.
    assert "authority_recovery" not in coordinator.read(connect(coordinator))
    case.clock[0]=NOW
    session=connect(coordinator)
    op,fresh=coordinator._accept(session,recovery_command(coordinator,session))
    assert fresh and not base.admissions
    with case.store.database.read() as connection:
        hosted=json.loads(owner.recovery.row(connection,rid(op))["request_json"])
    assert hosted["scope"]["evidence"]["kind"]=="lost-enrollment"
    assert (await owner.execute(op,reconcile=False)).state=="waiting"
    assert [item[:2] for item in state["calls"]]==[("authority","prepare"),("ceremony","prepare"),("auth","read")]
    with case.store.database.transaction() as connection:
        guard=lambda **kw:owner._authorize(op,**kw)
        child=owner.browser.journal.claim_select(connection,rid(op),authorize=guard)
        aid=child["authentication_attempt_id"]
        owner.browser.journal.record_authentication(connection,rid(op),"select",child,
            auth_response(child,"selected",child=aid),authorize=guard)
        current=owner.browser.journal.auth_read(connection,rid(op),authorize=guard)
        owner.browser.journal.record_authentication(connection,rid(op),"read",current,
            auth_response(current,"admitted",child=aid),authorize=guard)
    case.clock[0]+=timedelta(seconds=16)
    state["verifier"],state["result"]=lost_delivery(case,hosted)
    result=await owner.execute(op,reconcile=True)
    assert result.state=="completed"
    coordinator._settle(op,result)
    assert case.store.claim_submission(case.receipt["claim_id"]).state.value=="consumed"
    assert case.store.provisioning_candidate(case.scope["association_request_id"]).state.value=="pending"
    with case.store.database.read() as connection:
        assert owner.recovery.row(connection,rid(op))["state"]=="admitted"
        assert connection.execute("SELECT count(*) FROM authorized_account_bindings").fetchone()[0]==0
    assert "authority_recovery" not in coordinator.read(session)
    assert not base.calls
    await owner.stop()


@pytest.mark.asyncio
async def test_protected_owner_refuses_lost_admission_when_evidence_changes_after_the_offer(case):
    from app.onboarding.coordinator import CommandRefused
    from test_protected_authority_setup import build, connect, recovery_command
    past_boundary(case)
    case.clock[0]=NOW
    coordinator,owner,base,state=build(case)
    session=connect(coordinator)
    request=recovery_command(coordinator,session)
    with case.store.database.transaction() as connection:
        owner.recovery.lost_enrollment.record_refusal(connection,operation_id=case.receipt["operation_id"],
            reason="bootstrap_authority_withdrawn",authorize=allow)
    with pytest.raises(CommandRefused):
        coordinator._accept(session,request)
    with case.store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM installation_authority_recoveries").fetchone()[0]==0
    assert not state["calls"] and not base.admissions
    await owner.stop()
