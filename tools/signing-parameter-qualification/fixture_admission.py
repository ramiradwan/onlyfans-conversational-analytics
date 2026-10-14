"""Fresh, verified synthetic admission created outside the frozen consumer.

All four grants use pinned public contract fixture keys and real verification.
No production key is generated or used. Producer code is excluded from both
shipping and qualification Brain. The database uses the ordinary private-file
and SQLCipher protections (DPAPI on Windows outside explicit pytest mode).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.persistence.auth import (
    AuthorizedAccountBinding, BridgeSessionIssue, ProvisioningCandidate,
    ProvisioningCandidateState, SQLiteAuthenticationStore, WebAuthnCredential,
)
from app.provisioning.finalize import FinalizationRequest, verified_grant_bindings
from app.core.first_run import initialize_production_configuration
from app.security.account_bindings import sole_eligible_account
from app.security.companion_pairing import CompanionPairingService
from app.security.hosted_grants import HostedGrantClient
from app.security.installation_key import InstallationKeyAuthority
from app.security.local_data_key import protect_local_secret
from app.persistence.private_files import apply_private_file_security, sync_file
from contracts.loader import verify_snapshot_integrity
from fixture_installation import ORDER, PREFIX, SESSION_PURPOSE, SyntheticInstallationProvider

ROOT = Path(__file__).resolve().parents[2]
KINDS = ("installation_grant", "creator_account_binding", "membership_snapshot", "license_entitlement")
PURPOSES = ("installation-binding", "installation-binding", "membership", "license")
FIXTURE_ACCOUNT = "12345"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def mint_fixture_tokens(now: datetime, *, grant_basis_expires_in_seconds: int | None = None,
                       companion_authority_expires_in_seconds: int | None = None):
    """Return fresh tokens and public trust after checking pinned fixture bytes."""
    verify_snapshot_integrity(ROOT / "contracts")
    if now.tzinfo is None:
        raise ValueError("Qualification admission requires an aware clock")
    fixture = ROOT / "contracts/grant-profile-v1"
    trust = json.loads((fixture / "keys/trust-set.json").read_bytes())
    if trust.get("production_usable") is not False or not all(
        entry.get("fixture_only") is True for entry in trust["keys"]
    ):
        raise ValueError("Qualification requires public synthetic trust")
    tokens = {}
    issued = int(now.timestamp())
    for expiry_seconds in (grant_basis_expires_in_seconds, companion_authority_expires_in_seconds):
        if expiry_seconds is not None and (type(expiry_seconds) is not int or not 10 <= expiry_seconds <= 300):
            raise ValueError("Qualification expiry must be 10–300 real seconds")
    for kind, purpose in zip(KINDS, PURPOSES, strict=True):
        path = fixture / kind / "valid-current"
        payload = json.loads((path / "payload.json").read_bytes())
        header = json.loads((path / "protected.json").read_bytes())
        lifetime = payload["exp"] - payload["iat"]
        payload.update(iat=issued, nbf=issued, exp=issued + lifetime)
        if kind == "installation_grant" and companion_authority_expires_in_seconds is not None:
            # The normal Noise session is bounded by these retained pairing
            # grants. Preserve the normative signed lifetime and let its real
            # existing-data grace end at the requested wall-clock deadline.
            expiry = issued - 604_800 + companion_authority_expires_in_seconds
            payload.update(iat=expiry - lifetime, nbf=expiry - lifetime, exp=expiry)
        if kind == "creator_account_binding":
            payload["creator_account_id"] = FIXTURE_ACCOUNT
            payload["sub"] = f'installation:{payload["installation_id"]}:creator:{FIXTURE_ACCOUNT}'
        if kind == "membership_snapshot":
            payload["allowed_creator_account_ids"] = [FIXTURE_ACCOUNT]
            payload["roles"] = ["owner"]
            payload["scopes"] = sorted((
                "commercial:manage", "creator-binding:approve", "creator-binding:revoke",
                "installation-claim:issue", "installation:provision", "membership:manage",
                "organization:manage", "provisioning:milestone", "runtime:existing-data",
                "runtime:licensed-processing",
            ))
            if grant_basis_expires_in_seconds is not None:
                # Real normative existing-data grace, not a shifted clock or
                # database edit. Ordinary hosted admission retains exp+259200.
                expiry = issued - 259_200 + grant_basis_expires_in_seconds
                payload.update(iat=expiry - lifetime, nbf=expiry - lifetime, exp=expiry)
        material = hashlib.sha256((PREFIX + purpose).encode("ascii")).digest()
        key = ec.derive_private_key(int.from_bytes(material, "big") % (ORDER - 1) + 1, ec.SECP256R1())
        public = key.public_key().public_numbers()
        entry = next(entry for entry in trust["keys"] if entry["jwk"]["kid"] == header["kid"])
        if entry["purpose"] != purpose or (
            entry["jwk"]["x"], entry["jwk"]["y"]
        ) != (_encode(public.x.to_bytes(32, "big")), _encode(public.y.to_bytes(32, "big"))):
            raise ValueError("Published fixture derivation does not match pinned trust")
        signing_input = _encode(_canonical(header)) + "." + _encode(_canonical(payload))
        r, s = decode_dss_signature(key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256())))
        tokens[kind] = (signing_input + "." + _encode(r.to_bytes(32, "big") + min(s, ORDER - s).to_bytes(32, "big")), payload)
    return tokens, trust


class _NoHostedTransport:
    def __getattr__(self, name):
        raise RuntimeError("Qualification admission cannot contact hosted services")


@dataclass(frozen=True)
class FixtureAdmission:
    store: SQLiteAuthenticationStore
    authority: InstallationKeyAuthority
    service: CompanionPairingService
    policy: object
    account: str
    receipt: dict


def seed_admission(directory: str | Path, *, now: datetime | None = None,
                   grant_basis_expires_in_seconds: int | None = None,
                   companion_authority_expires_in_seconds: int | None = None) -> FixtureAdmission:
    """Create a new disposable durable admission, with a real advancing clock."""
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    database = root / "auth.sqlite3"
    if database.exists():
        raise ValueError("Qualification admission refuses an existing authentication database")
    now = now or datetime.now(timezone.utc)
    tokens, trust = mint_fixture_tokens(now, grant_basis_expires_in_seconds=grant_basis_expires_in_seconds,
                                      companion_authority_expires_in_seconds=companion_authority_expires_in_seconds)
    store = SQLiteAuthenticationStore(database)
    authority = InstallationKeyAuthority(store, SyntheticInstallationProvider())
    key = authority.ensure_ready()
    membership = tokens["membership_snapshot"][1]
    binding = tokens["creator_account_binding"][1]
    account = binding["creator_account_id"]
    client = HostedGrantClient(_NoHostedTransport(), authority, store, clock=lambda: now, trust_set=trust)
    references = tuple(client._verified_reference(
        token, payload, grant_type=kind,
        organization_id=payload["organization_id"], installation_id=payload["installation_id"],
        key=key, external_issuer=membership["ciam_issuer"], external_subject=membership["ciam_subject"],
        requested_account_ids=(account,),
    ) for kind, (token, payload) in tokens.items())
    store.record_verified_grants(references)
    association = "signing-parameter-qualification-association"
    store.record_provisioning_candidate(ProvisioningCandidate(
        association_request_id=association, installation_id=binding["installation_id"],
        onboarding_transaction_id="signing-parameter-qualification-onboarding",
        organization_id=binding["organization_id"], creator_account_id=account,
        state=ProvisioningCandidateState.PENDING, requested_at=now,
    ))
    store.approve_provisioning_candidate(association, resolved_at=now)
    bindings, verified_account, ids = verified_grant_bindings(
        store=store, request=FinalizationRequest(association, account)
    )
    store.record_authorized_account_binding(AuthorizedAccountBinding(
        creator_account_id=verified_account.creator_account_id,
        installation_id=binding["installation_id"], platform_creator_id=verified_account.platform_creator_id,
        association_request_id=association, grant_bundle_sha256=verified_account.grant_bundle_sha256,
        authorized_at=now, grant_reference_ids=ids,
    ))
    initialize_production_configuration(bindings=bindings, extension_id="a" * 32, data_directory=root)
    eligible = sole_eligible_account(store, now=now)
    if eligible.creator_account_id != account:
        raise ValueError("Qualification account admission is incoherent")
    # The bounded harness represents an already-authenticated local operator.
    # Its credential is never used to fabricate a WebAuthn authentication proof.
    credential = WebAuthnCredential(
        credential_id="signing-parameter-qualification-operator", principal_id=bindings.principal_id,
        external_issuer=membership["ciam_issuer"], external_subject=membership["ciam_subject"],
        installation_id=binding["installation_id"], public_key=b"QA-ONLY-ADMITTED-OPERATOR",
        signature_count=0, enrolled_at=now,
    )
    store.register_webauthn_credential(credential)
    session = store.issue_bridge_session(BridgeSessionIssue(
        credential_id=credential.credential_id, principal_id=bindings.principal_id,
        creator_account_id=account, role=bindings.bridge_role,
        expires_at=now + timedelta(minutes=20), grant_reference_ids=ids,
    ))
    receipt = {
        "qualification_only": True, "production_usable": False,
        "grant_verification": "real-current-wallclock", "verified_grant_types": list(KINDS),
        "installation_key_provider": "public-synthetic-software-test-double",
        "operator_admission": "pre-admitted-local-session-no-webauthn-ceremony",
        "issued_at": int(now.timestamp()), "grant_bundle_sha256": verified_account.grant_bundle_sha256,
        "membership_time_state": "grace" if grant_basis_expires_in_seconds is not None else "current",
    }
    if grant_basis_expires_in_seconds is not None:
        receipt["grant_basis_expires_at"] = int(now.timestamp()) + grant_basis_expires_in_seconds
    if companion_authority_expires_in_seconds is not None:
        receipt["companion_authority_expires_at"] = int(now.timestamp()) + companion_authority_expires_in_seconds
    session_path = root / "operator-session.dpapi"
    session_path.write_bytes(protect_local_secret(_canonical({
        "session_value": session.session_value, "csrf_value": session.csrf_value,
    }), purpose=SESSION_PURPOSE))
    apply_private_file_security(session_path)
    sync_file(session_path)
    receipt_path = root / "admission.json"
    receipt_path.write_bytes(_canonical(receipt))
    apply_private_file_security(receipt_path)
    sync_file(receipt_path)
    return FixtureAdmission(store, authority, CompanionPairingService(store, authority), session.policy, account, receipt)
