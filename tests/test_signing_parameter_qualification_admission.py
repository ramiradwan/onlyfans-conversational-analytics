"""Qualification must retain real, current grant and account authority."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.persistence.auth import RevocationKey, RevocationScopeType
from app.security.account_bindings import eligible_accounts
from app.security.grant_verifier import GrantVerificationContext, verify_grant
from app.security.runtime_policy import RuntimeAuthorizationDenied, authorized_account

pytestmark = [pytest.mark.ci_tier("fast")]
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/signing-parameter-qualification"))
from fixture_admission import FIXTURE_ACCOUNT, KINDS, mint_fixture_tokens, seed_admission  # noqa: E402
from fixture_installation import open_admitted_context  # noqa: E402


def test_current_four_grants_verify_and_tampering_refuses():
    now = datetime.now(timezone.utc)
    tokens, trust = mint_fixture_tokens(now)
    for kind in KINDS:
        token, payload = tokens[kind]
        context = GrantVerificationContext(
            expected_grant_type=kind, expected_audience=payload["aud"],
            expected_organization_id=payload["organization_id"], expected_installation_id=payload["installation_id"],
            expected_installation_key_id=payload["installation_key_id"], expected_installation_key_jkt=payload["installation_key_jkt"],
            expected_subject=payload["sub"], verifier_time=int(now.timestamp()),
            requested_operation="runtime:existing-data", requested_creator_account_ids=(FIXTURE_ACCOUNT,),
        )
        assert verify_grant(token, trust_set=trust, context=context).valid
        head, body, signature = token.split(".")
        changed = ("A" if signature[0] != "A" else "B") + signature[1:]
        assert not verify_grant(f"{head}.{body}.{changed}", trust_set=trust, context=context).valid


def test_durable_admission_reopens_and_wrong_account_refuses(tmp_path):
    admission = seed_admission(tmp_path)
    reopened = open_admitted_context(tmp_path)
    assert reopened.account == admission.account
    assert reopened.authority.ensure_ready() == admission.authority.ensure_ready()
    assert len(reopened.store.verified_grants()) == 4
    assert reopened.receipt["production_usable"] is False
    assert reopened.receipt["grant_bundle_sha256"] != "0" * 64
    with pytest.raises(RuntimeAuthorizationDenied):
        authorized_account(reopened.policy, "other-account")
    with pytest.raises(ValueError, match="existing authentication"):
        seed_admission(tmp_path)


def test_expiry_and_revocation_remove_account_authority(tmp_path):
    admission = seed_admission(tmp_path)
    now = datetime.now(timezone.utc)
    assert eligible_accounts(admission.store, now=now)
    assert not eligible_accounts(admission.store, now=now + timedelta(days=40))
    reference = admission.store.verified_grants()[0]
    admission.store.revoke(RevocationKey(RevocationScopeType.VERIFIED_GRANT, reference.reference_id))
    assert not eligible_accounts(admission.store, now=now)


def test_normal_session_reauthentication_tracks_epoch_and_still_refuses_revocation(tmp_path):
    seed_admission(tmp_path)
    reopened = open_admitted_context(tmp_path)
    before = reopened.policy
    reopened.store.revoke(RevocationKey(RevocationScopeType.PRINCIPAL, "unrelated-qa-principal"))
    assert not reopened.store.bridge_session_is_current(before)
    current = reopened.refresh_policy()
    assert current.identity.session_id == before.identity.session_id
    assert reopened.store.bridge_session_is_current(current)
    reopened.store.revoke(RevocationKey(RevocationScopeType.PRINCIPAL, current.identity.principal_id))
    with pytest.raises(ValueError, match="session is not current"):
        reopened.refresh_policy()


def test_short_real_expiry_uses_verified_normative_grace_without_clock_override(tmp_path):
    now = datetime.now(timezone.utc)
    admission = seed_admission(tmp_path, now=now, grant_basis_expires_in_seconds=120)
    membership = next(grant for grant in admission.store.verified_grants() if grant.grant_type == "membership_snapshot")
    assert membership.expires_at == datetime.fromtimestamp(int(now.timestamp()) + 120, timezone.utc)
    assert admission.receipt["membership_time_state"] == "grace"
    # Membership limits account/history eligibility independently of the
    # companion channel, which pins the installation and creator-binding grants.
    assert admission.store.bridge_session_is_current(admission.policy)
    assert eligible_accounts(admission.store, now=now)
    assert not eligible_accounts(admission.store, now=now + timedelta(seconds=121))


def test_short_companion_authority_is_a_real_verified_pairing_grant_expiry(tmp_path):
    now = datetime.now(timezone.utc)
    admission = seed_admission(tmp_path, now=now, companion_authority_expires_in_seconds=120)
    installation = next(grant for grant in admission.store.verified_grants() if grant.grant_type == "installation_grant")
    assert installation.expires_at == datetime.fromtimestamp(int(now.timestamp()) + 120, timezone.utc)
    assert admission.receipt["companion_authority_expires_at"] == int(now.timestamp()) + 120
    assert admission.receipt["membership_time_state"] == "current"
    assert eligible_accounts(admission.store, now=now)
    assert not eligible_accounts(admission.store, now=now + timedelta(seconds=121))
