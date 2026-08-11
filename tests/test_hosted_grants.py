from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.persistence.auth import InstallationKeyReference, SQLiteAuthenticationStore
from app.security.hosted_grants import (
    CLAIM_PROFILE,
    PROOF_AUDIENCE,
    PROOF_PROFILE,
    REFRESH_PROFILE,
    ClaimConsumption,
    DeviceMetadata,
    GrantVerificationRefused,
    HostedGrantClient,
    HostedGrantUnavailable,
    InstallationClaim,
    InstallationClaimReplay,
    TransportResponse,
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
_EXTERNAL_ISSUER = "https://identity.test.invalid/fixture-tenant"
_EXTERNAL_SUBJECT = "fixture-customer-subject-001"


@dataclass(frozen=True)
class SignedBundle:
    tokens: dict[str, str]
    replacement_membership: str
    trust_set: dict[str, object]
    installation_key: InstallationKeyReference


class FakeProofAuthority:
    def __init__(self, reference: InstallationKeyReference) -> None:
        self.reference = reference
        self.signed_values: list[bytes] = []

    def ensure_ready(self) -> InstallationKeyReference:
        return self.reference

    def sign_challenge(self, challenge: bytes) -> InstallationProof:
        self.signed_values.append(challenge)
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
            if self.refresh_mode == "timeout":
                raise TimeoutError(f"request failed: {self.claim.claim_secret}")
            if self.refresh_mode == "unreachable":
                raise OSError(f"host unavailable: {self.claim.claim_secret}")
            if self.refresh_mode == "server_error":
                return _json_response(503, {"detail": self.claim.claim_secret})
            if self.refresh_mode == "malformed":
                return TransportResponse(201, b"{")
            if self.refresh_mode == "unsigned_denial":
                return _json_response(403, {"denial_jws": "not-a-signed-denial"})
            purpose = json_body["purpose"]
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
            )
        raise AssertionError(f"unexpected request path: {path}")


@pytest.fixture
def bundle() -> SignedBundle:
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
        "iat": 1_784_332_800,
        "nbf": 1_784_332_800,
    }
    membership_claims = {
        **common,
        "aud": "urn:bridge-clean:local-brain:membership",
        "exp": 1_784_419_200,
        "grant_type": "membership_snapshot",
        "jti": "0198a1b2-c3d4-7100-8000-000000000003",
        "membership_id": "0198a1b2-c3d4-7200-8000-000000000002",
        "ciam_issuer": _EXTERNAL_ISSUER,
        "ciam_subject": _EXTERNAL_SUBJECT,
        "roles": ["creator_operator"],
        "scopes": ["runtime:existing-data", "runtime:licensed-processing"],
        "allowed_creator_account_ids": [_ACCOUNT_ID],
        "sub": _membership_subject(),
    }
    tokens = {
        "installation_grant": _token(
            {
                **common,
                "aud": "urn:bridge-clean:local-brain:installation",
                "exp": 1_786_924_800,
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
                "exp": 1_784_419_200,
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
    return SignedBundle(tokens, replacement_membership, trust_set, installation_key)


@pytest.fixture
def claim() -> InstallationClaim:
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
    ["unreachable", "timeout", "server_error", "malformed", "unsigned_denial"],
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
    assert row["revoked_at"] is None
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


def test_verified_refresh_replaces_the_reference_and_policy(
    tmp_path: Path,
    bundle: SignedBundle,
    claim: InstallationClaim,
    identity: AuthContext,
    device: DeviceMetadata,
) -> None:
    client, store, transport, authority = _client(tmp_path, bundle, claim)
    consumed = client.consume_claim(claim, device, identity=identity)
    assert consumed.policy is not None
    previous_reference = consumed.grant_reference_ids[1]
    transport.refresh_mode = "success"

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


def _json_response(status_code: int, value: dict[str, object]) -> TransportResponse:
    return TransportResponse(
        status_code,
        json.dumps(value, separators=(",", ":")).encode("utf-8"),
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
