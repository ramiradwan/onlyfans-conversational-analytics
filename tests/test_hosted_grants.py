from __future__ import annotations

import base64
import gzip
import hashlib
import json
import struct
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.persistence.auth import InstallationKeyReference, SQLiteAuthenticationStore
from app.security.hosted_grants import (
    CLAIM_PROFILE,
    CREATOR_ASSOCIATION_PROFILE,
    PROOF_AUDIENCE,
    PROOF_PROFILE,
    REFRESH_PROFILE,
    ClaimConsumption,
    CreatorAssociationPending,
    CreatorAssociationRefused,
    CreatorAssociationRequest,
    DeviceMetadata,
    GrantVerificationRefused,
    HostedGrantClient,
    HostedGrantUnavailable,
    HTTPXHostedTransport,
    InstallationClaim,
    InstallationClaimReplay,
    TransportResponse,
    _PROOF_DOMAIN,
)
from app.security.installation_key import InstallationProof
from app.security.runtime_policy import AuthContext


_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)
_INSTANT = datetime.fromtimestamp(1_784_332_900, timezone.utc)
_ORGANIZATION_ID = "0198a1b2-c3d4-7000-8000-000000000001"
_INSTALLATION_ID = "0198a1b2-c3d4-7000-8000-000000000002"
_ACCOUNT_ID = "creator-account-fixture-001"
_SECOND_ACCOUNT_ID = "creator-account-fixture-002"
_EXTERNAL_ISSUER = "https://identity.test.invalid/fixture-tenant"
_EXTERNAL_SUBJECT = "fixture-customer-subject-001"

# Every proof purpose the client is allowed to use, listed explicitly so a
# fixture never derives its expectations from the production module it is
# meant to be checking.
_RECOGNIZED_PURPOSES = {
    "installation-claim-consume",
    "creator-association-request",
    "creator-association-status",
    "installation-grant-refresh",
    "creator-account-binding-refresh",
    "membership-snapshot-refresh",
    "license-entitlement-refresh",
}


def _proof_purpose(canonical: bytes) -> str:
    """Extract the purpose field from a signed proof's canonical bytes."""
    assert canonical.startswith(_PROOF_DOMAIN)
    offset = len(_PROOF_DOMAIN)
    for _ in range(4):  # skip challenge, method, path, body-digest fields
        (length,) = struct.unpack_from("!I", canonical, offset)
        offset += 4 + length
    (length,) = struct.unpack_from("!I", canonical, offset)
    offset += 4
    return canonical[offset : offset + length].decode("ascii")


@dataclass(frozen=True)
class SignedBundle:
    tokens: dict[str, str] = field(repr=False)
    creator_bindings: dict[str, str] = field(repr=False)
    replacement_membership: str = field(repr=False)
    trust_set: dict[str, object]
    installation_key: InstallationKeyReference
    purpose_keys: dict[str, ec.EllipticCurvePrivateKey] = field(
        default_factory=dict, repr=False
    )


class FakeProofAuthority:
    def __init__(self, reference: InstallationKeyReference) -> None:
        self.reference = reference
        self.signed_values: list[bytes] = []

    def ensure_ready(self) -> InstallationKeyReference:
        return self.reference

    def sign_challenge(self, challenge: bytes) -> InstallationProof:
        self.signed_values.append(challenge)
        purpose = _proof_purpose(challenge)
        assert purpose in _RECOGNIZED_PURPOSES, f"unrecognized proof purpose: {purpose!r}"
        return InstallationProof(
            self.reference.installation_key_id,
            "ES256",
            b"\x01" * 64,
        )


class StoredClaimTransport:
    def __init__(self, bundle: SignedBundle, claim: InstallationClaim) -> None:
        self.bundle = bundle
        self.claim = claim
        self._lock = Lock()
        self._consumed = False
        self.claim_attempts = 0
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.refresh_mode: str = "timeout"
        self.association_request_mode = "success"
        self.association_status_mode = "success"
        self.association_accounts: dict[str, str] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object],
    ) -> TransportResponse:
        assert method == "POST"
        self.requests.append((path, json_body))
        if path == self.claim.consume_path:
            # The secret is the only claim field a caller could get wrong while
            # every other field still parses, so it is checked rather than
            # echoed.
            submitted = json_body.get("request")
            if (
                not isinstance(submitted, dict)
                or submitted.get("claim_secret") != self.claim.claim_secret
            ):
                return _json_response(
                    401, {"detail": "installation_claim_secret_rejected"}
                )
            with self._lock:
                self.claim_attempts += 1
                if self._consumed:
                    return _json_response(
                        409, {"detail": "installation_claim_already_consumed"}
                    )
                self._consumed = True
            return _json_response(
                200,
                {
                    "profile": CLAIM_PROFILE,
                    "status": "consumed",
                    "claim_id": self.claim.claim_id,
                    "onboarding_transaction_id": self.claim.onboarding_transaction_id,
                    "organization_id": self.claim.organization_id,
                    "installation_id": self.claim.installation_id,
                    "installation_key_id": self.bundle.installation_key.installation_key_id,
                    "installation_key_jkt": self.bundle.installation_key.installation_key_jkt,
                    "consumed_at": "2026-07-18T00:01:00.000Z",
                    "grants": self.bundle.tokens,
                    "bootstrap_config_version": "1.0.0",
                },
            )
        if path.endswith("/proof-challenges"):
            purpose = json_body["purpose"]
            if purpose not in _RECOGNIZED_PURPOSES:
                # An unrecognised purpose gets a syntactically valid
                # challenge whose echoed "purpose" cannot match the
                # request. HostedGrantClient._validated_challenge's own
                # purpose-equality check then refuses it, so the failure
                # is attributable to that mismatch instead of collapsing
                # into the same generic transport-exception path every
                # other malfunction produces (that path is deliberately
                # swallowed by HostedGrantClient._request's credential-leak
                # guard and cannot distinguish causes).
                return _json_response(
                    201,
                    {
                        "profile": PROOF_PROFILE,
                        "purpose": "unrecognized-proof-purpose",
                        "installation_id": self.claim.installation_id,
                        "challenge": _b64url(b"a" * 32),
                        "audience": PROOF_AUDIENCE,
                        "issued_at": "2026-07-18T00:01:00.000Z",
                        "expires_at": "2026-07-18T00:02:00.000Z",
                    },
                )
            if purpose in {
                "creator-association-request",
                "creator-association-status",
            }:
                if json_body["profile"] != "urn:bridge-clean:provisioning-proof:v1":
                    return _json_response(422, {"detail": "invalid_proof_profile"})
                return _json_response(
                    201,
                    {
                        "profile": "urn:bridge-clean:provisioning-proof:v1",
                        "purpose": purpose,
                        "installation_id": self.claim.installation_id,
                        "challenge": _b64url(b"a" * 32),
                        "audience": "urn:bridge-clean:commercial-control-plane:provisioning-v2",
                        "issued_at": "2026-07-18T00:01:00.000Z",
                        "expires_at": "2026-07-18T00:02:00.000Z",
                    },
                )
            if self.refresh_mode == "timeout":
                raise TimeoutError(f"request failed: {self.claim.claim_secret}")
            if self.refresh_mode == "unreachable":
                raise OSError(f"host unavailable: {self.claim.claim_secret}")
            if self.refresh_mode == "server_error":
                return _json_response(503, {"detail": self.claim.claim_secret})
            if self.refresh_mode == "malformed":
                return TransportResponse(201, b"{", "application/json")
            if self.refresh_mode == "unsigned_denial":
                return _json_response(403, {"denial_jws": "not-a-signed-denial"})
            return _json_response(
                201,
                {
                    "profile": PROOF_PROFILE,
                    "purpose": purpose,
                    "installation_id": self.claim.installation_id,
                    "challenge": _b64url(b"r" * 32),
                    "audience": PROOF_AUDIENCE,
                    "issued_at": "2026-07-18T00:01:00.000Z",
                    "expires_at": "2026-07-18T00:02:00.000Z",
                },
            )
        if path == "/v1/grants/membership:refresh":
            content_type = {
                "wrong_content_type": "text/html",
                "json_charset": "application/json; charset=utf-8",
            }.get(self.refresh_mode, "application/json")
            return _json_response(
                200,
                {
                    "profile": REFRESH_PROFILE,
                    "request_id": "0198a1b2-c3d4-7500-8000-000000000001",
                    "grant_type": "membership_snapshot",
                    "previous_jti": "0198a1b2-c3d4-7100-8000-000000000003",
                    "grant": self.bundle.replacement_membership,
                    "server_time": "2026-07-18T00:01:00.000Z",
                    "refresh_after": "2026-07-18T06:01:00.000Z",
                },
                content_type,
            )
        request_path = f"/v1/installations/{self.claim.installation_id}/creator-associations"
        if path == request_path:
            if self.association_request_mode == "refused":
                return _json_response(401, {"detail": "creator_association_refused"})
            if self.association_request_mode == "pending":
                return _json_response(409, {"detail": "creator_association_pending"})
            if self.association_request_mode == "unavailable":
                return _json_response(503, {"detail": "installation_claim_unavailable"})
            body = json_body["request"]
            assert isinstance(body, dict)
            request_id = body["association_request_id"]
            account_id = body["creator_account_id"]
            assert isinstance(request_id, str)
            assert isinstance(account_id, str)
            self.association_accounts[request_id] = account_id
            return _json_response(
                202,
                {
                    "profile": CREATOR_ASSOCIATION_PROFILE,
                    "association_request_id": request_id,
                    "organization_id": self.claim.organization_id,
                    "installation_id": self.claim.installation_id,
                    "creator_account_id": account_id,
                    "status": "pending",
                    "updated_at": "2026-07-18T00:01:00.000Z",
                    "creator_account_binding": None,
                },
            )
        status_prefix = f"{request_path}/"
        if path.startswith(status_prefix):
            if self.association_status_mode == "refused":
                return _json_response(401, {"detail": "creator_association_refused"})
            if self.association_status_mode == "unavailable":
                return _json_response(503, {"detail": "installation_claim_unavailable"})
            request_id = path.removeprefix(status_prefix)
            account_id = self.association_accounts[request_id]
            return _json_response(
                200,
                {
                    "profile": CREATOR_ASSOCIATION_PROFILE,
                    "association_request_id": request_id,
                    "organization_id": self.claim.organization_id,
                    "installation_id": self.claim.installation_id,
                    "creator_account_id": account_id,
                    "status": "approved",
                    "updated_at": "2026-07-18T00:02:00.000Z",
                    "creator_account_binding": self.bundle.creator_bindings[account_id],
                },
            )
        raise AssertionError(f"unexpected request path: {path}")


@pytest.fixture
def bundle() -> SignedBundle:
    return signed_bundle(iat=1_784_332_800)


def signed_bundle(*, iat: int) -> SignedBundle:
    """Mint one trust set and the grant tuple it verifies, timed from `iat`.

    Grant lifetimes are stated here rather than read from
    `app.security.grant_verifier`, so a changed profile lifetime shows up as a
    refused time contract instead of being followed silently.
    """

    installation_lifetime = 2_592_000
    creator_binding_lifetime = 604_800
    membership_lifetime = 86_400
    license_lifetime = 86_400
    purpose_keys = {
        purpose: ec.generate_private_key(ec.SECP256R1())
        for purpose in ("installation-binding", "membership", "license")
    }
    trust_entries = [
        _trust_entry(purpose, purpose_keys[purpose])
        for purpose in ("installation-binding", "membership", "license")
    ]
    trust_set: dict[str, object] = {
        "profile": "urn:bridge-clean:grant-profile:v1",
        "keys": trust_entries,
        "production_usable": False,
    }
    installation_key = _installation_key_reference()
    common: dict[str, object] = {
        "profile": "urn:bridge-clean:grant-profile:v1",
        "iss": "urn:bridge-clean:commercial-control-plane",
        "organization_id": _ORGANIZATION_ID,
        "installation_id": _INSTALLATION_ID,
        "installation_key_id": installation_key.installation_key_id,
        "installation_key_jkt": installation_key.installation_key_jkt,
        "iat": iat,
        "nbf": iat,
    }
    membership_claims = {
        **common,
        "aud": "urn:bridge-clean:local-brain:membership",
        "exp": iat + membership_lifetime,
        "grant_type": "membership_snapshot",
        "jti": "0198a1b2-c3d4-7100-8000-000000000003",
        "membership_id": "0198a1b2-c3d4-7200-8000-000000000002",
        "ciam_issuer": _EXTERNAL_ISSUER,
        "ciam_subject": _EXTERNAL_SUBJECT,
        "roles": ["creator_operator"],
        "scopes": ["runtime:existing-data", "runtime:licensed-processing"],
        "allowed_creator_account_ids": [_ACCOUNT_ID, _SECOND_ACCOUNT_ID],
        "sub": _membership_subject(),
    }
    tokens = {
        "installation_grant": _token(
            {
                **common,
                "aud": "urn:bridge-clean:local-brain:installation",
                "exp": iat + installation_lifetime,
                "grant_type": "installation_grant",
                "jti": "0198a1b2-c3d4-7100-8000-000000000001",
                "sub": f"installation:{_INSTALLATION_ID}",
            },
            "urn:bridge-clean:grant:installation:v1",
            trust_entries[0]["jwk"],
            purpose_keys["installation-binding"],
        ),
        "membership_snapshot": _token(
            membership_claims,
            "urn:bridge-clean:grant:membership:v1",
            trust_entries[1]["jwk"],
            purpose_keys["membership"],
        ),
        "license_entitlement": _token(
            {
                **common,
                "aud": "urn:bridge-clean:local-brain:license",
                "entitlement_id": "0198a1b2-c3d4-7200-8000-000000000003",
                "exp": iat + license_lifetime,
                "features": [
                    "agent-capture",
                    "analytics-processing",
                    "command-execution",
                    "ingestion",
                ],
                "grant_type": "license_entitlement",
                "jti": "0198a1b2-c3d4-7100-8000-000000000004",
                "product_id": "bridge-clean",
                "sub": (
                    f"organization:{_ORGANIZATION_ID}:"
                    f"installation:{_INSTALLATION_ID}"
                ),
            },
            "urn:bridge-clean:grant:license:v1",
            trust_entries[2]["jwk"],
            purpose_keys["license"],
        ),
    }
    replacement_membership = _token(
        {
            **membership_claims,
            "jti": "0198a1b2-c3d4-7100-8000-000000000005",
        },
        "urn:bridge-clean:grant:membership:v1",
        trust_entries[1]["jwk"],
        purpose_keys["membership"],
    )
    creator_bindings = {
        account_id: _token(
            {
                **common,
                "aud": "urn:bridge-clean:local-brain:creator-binding",
                "approval_id": approval_id,
                "approval_revision": 1,
                "creator_account_id": account_id,
                "exp": iat + creator_binding_lifetime,
                "grant_type": "creator_account_binding",
                "jti": grant_id,
                "sub": f"installation:{_INSTALLATION_ID}:creator:{account_id}",
            },
            "urn:bridge-clean:grant:creator-binding:v1",
            trust_entries[0]["jwk"],
            purpose_keys["installation-binding"],
        )
        for account_id, approval_id, grant_id in (
            (
                _ACCOUNT_ID,
                "0198a1b2-c3d4-7600-8000-000000000001",
                "0198a1b2-c3d4-7700-8000-000000000001",
            ),
            (
                _SECOND_ACCOUNT_ID,
                "0198a1b2-c3d4-7600-8000-000000000002",
                "0198a1b2-c3d4-7700-8000-000000000002",
            ),
        )
    }
    return SignedBundle(
        tokens, creator_bindings, replacement_membership, trust_set, installation_key,
        purpose_keys,
    )


@pytest.fixture
def claim() -> InstallationClaim:
    return signed_claim()


def signed_claim() -> InstallationClaim:
    """Build the single-use claim `StoredClaimTransport` answers."""

    claim_id = "0198a1b2-c3d4-7300-8000-000000000001"
    return InstallationClaim(
        claim_id=claim_id,
        claim_secret=_b64url(b"s" * 32),
        challenge=_b64url(b"c" * 32),
        onboarding_transaction_id="0198a1b2-c3d4-7400-8000-000000000001",
        organization_id=_ORGANIZATION_ID,
        installation_id=_INSTALLATION_ID,
        consume_path=f"/v1/installation-claims/{claim_id}:consume",
    )


def signed_creator_account_ids() -> tuple[str, str]:
    """The creator accounts `signed_bundle` binds, in membership order."""

    return _ACCOUNT_ID, _SECOND_ACCOUNT_ID


@pytest.fixture
def identity() -> AuthContext:
    return AuthContext(
        principal_id="principal-1",
        creator_account_id=_ACCOUNT_ID,
        role="creator",
    )


@pytest.fixture
def device() -> DeviceMetadata:
    return DeviceMetadata("windows", "1.0.0", "Local workstation")


def _client(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
) -> tuple[HostedGrantClient, SQLiteAuthenticationStore, StoredClaimTransport, FakeProofAuthority]:
    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: _INSTANT)
    transport = StoredClaimTransport(bundle, claim)
    authority = FakeProofAuthority(bundle.installation_key)
    client = HostedGrantClient(
        transport,
        authority,
        store,
        clock=lambda: _INSTANT,
        trust_set=bundle.trust_set,
    )
    return client, store, transport, authority


def test_tampered_grant_is_refused_before_any_reference_is_stored(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    identity: AuthContext,
    device: DeviceMetadata,
) -> None:
    tampered = dict(bundle.tokens)
    tampered["membership_snapshot"] = _tamper_signature(
        tampered["membership_snapshot"]
    )
    altered = SignedBundle(
        tampered,
        bundle.creator_bindings,
        bundle.replacement_membership,
        bundle.trust_set,
        bundle.installation_key,
    )
    client, store, _, _ = _client(tmp_path, altered, claim)

    with pytest.raises(GrantVerificationRefused) as caught:
        client.consume_claim(claim, device, identity=identity)

    with store.database.read() as connection:
        stored = connection.execute(
            "SELECT COUNT(*) AS count FROM verified_grant_references"
        ).fetchone()["count"]
    assert caught.value.result == "invalid_signature"
    assert stored == 0
    assert claim.claim_secret not in str(caught.value)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("aud", "urn:bridge-clean:local-brain:wrong", "audience_mismatch"),
        ("sub", "installation:wrong", "subject_mismatch"),
    ],
)
def test_audience_and_subject_have_separate_verifier_results(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    identity: AuthContext,
    device: DeviceMetadata,
    field: str,
    value: str,
    expected: str,
) -> None:
    payload = _payload(bundle.tokens["installation_grant"])
    payload[field] = value
    signing_key, trust_entry = _new_signing_key("installation-binding")
    token = _token(
        payload,
        "urn:bridge-clean:grant:installation:v1",
        trust_entry["jwk"],
        signing_key,
    )
    trust_set = dict(bundle.trust_set)
    trust_set["keys"] = [trust_entry, *bundle.trust_set["keys"][1:]]
    tokens = dict(bundle.tokens)
    tokens["installation_grant"] = token
    altered = SignedBundle(
        tokens,
        bundle.creator_bindings,
        bundle.replacement_membership,
        trust_set,
        bundle.installation_key,
    )
    client, store, _, _ = _client(tmp_path, altered, claim)

    with pytest.raises(GrantVerificationRefused) as caught:
        client.consume_claim(claim, device, identity=identity)

    assert caught.value.result == expected
    with store.database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM verified_grant_references"
        ).fetchone()["count"] == 0


@pytest.mark.parametrize(
    "refresh_mode",
    [
        "unreachable",
        "timeout",
        "server_error",
        "malformed",
        "unsigned_denial",
        "wrong_content_type",
    ],
)
def test_transient_refresh_failure_keeps_the_current_policy_usable(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    identity: AuthContext,
    device: DeviceMetadata,
    refresh_mode: str,
) -> None:
    client, store, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    assert isinstance(consumed, ClaimConsumption)
    assert consumed.policy is not None
    membership_reference = consumed.grant_reference_ids[1]
    transport.refresh_mode = refresh_mode

    refreshed = client.refresh_grant(
        identity,
        consumed.grant_reference_ids,
        membership_reference,
    )

    assert refreshed.state == "unknown"
    assert refreshed.policy == consumed.policy
    assert refreshed.policy is not None
    assert store.runtime_policy_is_current(refreshed.policy)
    assert store.verified_grant(membership_reference) is not None
    with store.database.read() as connection:
        row = connection.execute(
            "SELECT revoked_at FROM verified_grant_references WHERE reference_id = ?",
            (membership_reference,),
        ).fetchone()
        stored = connection.execute(
            "SELECT COUNT(*) AS count FROM verified_grant_references"
        ).fetchone()["count"]
    assert row["revoked_at"] is None
    assert stored == 3
    assert refreshed.grant_reference_ids == consumed.grant_reference_ids
    assert claim.claim_secret not in str(refreshed)
    assert transport.refresh_mode == refresh_mode


def test_replayed_claim_is_refused_by_the_compare_and_consume_result(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    identity: AuthContext,
    device: DeviceMetadata,
) -> None:
    client, store, transport, _ = _client(tmp_path, bundle, claim)
    first = client.consume_claim(claim, device, identity=identity)

    with pytest.raises(InstallationClaimReplay) as caught:
        client.consume_claim(claim, device, identity=identity)

    assert transport.claim_attempts == 2
    assert len(first.grant_reference_ids) == 3
    with store.database.read() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM verified_grant_references"
        ).fetchone()["count"] == 3
    assert claim.claim_secret not in str(caught.value)


def test_transport_exception_does_not_expose_the_claim_secret(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    device: DeviceMetadata,
) -> None:
    class EchoingTransport:
        def request(
            self,
            method: str,
            path: str,
            *,
            json_body: dict[str, object],
        ) -> TransportResponse:
            raise RuntimeError(f"failed request: {json_body}")

    store = SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=lambda: _INSTANT)
    client = HostedGrantClient(
        EchoingTransport(),
        FakeProofAuthority(bundle.installation_key),
        store,
        clock=lambda: _INSTANT,
        trust_set=bundle.trust_set,
    )

    with pytest.raises(HostedGrantUnavailable) as caught:
        client.consume_claim(claim, device)

    assert str(caught.value) == "Hosted request is unavailable"
    assert claim.claim_secret not in str(caught.value)


@pytest.mark.parametrize("refresh_mode", ["success", "json_charset"])
def test_verified_refresh_replaces_the_reference_and_policy(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    identity: AuthContext,
    device: DeviceMetadata,
    refresh_mode: str,
) -> None:
    client, store, transport, authority = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    assert consumed.policy is not None
    previous_reference = consumed.grant_reference_ids[1]
    transport.refresh_mode = refresh_mode

    refreshed = client.refresh_grant(
        identity,
        consumed.grant_reference_ids,
        previous_reference,
    )

    assert refreshed.state == "updated"
    assert refreshed.grant_reference_ids[1] != previous_reference
    assert refreshed.policy is not None
    assert store.runtime_policy_is_current(refreshed.policy)
    assert not store.runtime_policy_is_current(consumed.policy)
    with store.database.read() as connection:
        old = connection.execute(
            "SELECT revoked_at FROM verified_grant_references WHERE reference_id = ?",
            (previous_reference,),
        ).fetchone()
    assert old["revoked_at"] is not None
    assert len(authority.signed_values) == 2
    assert claim.claim_secret.encode("ascii") not in authority.signed_values[0]


def _association(
    *,
    request_id: str,
    account_id: str = _ACCOUNT_ID,
) -> CreatorAssociationRequest:
    return CreatorAssociationRequest(
        association_request_id=request_id,
        onboarding_transaction_id="0198a1b2-c3d4-7400-8000-000000000001",
        organization_id=_ORGANIZATION_ID,
        installation_id=_INSTALLATION_ID,
        creator_account_id=account_id,
    )


def test_creator_binding_acquisition_defers_local_persistence_to_its_caller(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    identity: AuthContext,
    device: DeviceMetadata,
) -> None:
    client, store, _, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    membership_reference_id = consumed.grant_reference_ids[1]
    first_request = _association(
        request_id="0198a1b2-c3d4-7800-8000-000000000001"
    )
    second_request = _association(
        request_id="0198a1b2-c3d4-7800-8000-000000000002",
        account_id=_SECOND_ACCOUNT_ID,
    )

    client.request_creator_association(first_request)
    first = client.acquire_creator_account_binding(
        first_request, membership_reference_id=membership_reference_id
    )
    client.request_creator_association(second_request)
    second = client.acquire_creator_account_binding(
        second_request, membership_reference_id=membership_reference_id
    )

    assert first.creator_account_id == _ACCOUNT_ID
    assert second.creator_account_id == _SECOND_ACCOUNT_ID
    assert not any(
        grant.grant_type == "creator_account_binding"
        for grant in store.verified_grants()
    )


def test_tampered_creator_binding_is_refused_before_it_is_stored(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    identity: AuthContext,
    device: DeviceMetadata,
) -> None:
    client, store, _, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    association = _association(
        request_id="0198a1b2-c3d4-7800-8000-000000000005"
    )
    bundle.creator_bindings[_ACCOUNT_ID] = _tamper_signature(
        bundle.creator_bindings[_ACCOUNT_ID]
    )
    client.request_creator_association(association)

    with pytest.raises(GrantVerificationRefused) as caught:
        client.acquire_creator_account_binding(
            association, membership_reference_id=consumed.grant_reference_ids[1]
        )

    assert caught.value.result == "invalid_signature"
    assert not any(
        grant.grant_type == "creator_account_binding"
        for grant in store.verified_grants()
    )


def test_valid_creator_binding_for_another_account_is_refused_before_storage(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    identity: AuthContext,
    device: DeviceMetadata,
) -> None:
    client, store, _, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    association = _association(
        request_id="0198a1b2-c3d4-7800-8000-000000000006"
    )
    # The second binding is validly signed, but it names a different account.
    bundle.creator_bindings[_ACCOUNT_ID] = bundle.creator_bindings[_SECOND_ACCOUNT_ID]
    client.request_creator_association(association)

    refusal: str | None = None
    try:
        client.acquire_creator_account_binding(
            association, membership_reference_id=consumed.grant_reference_ids[1]
        )
    except GrantVerificationRefused as error:
        refusal = error.result

    assert refusal == "creator_account_mismatch"
    assert not any(
        grant.grant_type == "creator_account_binding"
        for grant in store.verified_grants()
    )


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("refused", CreatorAssociationRefused),
        ("pending", CreatorAssociationPending),
        ("unavailable", HostedGrantUnavailable),
    ],
)
def test_creator_association_request_refusal_paths_are_named(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    mode: str,
    error: type[Exception],
) -> None:
    client, _, transport, _ = _client(tmp_path, bundle, claim)
    transport.association_request_mode = mode

    with pytest.raises(error):
        client.request_creator_association(
            _association(request_id="0198a1b2-c3d4-7800-8000-000000000003")
        )


@pytest.mark.parametrize("mode", ["refused", "unavailable"])
def test_creator_binding_status_refusal_paths_are_named(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    identity: AuthContext,
    device: DeviceMetadata,
    mode: str,
) -> None:
    client, _, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    association = _association(
        request_id="0198a1b2-c3d4-7800-8000-000000000004"
    )
    client.request_creator_association(association)
    transport.association_status_mode = mode
    expected = (
        CreatorAssociationRefused if mode == "refused" else HostedGrantUnavailable
    )

    with pytest.raises(expected):
        client.acquire_creator_account_binding(
            association, membership_reference_id=consumed.grant_reference_ids[1]
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://control.example",
        "https://control.example/",
        "https://control.example:8443",
    ],
)
def test_concrete_transport_is_constructed_against_an_https_origin(
    base_url: str,
) -> None:
    transport = HTTPXHostedTransport(base_url)
    try:
        client_base_url = transport._client.base_url
    finally:
        transport.close()

    assert client_base_url.scheme == "https"
    assert client_base_url.host == "control.example"
    assert client_base_url.path == "/"
    assert transport._client.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://control.example",
        "https://operator@control.example",
        "https://operator:opaque-value@control.example",
        "https://:opaque-value@control.example",
        "https://control.example/v1",
        "https://control.example/?query=1",
        "https://control.example/#fragment",
        "https://",
        "control.example",
        "not a base url",
    ],
)
def test_concrete_transport_refuses_a_base_url_that_is_not_a_bare_origin(
    base_url: str,
) -> None:
    with pytest.raises(ValueError) as caught:
        HTTPXHostedTransport(base_url)

    assert "opaque-value" not in str(caught.value)


def _json_response(
    status_code: int,
    value: dict[str, object],
    content_type: str = "application/json",
) -> TransportResponse:
    return TransportResponse(
        status_code,
        json.dumps(value, separators=(",", ":")).encode("utf-8"),
        content_type,
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _trust_entry(
    purpose: str, key: ec.EllipticCurvePrivateKey
) -> dict[str, object]:
    public = key.public_key().public_numbers()
    jwk: dict[str, str] = {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64url(public.x.to_bytes(32, "big")),
        "y": _b64url(public.y.to_bytes(32, "big")),
    }
    thumbprint = hashlib.sha256(_canonical(jwk)).digest()
    purpose_code = {
        "installation-binding": "ib",
        "membership": "ms",
        "license": "le",
    }[purpose]
    kid = f"bc1.{purpose_code}.{_b64url(thumbprint[:16])}"
    jwk["kid"] = kid
    return {
        "fixture_only": True,
        "jwk": jwk,
        "purpose": purpose,
        "thumbprint": _b64url(thumbprint),
    }


def _new_signing_key(
    purpose: str,
) -> tuple[ec.EllipticCurvePrivateKey, dict[str, object]]:
    key = ec.generate_private_key(ec.SECP256R1())
    return key, _trust_entry(purpose, key)


def _token(
    payload: dict[str, object],
    typ: str,
    jwk: object,
    key: ec.EllipticCurvePrivateKey,
) -> str:
    assert isinstance(jwk, dict)
    header = {"alg": "ES256", "kid": jwk["kid"], "typ": typ}
    signing_input = b".".join((_b64url(_canonical(header)).encode(), _b64url(_canonical(payload)).encode()))
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    s = min(s, _P256_ORDER - s)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input.decode()}.{_b64url(signature)}"


def _installation_key_reference() -> InstallationKeyReference:
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_numbers()
    members = {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64url(public.x.to_bytes(32, "big")),
        "y": _b64url(public.y.to_bytes(32, "big")),
    }
    thumbprint = hashlib.sha256(_canonical(members)).digest()
    key_id = f"ik1.{_b64url(thumbprint[:16])}"
    return InstallationKeyReference(
        provider_name="test-provider",
        provider_key_name="test-key",
        algorithm="ECDSA_P256",
        installation_key_id=key_id,
        installation_key_jkt=_b64url(thumbprint),
        public_key_jwk=_canonical({**members, "kid": key_id}).decode("ascii"),
        created_at=_INSTANT,
        activated_at=_INSTANT,
    )


def _membership_subject() -> str:
    return "principal:" + _b64url(
        _canonical({"issuer": _EXTERNAL_ISSUER, "subject": _EXTERNAL_SUBJECT})
    )


def _tamper_signature(token: str) -> str:
    header, payload, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    return f"{header}.{payload}.{replacement}{signature[1:]}"


def _payload(token: str) -> dict[str, Any]:
    encoded = token.split(".")[1]
    return json.loads(
        base64.urlsafe_b64decode(encoded + "=" * ((4 - len(encoded) % 4) % 4))
    )


def _proof_fields(canonical: bytes) -> tuple[bytes, ...]:
    domain = b"BRIDGE-CLEAN-INSTALLATION-PROOF-V1\x00"
    assert canonical.startswith(domain)
    offset = len(domain)
    fields = []
    while offset < len(canonical):
        length = struct.unpack_from("!I", canonical, offset)[0]
        offset += 4
        fields.append(canonical[offset : offset + length])
        offset += length
    assert offset == len(canonical)
    assert len(fields) == 9
    return tuple(fields)


def test_creator_association_uses_the_producer_provisioning_v2_proof_family(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim,
    device: DeviceMetadata,
) -> None:
    client, _, transport, authority = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device)
    association = _association(request_id="0198a1b2-c3d4-7800-8000-000000000011")
    client.request_creator_association(association)
    client.acquire_creator_account_binding(
        association, membership_reference_id=consumed.grant_reference_ids[1]
    )
    challenges = [body for path, body in transport.requests if path.endswith("/proof-challenges")]
    assert [body["profile"] for body in challenges] == [
        "urn:bridge-clean:provisioning-proof:v1",
        "urn:bridge-clean:provisioning-proof:v1",
    ]
    assert [body["account_id"] for body in challenges] == [_ACCOUNT_ID, _ACCOUNT_ID]
    fields = [_proof_fields(value) for value in authority.signed_values]
    assert fields[0][8] == b"urn:bridge-clean:commercial-control-plane:provisioning"
    for proof, purpose in zip(fields[1:], (
        b"creator-association-request", b"creator-association-status"
    ), strict=True):
        assert proof[4] == purpose
        assert proof[5] == _INSTALLATION_ID.encode()
        assert proof[6] == _ACCOUNT_ID.encode()
        assert proof[8] == b"urn:bridge-clean:commercial-control-plane:provisioning-v2"


@pytest.mark.parametrize("operation", ["request", "status"])
@pytest.mark.parametrize(("member", "value"), [
    ("profile", "urn:bridge-clean:installation-key-proof:v1"),
    ("audience", "urn:bridge-clean:commercial-control-plane:provisioning"),
    ("purpose", "onboarding-progress-report"),
    ("installation_id", "different-installation"),
])
def test_association_refuses_a_mismatched_challenge_before_signing(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim,
    device: DeviceMetadata, operation: str, member: str, value: str,
) -> None:
    client, _, transport, authority = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device)
    association = _association(request_id="0198a1b2-c3d4-7800-8000-000000000012")
    if operation == "status":
        client.request_creator_association(association)
    signed_before = len(authority.signed_values)
    request = transport.request

    def altered(method, path, *, json_body):
        response = request(method, path, json_body=json_body)
        if path.endswith("/proof-challenges"):
            body = json.loads(response.body)
            body[member] = value
            return _json_response(201, body)
        return response

    transport.request = altered
    with pytest.raises(HostedGrantUnavailable):
        if operation == "request":
            client.request_creator_association(association)
        else:
            client.acquire_creator_account_binding(
                association, membership_reference_id=consumed.grant_reference_ids[1]
            )
    assert len(authority.signed_values) == signed_before


class _CountingResponseStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


def _stream_transport(stream: _CountingResponseStream, *, encoding: str = "identity"):
    transport = HTTPXHostedTransport("https://control.example")
    transport._client.close()
    transport._client = httpx.Client(
        base_url="https://control.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"content-type": "application/json", "content-encoding": encoding},
            stream=stream,
        )),
    )
    return transport


@pytest.mark.parametrize("size", [0, 65_536])
def test_streamed_response_accepts_the_size_boundary(size: int):
    body = b" " * size
    stream = _CountingResponseStream([body])
    transport = _stream_transport(stream)
    try:
        response = transport.request("POST", "/v1/test", json_body={})
    finally:
        transport.close()
    assert len(response.body) == size
    assert stream.closed


def test_streamed_response_stops_reading_and_closes_at_the_size_limit():
    stream = _CountingResponseStream([b"x" * 8_192] * 100)
    transport = _stream_transport(stream)
    try:
        with pytest.raises(RuntimeError, match="^Hosted request failed$"):
            transport.request("POST", "/v1/test", json_body={})
    finally:
        transport.close()
    assert stream.read_count == 9
    assert stream.closed


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "gzip, identity"])
def test_streamed_response_refuses_compression_before_reading_or_decompressing(encoding: str):
    stream = _CountingResponseStream([gzip.compress(b"x" * 65_537)])
    transport = _stream_transport(stream, encoding=encoding)
    try:
        with pytest.raises(RuntimeError, match="^Hosted request failed$"):
            transport.request("POST", "/v1/test", json_body={})
    finally:
        transport.close()
    assert stream.closed
    assert stream.read_count == 0


def test_streamed_response_deadline_stops_small_slow_drip_chunks(monkeypatch):
    now = 0.0

    class SlowStream(_CountingResponseStream):
        def __iter__(self):
            nonlocal now
            for chunk in super().__iter__():
                now += 1.0
                yield chunk

    monkeypatch.setattr("app.security.hosted_grants.monotonic", lambda: now)
    stream = SlowStream([b"x"] * 100)
    transport = _stream_transport(stream)
    try:
        with pytest.raises(RuntimeError, match="^Hosted request failed$"):
            transport.request("POST", "/v1/test", json_body={})
    finally:
        transport.close()
    assert stream.read_count == 10
    assert stream.closed


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan"), True])
def test_concrete_transport_refuses_an_unbounded_or_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        HTTPXHostedTransport("https://control.example", timeout_seconds=timeout)


def test_transport_response_repr_does_not_expose_signed_objects():
    response = TransportResponse(403, b"sensitive-compact-denial", "application/json")
    assert "sensitive-compact-denial" not in repr(response)


@pytest.mark.parametrize("has_close", [True, False])
def test_hosted_client_closes_owned_transport_when_supported(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim, has_close: bool,
):
    client, _, transport, _ = _client(tmp_path, bundle, claim)
    closed = []
    if has_close:
        transport.close = lambda: closed.append(True)
    client.close()
    assert closed == ([True] if has_close else [])


def _signed_denial(bundle: SignedBundle, grant_type: str, *, changes=None) -> str:
    original = _payload(
        bundle.creator_bindings[_ACCOUNT_ID]
        if grant_type == "creator_account_binding" else bundle.tokens[grant_type]
    )
    purpose = {
        "installation_grant": "installation-binding",
        "creator_account_binding": "installation-binding",
        "membership_snapshot": "membership",
        "license_entitlement": "license",
    }[grant_type]
    now = int(_INSTANT.timestamp())
    claims = {
        "profile": "urn:bridge-clean:grant-denial:v1",
        "iss": "urn:bridge-clean:commercial-control-plane",
        "aud": original["aud"], "sub": original["sub"],
        "jti": "0198a1b2-c3d4-7900-8000-000000000001",
        "iat": now, "nbf": now, "exp": now + 600,
        "grant_type": grant_type, "revoked_jti": original["jti"],
        "effective_at": now,
        "reason_code": {
            "installation_grant": "revoked",
            "creator_account_binding": "approval_revoked",
            "membership_snapshot": "membership_removed",
            "license_entitlement": "entitlement_inactive",
        }[grant_type],
    }
    claims.update(changes or {})
    key = bundle.purpose_keys[purpose]
    return _token(claims, "urn:bridge-clean:grant-denial:v1", _trust_entry(purpose, key)["jwk"], key)


def _respond_with_denial(transport, token, *, status=403, extra=False, duplicate=False):
    transport.refresh_mode = "success"
    request = transport.request

    def respond(method, path, *, json_body):
        if path.startswith("/v1/grants/"):
            if duplicate:
                member = '"denial_jws":' + json.dumps(token)
                return TransportResponse(status, ("{" + member + "," + member + "}").encode(), "application/json")
            body = {"denial_jws": token}
            if extra:
                body["detail"] = "unexpected-field"
            return _json_response(status, body)
        return request(method, path, json_body=json_body)

    transport.request = respond


@pytest.mark.parametrize("grant_type", [
    "installation_grant", "creator_account_binding", "membership_snapshot", "license_entitlement",
])
def test_verified_denial_applies_durably_and_preserves_license_orthogonality(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim,
    identity: AuthContext, device: DeviceMetadata, grant_type: str,
):
    client, store, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    ids = consumed.grant_reference_ids
    if grant_type == "creator_account_binding":
        association = _association(request_id="0198a1b2-c3d4-7800-8000-000000000013")
        client.request_creator_association(association)
        binding = client.acquire_creator_account_binding(
            association, membership_reference_id=ids[1]
        )
        store.record_verified_grants((binding,))
        ids += (binding.reference_id,)
    current = next(grant for grant in store.verified_grants() if grant.grant_type == grant_type)
    token = _signed_denial(bundle, grant_type)
    _respond_with_denial(transport, token)

    result = client.refresh_grant(identity, ids, current.reference_id)

    assert result.state == "revoked"
    assert current.reference_id not in result.grant_reference_ids
    if token in repr(result):
        pytest.fail("Refresh diagnostics exposed a signed denial")
    if grant_type == "license_entitlement":
        assert result.policy is not None
        assert store.runtime_policy_is_current(result.policy)
        assert set(result.grant_reference_ids) == set(ids) - {current.reference_id}
    else:
        assert result.policy is None
    reopened = SQLiteAuthenticationStore(store.database.path, clock=lambda: _INSTANT)
    assert current.reference_id not in {grant.reference_id for grant in reopened.verified_grants()}
    assert reopened.verified_grant(current.reference_id).compact_jws is None


@pytest.mark.parametrize("failure", [
    "wrong_subject", "wrong_audience", "wrong_jti", "tampered", "expired", "future",
    "extra_envelope", "duplicate_envelope", "not_a_string", "not_403",
])
def test_unverified_denial_is_unknown_and_preserves_current_authority(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim,
    identity: AuthContext, device: DeviceMetadata, failure: str,
):
    client, store, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    now = int(_INSTANT.timestamp())
    changes = {
        "wrong_subject": {"sub": "principal:different"},
        "wrong_audience": {"aud": "urn:bridge-clean:local-brain:installation"},
        "wrong_jti": {"revoked_jti": "0198a1b2-c3d4-7100-8000-000000000099"},
        "expired": {"iat": now - 600, "nbf": now - 600, "exp": now},
        "future": {"iat": now + 61, "nbf": now + 61, "exp": now + 661},
    }.get(failure, {})
    token = _signed_denial(bundle, "membership_snapshot", changes=changes)
    if failure == "tampered":
        token = _tamper_signature(token)
    _respond_with_denial(
        transport, [] if failure == "not_a_string" else token,
        status=401 if failure == "not_403" else 403, extra=failure == "extra_envelope",
        duplicate=failure == "duplicate_envelope",
    )

    result = client.refresh_grant(identity, consumed.grant_reference_ids, consumed.grant_reference_ids[1])

    assert result.state == "unknown"
    assert result.policy == consumed.policy
    assert store.runtime_policy_is_current(result.policy)
    assert result.grant_reference_ids == consumed.grant_reference_ids
    if token in repr(result):
        pytest.fail("Refresh diagnostics exposed a signed denial")


def test_delayed_denial_does_not_withdraw_a_concurrent_replacement(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim,
    identity: AuthContext, device: DeviceMetadata,
):
    client, store, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    current_id = consumed.grant_reference_ids[1]
    transport.refresh_mode = "success"
    request = transport.request
    replacements = []

    def delayed(method, path, *, json_body):
        if path == "/v1/grants/membership:refresh":
            transport.request = request
            replacements.append(client.refresh_grant(identity, consumed.grant_reference_ids, current_id))
            return _json_response(403, {"denial_jws": _signed_denial(bundle, "membership_snapshot")})
        return request(method, path, json_body=json_body)

    transport.request = delayed
    result = client.refresh_grant(identity, consumed.grant_reference_ids, current_id)

    assert result.state == "unknown"
    assert result.policy is None
    assert len(replacements) == 1
    assert replacements[0].state == "updated"
    assert store.runtime_policy_is_current(replacements[0].policy)


def test_reference_refresh_does_not_require_runtime_access_for_empty_membership(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim, device: DeviceMetadata,
):
    payload = _payload(bundle.tokens["membership_snapshot"])
    payload["allowed_creator_account_ids"] = []
    key = bundle.purpose_keys["membership"]
    bundle.tokens["membership_snapshot"] = _token(
        payload, "urn:bridge-clean:grant:membership:v1", _trust_entry("membership", key)["jwk"], key
    )
    client, store, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device)
    current_id = consumed.grant_reference_ids[1]
    assert store.verified_grant(current_id).allowed_creator_account_ids == ()
    transport.refresh_mode = "success"

    def no_runtime_authority(*args, **kwargs):
        pytest.fail("Proof-only grant refresh requested runtime authority")

    client.runtime_policy = no_runtime_authority
    result = client.refresh_reference(current_id)

    assert result.state == "updated"
    assert result.policy is None
    assert len(result.grant_reference_ids) == 1
    assert store.verified_grant(result.grant_reference_ids[0]).allowed_creator_account_ids == (
        _ACCOUNT_ID, _SECOND_ACCOUNT_ID,
    )


def _respond_with_replacement(transport, bundle: SignedBundle, grant_type: str, *, issued_at=None, changes=None):
    original = _payload(
        bundle.creator_bindings[_ACCOUNT_ID]
        if grant_type == "creator_account_binding" else bundle.tokens[grant_type]
    )
    purpose, typ = {
        "installation_grant": ("installation-binding", "urn:bridge-clean:grant:installation:v1"),
        "membership_snapshot": ("membership", "urn:bridge-clean:grant:membership:v1"),
        "creator_account_binding": ("installation-binding", "urn:bridge-clean:grant:creator-binding:v1"),
        "license_entitlement": ("license", "urn:bridge-clean:grant:license:v1"),
    }[grant_type]
    payload = {**original, "jti": "0198a1b2-c3d4-7100-8000-000000000098"}
    if issued_at is not None:
        payload.update(iat=issued_at, nbf=issued_at, exp=issued_at + original["exp"] - original["iat"])
    payload.update(changes or {})
    if grant_type == "creator_account_binding":
        payload["sub"] = f"installation:{_INSTALLATION_ID}:creator:{payload['creator_account_id']}"
    key = bundle.purpose_keys[purpose]
    token = _token(payload, typ, _trust_entry(purpose, key)["jwk"], key)
    transport.refresh_mode = "success"
    request = transport.request

    def respond(method, path, *, json_body):
        if path.startswith("/v1/grants/"):
            return _json_response(200, {
                "profile": "urn:bridge-clean:grant-refresh:v1",
                "request_id": "0198a1b2-c3d4-7500-8000-000000000098",
                "grant_type": grant_type, "previous_jti": original["jti"],
                "grant": token, "server_time": "2026-07-18T00:01:00.000Z",
                "refresh_after": "2026-07-18T06:01:00.000Z",
            })
        return request(method, path, json_body=json_body)

    transport.request = respond


@pytest.mark.parametrize(("grant_type", "changes"), [
    ("membership_snapshot", {"membership_id": "different-membership"}),
    ("creator_account_binding", {"creator_account_id": _SECOND_ACCOUNT_ID}),
    ("creator_account_binding", {"approval_id": "0198a1b2-c3d4-7600-8000-000000000099"}),
    ("creator_account_binding", {"approval_revision": 2}),
    ("license_entitlement", {"entitlement_id": "different-entitlement"}),
    ("license_entitlement", {"product_id": "different-product"}),
    ("installation_grant", {"jti": "0198a1b2-c3d4-7100-8000-000000000001"}),
])
def test_signed_replacement_cannot_change_the_requested_immutable_grant_tuple(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim,
    device: DeviceMetadata, grant_type: str, changes: dict[str, object],
):
    client, store, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device)
    if grant_type == "creator_account_binding":
        association = _association(request_id="0198a1b2-c3d4-7800-8000-000000000014")
        client.request_creator_association(association)
        binding = client.acquire_creator_account_binding(
            association, membership_reference_id=consumed.grant_reference_ids[1]
        )
        store.record_verified_grants((binding,))
    before = store.verified_grants()
    current = next(grant for grant in before if grant.grant_type == grant_type)
    _respond_with_replacement(transport, bundle, grant_type, changes=changes)

    result = client.refresh_reference(current.reference_id)

    assert result.state == "unknown"
    assert result.policy is None
    assert store.verified_grants() == before
    if current.compact_jws is not None:
        _check_retained_secret(store.verified_grant(current.reference_id).compact_jws, current.compact_jws)


@pytest.mark.parametrize("after_signed_expiry", [1, 259_199, 259_200])
def test_reference_refresh_uses_the_exclusive_offline_grace_boundary(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim,
    device: DeviceMetadata, after_signed_expiry: int,
):
    from app.persistence.auth import AuthenticationStateError

    client, store, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device)
    current_id = consumed.grant_reference_ids[1]
    signed_expiry = _payload(bundle.tokens["membership_snapshot"])["exp"]
    now = datetime.fromtimestamp(signed_expiry + after_signed_expiry, timezone.utc)
    client._clock = lambda: now
    store._clock = lambda: now
    _respond_with_replacement(transport, bundle, "membership_snapshot", issued_at=int(now.timestamp()))
    requests_before = len(transport.requests)

    if after_signed_expiry == 259_200:
        with pytest.raises(AuthenticationStateError):
            client.refresh_reference(current_id)
        assert len(transport.requests) == requests_before
    else:
        result = client.refresh_reference(current_id)
        assert result.state == "updated"
        assert result.policy is None
        assert store.verified_grant(result.grant_reference_ids[0]).valid_from == now


@pytest.mark.parametrize("grant_type", ["installation_grant", "membership_snapshot"])
@pytest.mark.parametrize("response", ["replacement", "denial"])
def test_commit_guard_cancellation_prevents_verified_refresh_or_denial_writes(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim,
    device: DeviceMetadata, grant_type: str, response: str,
):
    client, store, transport, _ = _client(tmp_path, bundle, claim)
    client.consume_claim(claim, device)
    before = store.verified_grants()
    current = next(grant for grant in before if grant.grant_type == grant_type)
    if response == "denial":
        _respond_with_denial(transport, _signed_denial(bundle, grant_type))
    else:
        _respond_with_replacement(transport, bundle, grant_type)
    guard_entries = []

    @contextmanager
    def cancelled():
        guard_entries.append(True)
        yield False

    result = client.refresh_reference(current.reference_id, commit_guard=cancelled)

    assert result.state == "unknown"
    assert result.policy is None
    assert guard_entries == [True]
    assert store.verified_grants() == before
    if current.compact_jws is not None:
        _check_retained_secret(store.verified_grant(current.reference_id).compact_jws, current.compact_jws)
    with store.database.read() as connection:
        count = connection.execute("SELECT COUNT(*) FROM hosted_grant_tombstones").fetchone()[0]
    assert count == 0


@pytest.mark.parametrize("response", ["replacement", "denial"])
def test_commit_guard_remains_entered_during_authoritative_store_mutation(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim,
    device: DeviceMetadata, response: str, monkeypatch,
):
    client, store, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device)
    current_id = consumed.grant_reference_ids[0]
    if response == "denial":
        _respond_with_denial(transport, _signed_denial(bundle, "installation_grant"))
        method = "apply_hosted_grant_denial"
    else:
        _respond_with_replacement(transport, bundle, "installation_grant")
        method = "replace_verified_grant"
    active = False
    committed = []
    original = getattr(store, method)

    @contextmanager
    def admitted():
        nonlocal active
        active = True
        try:
            yield True
        finally:
            active = False

    def guarded_store(*args, **kwargs):
        assert active
        committed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, method, guarded_store)
    result = client.refresh_reference(current_id, commit_guard=admitted)

    assert result.state == ("revoked" if response == "denial" else "updated")
    assert committed == [True]
    assert not active


def test_transient_refresh_failure_rebuilds_policy_after_concurrent_revocation(
    tmp_path: Path, bundle: SignedBundle, claim: InstallationClaim,
    identity: AuthContext, device: DeviceMetadata,
):
    from app.persistence.auth import RevocationKey, RevocationScopeType

    client, store, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    current_id = consumed.grant_reference_ids[1]

    def unavailable(method, path, *, json_body):
        store.revoke(RevocationKey(RevocationScopeType.VERIFIED_GRANT, current_id))
        raise TimeoutError("Hosted request timed out")

    transport.request = unavailable
    result = client.refresh_grant(identity, consumed.grant_reference_ids, current_id)

    assert result.state == "unknown"
    assert result.policy is None
    assert not store.runtime_policy_is_current(consumed.policy)


def _check_retained_secret(actual: str | None, expected: str) -> None:
    import secrets

    if actual is None or not secrets.compare_digest(actual, expected):
        pytest.fail("retained verified token mismatch", pytrace=False)


def _check_secret_not_rendered(secret: str, rendered: str) -> None:
    if secret in rendered:
        pytest.fail("verified token exposed", pytrace=False)


def test_acquisition_retains_only_exact_pairing_grants(
    tmp_path, bundle, claim, identity, device, caplog
):
    client, store, _, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    for reference_id in consumed.grant_reference_ids:
        reference = store.verified_grant(reference_id)
        if reference.grant_type == "installation_grant":
            _check_retained_secret(
                reference.compact_jws, bundle.tokens[reference.grant_type]
            )
        else:
            assert reference.compact_jws is None
    association = _association(request_id="0198a1b2-c3d4-7800-8000-000000000001")
    client.request_creator_association(association)
    binding = client.acquire_creator_account_binding(
        association, membership_reference_id=consumed.grant_reference_ids[1]
    )
    store.record_verified_grant(binding)
    _check_retained_secret(
        store.verified_grant(binding.reference_id).compact_jws,
        bundle.creator_bindings[_ACCOUNT_ID],
    )
    for token in (*bundle.tokens.values(), *bundle.creator_bindings.values()):
        _check_secret_not_rendered(token, caplog.text)
        _check_secret_not_rendered(token, repr(store.verified_grants()))
        _check_secret_not_rendered(token, repr(bundle))


@pytest.mark.parametrize(
    "tampered", [False, True], ids=["verified", "invalid-signature"]
)
def test_pairing_grant_refresh_retains_verified_replacement_only(
    tmp_path, bundle, claim, identity, device, monkeypatch, caplog, tampered
):
    # A second independently generated authorized signer supplies the replacement.
    signer = ec.generate_private_key(ec.SECP256R1())
    entry = _trust_entry("installation-binding", signer)
    bundle.trust_set["keys"].append(entry)
    claims = _payload(bundle.tokens["installation_grant"])
    previous_jti = claims["jti"]
    claims["jti"] = "0198a1b2-c3d4-7100-8000-000000000099"
    replacement = _token(
        claims, "urn:bridge-clean:grant:installation:v1", entry["jwk"], signer
    )
    if tampered:
        replacement = _tamper_signature(replacement)
    client, store, transport, _ = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    previous_id = consumed.grant_reference_ids[0]
    transport.refresh_mode = "success"
    original_request = transport.request

    def request(method, path, *, json_body):
        # Refresh sends metadata/proof, never the retained grant as a credential.
        _check_secret_not_rendered(
            bundle.tokens["installation_grant"], json.dumps(json_body)
        )
        if path == "/v1/grants/installation:refresh":
            return _json_response(
                200,
                {
                    "profile": REFRESH_PROFILE,
                    "request_id": "0198a1b2-c3d4-7500-8000-000000000001",
                    "grant_type": "installation_grant",
                    "previous_jti": previous_jti,
                    "grant": replacement,
                    "server_time": "2026-07-18T00:01:00.000Z",
                    "refresh_after": "2026-07-18T06:01:00.000Z",
                },
            )
        return original_request(method, path, json_body=json_body)

    monkeypatch.setattr(transport, "request", request)
    refreshed = client.refresh_grant(
        identity, consumed.grant_reference_ids, previous_id
    )
    if tampered:
        assert refreshed.state == "unknown"
        _check_retained_secret(
            store.verified_grant(previous_id).compact_jws,
            bundle.tokens["installation_grant"],
        )
    else:
        assert refreshed.state == "updated"
        assert store.verified_grant(previous_id).compact_jws is None
        _check_retained_secret(
            store.verified_grant(refreshed.grant_reference_ids[0]).compact_jws,
            replacement,
        )
    for secret in (replacement, bundle.tokens["installation_grant"]):
        _check_secret_not_rendered(secret, caplog.text)
        _check_secret_not_rendered(secret, repr(store.verified_grants()))
