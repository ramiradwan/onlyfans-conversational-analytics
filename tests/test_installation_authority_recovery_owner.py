"""Durable source evidence, exact claims and refusal handling in encrypted storage."""
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
import hashlib

import pytest

from app.persistence.auth import (
    SQLiteAuthenticationStore, InstallationKeyReservation, ClaimSubmission, VerifiedGrantReference,
    ProvisioningCandidate, ProvisioningCandidateState, AuthorizedAccountBinding,
    RevocationKey, RevocationScopeType,
)
from app.persistence.installation_authority_recovery import InstallationAuthorityRecoveries, previous_authority
from app.persistence import sqlite_api as sqlite3
from app.security.hosted_grants import CLAIM_PROFILE_V2, HostedGrantUnavailable
from app.security.installation_authority_recovery import AuthorityRecoveryRefused
from test_installation_authority_recovery_client import VECTORS, response_for, wire_time
from test_source_replacement_client import Key, REGISTERED, NOW

pytestmark = [pytest.mark.ci_tier("integration"), pytest.mark.windows_compat]


def allow(**_):
    pass


@pytest.fixture(params=[False, True], ids=["no-creator", "approved-creator"])
def installed(tmp_path, request):
    scope = VECTORS["proof_cases"][0]["request"]["scope"]
    key = Key(REGISTERED, VECTORS["proof_cases"][0]).reference
    clock = [NOW]
    path = tmp_path / "auth.sqlite3"
    store = SQLiteAuthenticationStore(path, clock=lambda: clock[0])
    store.reserve_installation_key(InstallationKeyReservation(key.provider_name, key.provider_key_name, key.algorithm, key.created_at))
    store.activate_installation_key(key)
    enrolled = NOW - timedelta(days=45)
    claim_id = "0198a1b2-c3d4-7300-8000-000000000060"
    store.record_claim_submission(ClaimSubmission(claim_id, scope["enrollment_transaction_id"], scope["organization_id"],
        scope["installation_id"], enrolled, claim_profile=CLAIM_PROFILE_V2))
    store.resolve_claim_submission(claim_id, outcome=None, resolved_at=enrolled, enrolled_at=enrolled)
    prior = scope["evidence"]
    creator = prior["creator_scope"]
    common = dict(issuer="https://fixture.invalid/identity", subject="fixture-subject", installation_id=scope["installation_id"],
        valid_from=enrolled, expires_at=NOW-timedelta(days=1), verified_at=enrolled, organization_id=scope["organization_id"],
        installation_key_id=key.installation_key_id, installation_key_jkt=key.installation_key_jkt)
    def grant(kind, jti, **fields):
        return VerifiedGrantReference(reference_id="reference."+kind, grant_identifier=jti, grant_type=kind,
            grant_digest=hashlib.sha256(jti.encode()).hexdigest(), **(common | {"creator_account_id": None} | fields))
    grants = [grant("installation_grant", prior["installation_jti"]),
        grant("membership_snapshot", prior["membership_jti"], membership_id="membership.fixture",
            allowed_creator_account_ids=(creator["creator_account_id"],) if request.param else (), membership_roles=("creator_operator",))]
    if request.param:
        grants.append(grant("creator_account_binding", creator["binding_jti"], creator_account_id=creator["creator_account_id"],
            approval_id=creator["approval_id"], approval_revision=1))
    store.record_verified_grants(tuple(grants))
    if request.param:
        account = creator["creator_account_id"]
        association = "association.fixture"
        store.record_provisioning_candidate(ProvisioningCandidate(association, scope["installation_id"],
            scope["enrollment_transaction_id"], scope["organization_id"], account, ProvisioningCandidateState.PENDING, enrolled))
        store.approve_provisioning_candidate(association, resolved_at=enrolled)
        store.record_authorized_account_binding(AuthorizedAccountBinding(account, scope["installation_id"], account,
            association, "a"*64, enrolled, tuple(item.reference_id for item in grants)))
    return SimpleNamespace(store=store, path=path, clock=clock, scope=scope, grants=grants, creator=request.param)


def journal(case, store=None, origin="https://fixture.invalid"):
    return InstallationAuthorityRecoveries(store or case.store, origin=origin)


def reserve(case, owner):
    with case.store.database.transaction() as connection:
        return owner.reserve(connection, authorize=allow)


def test_exact_previous_grants_and_reservation(installed):
    case = installed
    owner = journal(case)
    with case.store.database.read() as connection:
        evidence = previous_authority(case.store, connection)
        assert evidence.membership.grant_identifier == case.grants[1].grant_identifier
        expected = dict(case.scope)
        expected["evidence"] = dict(expected["evidence"])
        if not case.creator:
            expected["evidence"]["creator_scope"] = {"kind": "none"}
        assert evidence.scope == expected
        assert connection.execute("SELECT count(*) FROM installation_authority_recoveries").fetchone()[0] == 0
    request = reserve(case, owner)
    assert request["scope"] == expected
    with case.store.database.transaction() as connection:
        with pytest.raises(HostedGrantUnavailable):
            owner.reserve(connection, authorize=allow)
        assert owner.claim(connection, request, "deliver", authorize=allow) is None
        assert owner.claim(connection, request, "prepare", authorize=allow) == 1
        assert owner.claim(connection, request, "prepare", authorize=allow) is None
    case.clock[0] += timedelta(seconds=16)
    reopened = SQLiteAuthenticationStore(case.path, clock=lambda: case.clock[0])
    recovered = journal(case, reopened)
    with reopened.database.transaction() as connection:
        assert recovered.claim(connection, request, "prepare", authorize=allow) == 2
        with pytest.raises(HostedGrantUnavailable):
            recovered.guard(connection, request, "prepare", 1, authorize=allow)
        assert recovered.guard(connection, request, "prepare", 2, authorize=allow)[0]["request_json"]
        assert recovered.claim(connection, request, "prepare", authorize=allow) is None
        result = response_for("prepare", request)
        recovered.record_preparation(connection, request, 2, result, authorize=allow)
        row = recovered.row(connection, request["recovery_id"])
        assert row["state"] == "prepared" and row["source_reference"] == result["source_reference"]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE installation_authority_recoveries SET scope_digest=?", ("0"*64,))
        assert connection.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0] == len(case.grants)


@pytest.mark.parametrize("fault", ["renewal", "revoked", "missing", "ambiguity", "actor", "claim"])
def test_recovery_refuses_unavailable_prior_authority(installed, fault):
    case = installed
    with case.store.database.transaction() as connection:
        if fault == "renewal":
            connection.execute("UPDATE verified_grant_references SET expires_at=? WHERE grant_type='installation_grant'",
                ((NOW+timedelta(days=1)).isoformat(),))
        elif fault == "revoked":
            case.store._revoke_in_transaction(connection, RevocationKey(RevocationScopeType.INSTALLATION, case.scope["installation_id"]),
                reason="test")
        elif fault == "missing":
            connection.execute("UPDATE verified_grant_references SET revoked_at=? WHERE grant_type='membership_snapshot'", (NOW.isoformat(),))
        elif fault == "ambiguity":
            item = replace(case.grants[0], reference_id="another", grant_identifier="0198a1b2-c3d4-7300-8000-000000000090",
                grant_digest="f"*64)
            case.store._insert_verified_grant(connection, item)
        elif fault == "actor":
            connection.execute("UPDATE verified_grant_references SET subject='different' WHERE grant_type='membership_snapshot'")
        else:
            connection.execute("UPDATE provisioning_claim_submissions SET enrolled_at=NULL")
        with pytest.raises(HostedGrantUnavailable):
            journal(case).reserve(connection, authorize=allow)
        assert connection.execute("SELECT count(*) FROM installation_authority_recoveries").fetchone()[0] == 0


@pytest.mark.parametrize("reason", ["installation_revoked", "membership_removed", "approval_revoked", "grant_withdrawn", "authorization_denied"])
def test_closed_refusal_is_durable_and_no_new_id(installed, reason):
    case = installed
    owner = journal(case)
    request = reserve(case, owner)
    with case.store.database.transaction() as connection:
        attempt = owner.claim(connection, request, "prepare", authorize=allow)
        owner.record_refusal(connection, request, "prepare", attempt, AuthorityRecoveryRefused(reason), authorize=allow)
        assert owner.row(connection, request["recovery_id"])["state"] == "refused"
    case.clock[0] += timedelta(days=1)
    reopened = SQLiteAuthenticationStore(case.path, clock=lambda: case.clock[0])
    owner = journal(case, reopened)
    with reopened.database.transaction() as connection:
        assert owner.row(connection, request["recovery_id"])["refusal_reason"] == reason
        assert owner.claim(connection, request, "prepare", authorize=allow) is None
        with pytest.raises(HostedGrantUnavailable):
            owner.reserve(connection, authorize=allow)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM installation_authority_recoveries")


def test_delivery_after_expiry_reads_only_original_and_preserves_not_ready(installed):
    case = installed
    owner = journal(case)
    request = reserve(case, owner)
    with case.store.database.transaction() as connection:
        attempt = owner.claim(connection, request, "prepare", authorize=allow)
        owner.record_preparation(connection, request, attempt, response_for("prepare", request), authorize=allow)
    case.clock[0] += timedelta(minutes=40)
    with case.store.database.transaction() as connection:
        assert owner.claim(connection, request, "prepare", authorize=allow) is None
        assert owner.claim(connection, request, "deliver", authorize=allow) == 1
        owner.record_refusal(connection, request, "deliver", 1, AuthorityRecoveryRefused("not_ready"), authorize=allow)
        assert owner.row(connection, request["recovery_id"])["state"] == "prepared"
        assert owner.claim(connection, request, "deliver", authorize=allow) is None
        assert connection.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0] == len(case.grants)
        with pytest.raises(HostedGrantUnavailable):
            journal(case, origin="https://other.invalid").guard(connection, request, "deliver", 1, authorize=allow)


def test_guard_rechecks_current_authority_before_delivery(installed):
    case = installed
    owner = journal(case)
    request = reserve(case, owner)
    with case.store.database.transaction() as connection:
        attempt = owner.claim(connection, request, "prepare", authorize=allow)
        owner.record_preparation(connection, request, attempt, response_for("prepare", request), authorize=allow)
    case.clock[0] += timedelta(seconds=16)
    with case.store.database.transaction() as connection:
        assert owner.claim(connection, request, "deliver", authorize=allow) == 1
        connection.execute("UPDATE verified_grant_references SET revoked_at=? WHERE grant_type='membership_snapshot'", (NOW.isoformat(),))
        with pytest.raises(HostedGrantUnavailable):
            owner.guard(connection, request, "deliver", 1, authorize=allow)

def signed_delivery(case, request, *, change=None):
    import base64
    import json
    from test_hosted_grants import signed_bundle, _token, _payload, _tamper_signature
    from app.security.hosted_grants import HostedGrantClient
    from app.persistence.auth import _new_uuid7
    from app.security.installation_authority_recovery import scope_digest
    scope = request["scope"]
    now = case.clock[0]
    instant = int(now.timestamp())
    signing = signed_bundle(iat=instant)
    key = case.store.installation_key_reference()
    verifier = HostedGrantClient(None, None, case.store, clock=lambda: case.clock[0], trust_set=signing.trust_set)
    creator = scope["evidence"]["creator_scope"]
    original_membership = case.grants[1]
    issuer, subject = original_membership.issuer, original_membership.subject
    if change == "actor":
        subject = "wrong-actor"
    member_subject = "principal:" + base64.urlsafe_b64encode(json.dumps({"issuer": issuer, "subject": subject},
        sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=").decode()
    tokens = {}
    for kind, template, typ, purpose, index in (
        ("installation_grant", signing.tokens["installation_grant"], "installation", "installation-binding", 0),
        ("membership_snapshot", signing.tokens["membership_snapshot"], "membership", "membership", 1),
        ("creator_account_binding", next(iter(signing.creator_bindings.values())), "creator-binding", "installation-binding", 0),
    ):
        if kind == "creator_account_binding" and not case.creator:
            continue
        payload = _payload(template)
        payload.update(organization_id=scope["organization_id"], installation_id=scope["installation_id"],
            installation_key_id=key.installation_key_id, installation_key_jkt=key.installation_key_jkt,
            jti=_new_uuid7(now))
        if kind == "installation_grant":
            payload["sub"] = f"installation:{scope['installation_id']}"
        elif kind == "membership_snapshot":
            payload.update(ciam_issuer=issuer, ciam_subject=subject, sub=member_subject,
                membership_id=original_membership.membership_id,
                allowed_creator_account_ids=[creator["creator_account_id"]] if case.creator else [])
            if change == "membership":
                payload["membership_id"] = "different-membership"
            if change == "accounts":
                payload["allowed_creator_account_ids"].append("other-creator")
        else:
            payload.update(creator_account_id=creator["creator_account_id"],
                approval_id=creator["approval_id"], approval_revision=creator["approval_revision"],
                sub=f"installation:{scope['installation_id']}:creator:{creator['creator_account_id']}")
            if change == "approval":
                payload["approval_id"] = "0198a1b2-c3d4-7300-8000-000000000091"
        if change == "old-jti" and kind == "installation_grant":
            payload["jti"] = case.grants[0].grant_identifier
        if change == "time" and kind == "installation_grant":
            for name in ("iat", "nbf", "exp"):
                payload[name] -= 1
        if change == "key" and kind == "installation_grant":
            payload["installation_key_id"] = "ik1.AAECAwQFBgcICQoLDA0ODw"
        token = _token(payload, f"urn:bridge-clean:grant:{typ}:v1", signing.trust_set["keys"][index]["jwk"],
            signing.purpose_keys[purpose])
        if change == "signature" and kind == "installation_grant":
            token = _tamper_signature(token)
        tokens[kind] = token
    result = {"profile": request["profile"], "recovery_id": request["recovery_id"], "scope_digest": scope_digest(scope),
        "status": "committed", "enrolled_at": wire_time(NOW-timedelta(days=45)), "committed_at": wire_time(now),
        "creator_scope": {k:v for k,v in creator.items() if k != "binding_jti"}, "grants": tokens,
        **{k:scope[k] for k in ("organization_id","installation_id","installation_key_id","installation_key_jkt","enrollment_transaction_id")}}
    return verifier, result


def prepared_delivery(case, owner):
    request = reserve(case, owner)
    with case.store.database.transaction() as connection:
        attempt = owner.claim(connection, request, "prepare", authorize=allow)
        owner.record_preparation(connection, request, attempt, response_for("prepare", request), authorize=allow)
        assert owner.browser_source(connection, request, authorize=allow)["reference"] == "A"*43
    case.clock[0] += timedelta(seconds=16)
    with case.store.database.transaction() as connection:
        attempt = owner.claim(connection, request, "deliver", authorize=allow)
        assert attempt == 1
    return request, attempt


def test_verified_recovery_admits_complete_bundle_atomically(installed):
    case = installed
    owner = journal(case)
    request, attempt = prepared_delivery(case, owner)
    verifier, result = signed_delivery(case, request)
    before = case.store.read_epoch()
    references = owner.adopt_delivery(request, attempt, result, verifier=verifier, authorize=allow)
    assert len(references) == len(case.grants)
    assert case.store.read_epoch().value == before.value + 1
    with case.store.database.read() as connection:
        row = owner.row(connection, request["recovery_id"])
        assert row["state"] == "admitted" and row["source_reference"] is None
        assert all(token not in row["admitted_grants_json"] for token in result["grants"].values())
        assert set(references) == {item["reference_id"] for item in connection.execute(
            "SELECT reference_id FROM verified_grant_references WHERE revoked_at IS NULL")}
        if case.creator:
            assert set(references) == {item["grant_reference_id"] for item in connection.execute(
                "SELECT grant_reference_id FROM authorized_account_binding_grants")}
        assert connection.execute("SELECT count(*) FROM verified_grant_references WHERE revoked_at IS NOT NULL").fetchone()[0] == len(references)
    reopened = SQLiteAuthenticationStore(case.path, clock=lambda: case.clock[0])
    with reopened.database.transaction() as connection:
        assert journal(case, reopened).row(connection, request["recovery_id"])["state"] == "admitted"
        with pytest.raises(HostedGrantUnavailable):
            journal(case, reopened).reserve(connection, authorize=allow)


@pytest.mark.parametrize("change", ["actor","membership","accounts","old-jti","time","key","signature","approval","local-withdrawal","adopt-fault"])
def test_recovery_grant_admission_rejects_changed_evidence_and_rolls_back(installed, change):
    case = installed
    if change == "approval" and not case.creator:
        pytest.skip("Creator approval only")
    owner = journal(case)
    request, attempt = prepared_delivery(case, owner)
    verifier, result = signed_delivery(case, request, change=change)
    if change == "local-withdrawal":
        with case.store.database.transaction() as connection:
            case.store._revoke_in_transaction(connection, RevocationKey(RevocationScopeType.INSTALLATION, case.scope["installation_id"]), reason="test")
    def guard(*, connection):
        if change == "adopt-fault" and connection.execute(
                "SELECT 1 FROM installation_authority_recoveries WHERE state='admitted'").fetchone():
            raise HostedGrantUnavailable("Changed before commit")
    with pytest.raises(HostedGrantUnavailable):
        owner.adopt_delivery(request, attempt, result, verifier=verifier, authorize=guard)
    with case.store.database.read() as connection:
        assert owner.row(connection, request["recovery_id"])["state"] == "prepared"
        assert connection.execute("SELECT count(*) FROM verified_grant_references").fetchone()[0] == len(case.grants)
