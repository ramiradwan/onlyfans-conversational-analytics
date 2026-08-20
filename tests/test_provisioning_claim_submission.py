"""One pasted claim package consumed into verified local grants."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    Prehashed,
    decode_dss_signature,
)
from fastapi.testclient import TestClient

from app import packaged_entry
from app.core.config import Settings
from app.core.runtime_paths import runtime_data_directory
from app.persistence.auth import (
    ClaimSubmission,
    ClaimSubmissionState,
    InstallationKeyReference,
    SQLiteAuthenticationStore,
)
from app.provisioning import claim_submission as submission_module
from app.provisioning.app import PROVISIONING_CLAIM_PATH
from app.provisioning.claim_package import CLAIM_PACKAGE_PROFILE, decode_claim_package
from app.provisioning.claim_submission import PRODUCT_VERSION, durable_claim_submission
from app.provisioning.session import (
    PROVISIONING_CSRF_HEADER,
    PROVISIONING_ORIGIN,
    PROVISIONING_SESSION_COOKIE_NAME,
)
from app.security import hosted_grants as hosted_grants_module
from app.security.hosted_grants import CLAIM_PROFILE, TransportResponse
from app.security.installation_key import (
    INSTALLATION_KEY_ALGORITHM,
    PLATFORM_CRYPTO_PROVIDER,
    InstallationKeyAuthority,
    InstallationKeyUnavailable,
    InstallationProof,
    ProviderKeyInfo,
)
from contracts.loader import ContractsIntegrityError


_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
)
ORGANIZATION_ID = "0198b2c3-d4e5-7000-8000-000000000001"
INSTALLATION_ID = "0198b2c3-d4e5-7000-8000-000000000002"
ONBOARDING_TRANSACTION_ID = "0198b2c3-d4e5-7400-8000-000000000001"
CLAIM_ID = "0198b2c3-d4e5-7300-8000-000000000001"
CONSUME_PATH = f"/v1/installation-claims/{CLAIM_ID}:consume"
EXTERNAL_ISSUER = "https://identity.test.invalid/fixture-tenant"
EXTERNAL_SUBJECT = "fixture-customer-subject-001"
ACCOUNT_ID = "creator-account-fixture-001"
DEVICE_NAME = "Provisioning workstation"
HOSTED_ORIGIN = "https://hosted.test.invalid"
HANDOFF_TOKEN = "t" * 32

# Stated independently of the modules under test so that moving either the
# store or the signed time contract cannot silently move the cases pinned to it.
DATABASE_FILENAME = "auth.sqlite3"
GRANT_LIFETIMES = {
    "installation_grant": 2_592_000,
    "membership_snapshot": 86_400,
    "license_entitlement": 86_400,
}
LOCAL_PLATFORM = {"win32": "windows", "darwin": "macos", "linux": "linux"}[sys.platform]

# The durable claim record and the columns it keeps for local bookkeeping.
# Every other column of that table carries claim material, so the set is stated
# here rather than derived from the schema under test.
CLAIM_RECORD_TABLE = "provisioning_claim_submissions"
CLAIM_RECORD_LOCAL_COLUMNS = {"state", "outcome", "submitted_at", "resolved_at"}
CLAIM_RECORD_COORDINATES = {
    "claim_id",
    "onboarding_transaction_id",
    "organization_id",
    "installation_id",
}

# Every refusal reason this action is allowed to report. A reason outside the
# set, or one carrying claim material, would reach the provisioning page.
NONSECRET_REASONS = {
    "claim_already_consumed",
    "claim_refused",
    "consumed",
    "device",
    "encoding",
    "grant_verification_refused",
    "hosted_origin_unavailable",
    "hosted_unavailable",
    "installation_key_unavailable",
    "profile",
    "schema",
    "size",
}


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


CLAIM_SECRET = _b64url(b"s" * 32)
CHALLENGE = _b64url(b"c" * 32)


def claim_document(**overrides: str) -> dict[str, str]:
    """Build the pasted package's decoded document."""

    document = {
        "profile": CLAIM_PACKAGE_PROFILE,
        "claim_id": CLAIM_ID,
        "claim_secret": CLAIM_SECRET,
        "challenge": CHALLENGE,
        "onboarding_transaction_id": ONBOARDING_TRANSACTION_ID,
        "organization_id": ORGANIZATION_ID,
        "installation_id": INSTALLATION_ID,
        "consume_path": CONSUME_PATH,
    }
    document.update(overrides)
    return document


def package(document: dict[str, str]) -> str:
    return _b64url(json.dumps(document, separators=(",", ":")).encode("utf-8"))


PACKAGE = package(claim_document())


@dataclass(frozen=True)
class SignedGrants:
    """One hosted claim result: the grants it returns and the trust to check them."""

    tokens: dict[str, str]
    trust_set: dict[str, object]
    installation_key: InstallationKeyReference


class FakeProofAuthority:
    """Possession proofs without a platform key provider."""

    def __init__(
        self, reference: InstallationKeyReference, *, unavailable: bool = False
    ) -> None:
        self.reference = reference
        self.unavailable = unavailable
        self.signed_values: list[bytes] = []

    def ensure_ready(self) -> InstallationKeyReference:
        if self.unavailable:
            raise InstallationKeyUnavailable("emulated installation key refusal")
        return self.reference

    def sign_challenge(self, challenge: bytes) -> InstallationProof:
        self.signed_values.append(challenge)
        return InstallationProof(
            self.reference.installation_key_id, "ES256", b"\x01" * 64
        )


class HostedClaimTransport:
    """The hosted plane at the outbound HTTPS seam."""

    def __init__(
        self,
        grants: SignedGrants,
        *,
        status: int | None = None,
        tokens: dict[str, str] | None = None,
    ) -> None:
        self.grants = grants
        self.status = status
        self.tokens = grants.tokens if tokens is None else tokens
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.origin = ""
        self.consumed = False

    def request(
        self, method: str, path: str, *, json_body: dict[str, object]
    ) -> TransportResponse:
        self.requests.append((method, path, dict(json_body)))
        if self.status is not None:
            return _json_response(self.status, {"detail": "hosted refusal"})
        if self.consumed:
            return _json_response(409, {"detail": "hosted claim already consumed"})
        self.consumed = True
        return _json_response(
            200,
            {
                "profile": CLAIM_PROFILE,
                "status": "consumed",
                "claim_id": CLAIM_ID,
                "onboarding_transaction_id": ONBOARDING_TRANSACTION_ID,
                "organization_id": ORGANIZATION_ID,
                "installation_id": INSTALLATION_ID,
                "installation_key_id": (
                    self.grants.installation_key.installation_key_id
                ),
                "installation_key_jkt": (
                    self.grants.installation_key.installation_key_jkt
                ),
                "consumed_at": "2026-08-14T00:01:00.000Z",
                "grants": self.tokens,
                "bootstrap_config_version": "1.0.0",
            },
        )


class RecordObservingTransport(HostedClaimTransport):
    """Hosted transport that reads durable claim state as the call is made."""

    def __init__(
        self, grants: SignedGrants, store: SQLiteAuthenticationStore
    ) -> None:
        super().__init__(grants)
        self.store = store
        self.unresolved_at_request: tuple[ClaimSubmission, ...] = ()

    def request(
        self, method: str, path: str, *, json_body: dict[str, object]
    ) -> TransportResponse:
        self.unresolved_at_request = self.store.unresolved_claim_submissions()
        return super().request(method, path, json_body=json_body)


class GrantStorageFailure(RuntimeError):
    """A local write that fails after the hosted plane consumed the claim."""


class BrokenGrantStore:
    """The authentication store with only its verified-grant write broken."""

    def __init__(self, store: SQLiteAuthenticationStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def record_verified_grants(self, grants: Any) -> None:
        raise GrantStorageFailure("emulated durable grant write failure")


class EmulatedPlatformProvider:
    """Platform key provider whose key material stays in memory."""

    provider_name = PLATFORM_CRYPTO_PROVIDER

    def __init__(self) -> None:
        self._keys: dict[str, ec.EllipticCurvePrivateKey] = {}

    def key_info(self, provider_key_name: str) -> ProviderKeyInfo | None:
        key = self._keys.get(provider_key_name)
        if key is None:
            return None
        numbers = key.public_key().public_numbers()
        return ProviderKeyInfo(
            provider_name=self.provider_name,
            provider_key_name=provider_key_name,
            algorithm=INSTALLATION_KEY_ALGORITHM,
            export_policy=0,
            hardware_backed=True,
            x=numbers.x.to_bytes(32, "big"),
            y=numbers.y.to_bytes(32, "big"),
        )

    def create_non_exportable_key(self, provider_key_name: str) -> ProviderKeyInfo:
        self._keys[provider_key_name] = ec.generate_private_key(ec.SECP256R1())
        info = self.key_info(provider_key_name)
        assert info is not None
        return info

    def sign_digest(self, provider_key_name: str, digest: bytes) -> bytes:
        der = self._keys[provider_key_name].sign(
            digest, ec.ECDSA(Prehashed(hashes.SHA256()))
        )
        r, s = decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


@pytest.fixture
def installation_key() -> InstallationKeyReference:
    return _installation_key_reference()


@pytest.fixture
def grants(installation_key: InstallationKeyReference) -> SignedGrants:
    return _signed_grants(installation_key)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(tmp_path / DATABASE_FILENAME)


@pytest.fixture
def data_directory(tmp_path: Path) -> Path:
    return runtime_data_directory(tmp_path / "runtime-data")


def submission(
    store: SQLiteAuthenticationStore,
    grants: SignedGrants,
    transport: HostedClaimTransport,
    *,
    proof_authority: Any = None,
) -> Callable[..., str | None]:
    """Build the action over a real decoder, client, and store."""

    authority = proof_authority or FakeProofAuthority(grants.installation_key)
    return durable_claim_submission(
        lambda: store,
        hosted_origin=HOSTED_ORIGIN,
        transport_factory=_transport_factory(transport),
        proof_authority_factory=lambda _: authority,
        device_display_name=lambda: DEVICE_NAME,
        trust_set=grants.trust_set,
    )


def test_a_valid_package_stores_every_grant_the_hosted_plane_returned(
    store: SQLiteAuthenticationStore, grants: SignedGrants
) -> None:
    transport = HostedClaimTransport(grants)

    refusal = submission(store, grants, transport)(package=PACKAGE)

    assert refusal is None
    assert [record.grant_type for record in store.verified_grants()] == [
        "installation_grant",
        "membership_snapshot",
        "license_entitlement",
    ]
    assert [(method, path) for method, path, _ in transport.requests] == [
        ("POST", CONSUME_PATH)
    ]


def test_the_hosted_request_carries_the_pasted_claim_and_local_device_metadata(
    store: SQLiteAuthenticationStore, grants: SignedGrants
) -> None:
    transport = HostedClaimTransport(grants)

    assert submission(store, grants, transport)(package=PACKAGE) is None

    request = transport.requests[0][2]["request"]
    assert request["claim_secret"] == CLAIM_SECRET
    assert request["onboarding_transaction_id"] == ONBOARDING_TRANSACTION_ID
    assert request["organization_id"] == ORGANIZATION_ID
    assert request["installation_id"] == INSTALLATION_ID
    assert request["device"] == {
        "platform": LOCAL_PLATFORM,
        "product_version": PRODUCT_VERSION,
        "display_name": DEVICE_NAME,
    }
    assert transport.origin == HOSTED_ORIGIN


def test_device_metadata_is_local_even_when_the_package_carries_its_own(
    store: SQLiteAuthenticationStore, grants: SignedGrants
) -> None:
    """A package naming a device is refused rather than believed."""

    transport = HostedClaimTransport(grants)
    document = claim_document()
    document["display_name"] = "attacker-supplied device"

    refusal = submission(store, grants, transport)(package=package(document))

    assert refusal == "schema"
    assert transport.requests == []


def test_the_reported_product_version_is_the_application_version() -> None:
    assert PRODUCT_VERSION == Settings.model_fields["version"].default


def test_a_second_submission_of_a_consumed_package_is_refused_as_consumed(
    store: SQLiteAuthenticationStore, grants: SignedGrants
) -> None:
    transport = HostedClaimTransport(grants)
    submit = submission(store, grants, transport)

    assert submit(package=PACKAGE) is None
    assert submit(package=PACKAGE) == "claim_already_consumed"
    assert len(store.verified_grants()) == 3


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (409, "claim_already_consumed"),
        (403, "claim_refused"),
        (400, "claim_refused"),
        (425, "hosted_unavailable"),
        (429, "hosted_unavailable"),
        (503, "hosted_unavailable"),
    ],
)
def test_each_hosted_outcome_reports_its_own_nonsecret_reason(
    store: SQLiteAuthenticationStore,
    grants: SignedGrants,
    status: int,
    reason: str,
) -> None:
    transport = HostedClaimTransport(grants, status=status)

    refusal = submission(store, grants, transport)(package=PACKAGE)

    assert refusal == reason
    assert refusal in NONSECRET_REASONS
    assert CLAIM_SECRET not in refusal
    assert store.verified_grants() == ()


def test_a_tampered_grant_is_refused_and_nothing_is_stored(
    store: SQLiteAuthenticationStore, grants: SignedGrants
) -> None:
    tokens = dict(grants.tokens)
    tokens["license_entitlement"] = _tampered(tokens["license_entitlement"])
    transport = HostedClaimTransport(grants, tokens=tokens)

    refusal = submission(store, grants, transport)(package=PACKAGE)

    assert refusal == "grant_verification_refused"
    assert store.verified_grants() == ()


@pytest.mark.parametrize(
    ("pasted", "reason"),
    [
        ("not a package", "encoding"),
        ("cGFzdGVk=", "encoding"),
        (package({**claim_document(), "profile": "urn:other:v1"}), "profile"),
        (package({"profile": CLAIM_PACKAGE_PROFILE}), "schema"),
        (package({**claim_document(), "claim_secret": "short"}), "schema"),
        (package({**claim_document(), "consume_path": "/v1/elsewhere"}), "schema"),
        ("A" * 1401, "size"),
    ],
)
def test_a_malformed_package_is_refused_before_any_hosted_request(
    store: SQLiteAuthenticationStore,
    grants: SignedGrants,
    pasted: str,
    reason: str,
) -> None:
    transport = HostedClaimTransport(grants)

    refusal = submission(store, grants, transport)(package=pasted)

    assert refusal == reason
    assert refusal in NONSECRET_REASONS
    assert transport.requests == []
    assert store.verified_grants() == ()


def test_an_unusable_hosted_origin_is_refused_rather_than_raised(
    store: SQLiteAuthenticationStore, grants: SignedGrants
) -> None:
    """The origin reaches the real transport, which parses it."""

    submit = durable_claim_submission(
        lambda: store,
        hosted_origin="",
        proof_authority_factory=lambda _: FakeProofAuthority(
            grants.installation_key
        ),
        device_display_name=lambda: DEVICE_NAME,
        trust_set=grants.trust_set,
    )

    assert submit(package=PACKAGE) == "hosted_origin_unavailable"
    assert store.verified_grants() == ()


@pytest.mark.parametrize("stage", ["construction", "consumption"])
def test_an_unusable_installation_key_is_refused_rather_than_raised(
    store: SQLiteAuthenticationStore, grants: SignedGrants, stage: str
) -> None:
    transport = HostedClaimTransport(grants)

    def refusing_factory(_: Any) -> Any:
        raise InstallationKeyUnavailable("emulated provider refusal")

    submit = (
        durable_claim_submission(
            lambda: store,
            hosted_origin=HOSTED_ORIGIN,
            transport_factory=_transport_factory(transport),
            proof_authority_factory=refusing_factory,
            device_display_name=lambda: DEVICE_NAME,
            trust_set=grants.trust_set,
        )
        if stage == "construction"
        else submission(
            store,
            grants,
            transport,
            proof_authority=FakeProofAuthority(
                grants.installation_key, unavailable=True
            ),
        )
    )

    assert submit(package=PACKAGE) == "installation_key_unavailable"
    assert transport.requests == []
    assert store.verified_grants() == ()


def test_the_decoded_claim_is_dropped_when_consumption_never_reaches_the_plane(
    store: SQLiteAuthenticationStore, grants: SignedGrants, monkeypatch
) -> None:
    """A refusal before release leaves no reusable claim behind."""

    decoded: list[Any] = []
    real_decoder = submission_module.decode_claim_package

    def recording_decoder(pasted: str) -> Any:
        package_object = real_decoder(pasted)
        decoded.append(package_object)
        return package_object

    monkeypatch.setattr(submission_module, "decode_claim_package", recording_decoder)
    submit = durable_claim_submission(
        lambda: store,
        hosted_origin="",
        proof_authority_factory=lambda _: FakeProofAuthority(
            grants.installation_key
        ),
        device_display_name=lambda: DEVICE_NAME,
        trust_set=grants.trust_set,
    )

    assert submit(package=PACKAGE) == "hosted_origin_unavailable"
    assert [package_object.released for package_object in decoded] == [True]


def test_a_packaged_trust_set_defect_is_not_reported_as_a_customer_refusal(
    store: SQLiteAuthenticationStore, grants: SignedGrants, monkeypatch
) -> None:
    """An unusable installed artifact is a defect, not a provisioning state."""

    def refuse_trust_set(_: str) -> dict[str, object]:
        raise ContractsIntegrityError("trust set digest mismatch")

    monkeypatch.setattr(
        hosted_grants_module, "load_pinned_trust_set", refuse_trust_set
    )
    submit = durable_claim_submission(
        lambda: store,
        hosted_origin=HOSTED_ORIGIN,
        transport_factory=_transport_factory(HostedClaimTransport(grants)),
        proof_authority_factory=lambda _: FakeProofAuthority(
            grants.installation_key
        ),
        device_display_name=lambda: DEVICE_NAME,
    )

    with pytest.raises(ContractsIntegrityError):
        submit(package=PACKAGE)


def test_the_composed_surface_refuses_a_malformed_package_without_reaching_out(
    data_directory: Path, monkeypatch
) -> None:
    """The surface packaged boot builds decodes before it opens a transport."""

    def refuse_transport(hosted_origin: str) -> Any:
        raise AssertionError("hosted transport was opened for a malformed package")

    monkeypatch.setattr(submission_module, "HTTPXHostedTransport", refuse_transport)
    client, headers = _bounded_composed_session(data_directory, monkeypatch)

    response = client.post(
        PROVISIONING_CLAIM_PATH, json={"package": "not a package"}, headers=headers
    )

    assert response.status_code == 409
    assert response.json() == {"state": "provisioning_ready", "reason": "encoding"}


def test_the_composed_surface_verifies_grants_against_the_pinned_trust_set(
    data_directory: Path, monkeypatch
) -> None:
    """Composition supplies no trust set of its own, so the pinned one applies."""

    provider = EmulatedPlatformProvider()
    grants = _signed_grants(_composed_installation_key(data_directory, provider))
    transport = HostedClaimTransport(grants)
    _emulate_platform(monkeypatch, provider, transport)
    client, headers = _bounded_composed_session(data_directory, monkeypatch)

    response = client.post(
        PROVISIONING_CLAIM_PATH, json={"package": PACKAGE}, headers=headers
    )

    assert response.status_code == 409
    assert response.json() == {
        "state": "provisioning_ready",
        "reason": "grant_verification_refused",
    }
    assert [(method, path) for method, path, _ in transport.requests] == [
        ("POST", CONSUME_PATH)
    ]
    assert transport.origin == HOSTED_ORIGIN
    assert transport.requests[0][2]["request"]["claim_secret"] == CLAIM_SECRET
    assert _composed_store(data_directory).verified_grants() == ()


def test_the_composed_surface_registers_the_installation_from_one_package(
    data_directory: Path, monkeypatch
) -> None:
    """The route reaches durable grant storage through the composed application."""

    provider = EmulatedPlatformProvider()
    installation_key = _composed_installation_key(data_directory, provider)
    grants = _signed_grants(installation_key)
    transport = HostedClaimTransport(grants)
    _emulate_platform(monkeypatch, provider, transport)
    monkeypatch.setattr(
        hosted_grants_module, "load_pinned_trust_set", lambda _: grants.trust_set
    )
    client, headers = _bounded_composed_session(data_directory, monkeypatch)

    response = client.post(
        PROVISIONING_CLAIM_PATH, json={"package": f"  {PACKAGE}\n"}, headers=headers
    )

    assert response.status_code == 200
    assert response.json() == {"state": "installation_registered"}
    stored = _composed_store(data_directory).verified_grants()
    assert [record.grant_type for record in stored] == [
        "installation_grant",
        "membership_snapshot",
        "license_entitlement",
    ]
    assert {record.installation_key_id for record in stored} == {
        installation_key.installation_key_id
    }


def test_the_claim_record_exists_before_the_hosted_call_and_resolves_after(
    store: SQLiteAuthenticationStore, grants: SignedGrants
) -> None:
    """The coordinates are durable while the outcome is still unknown."""

    transport = RecordObservingTransport(grants, store)

    assert submission(store, grants, transport)(package=PACKAGE) is None

    in_flight = transport.unresolved_at_request
    assert [record.claim_id for record in in_flight] == [CLAIM_ID]
    assert in_flight[0].onboarding_transaction_id == ONBOARDING_TRANSACTION_ID
    assert in_flight[0].organization_id == ORGANIZATION_ID
    assert in_flight[0].installation_id == INSTALLATION_ID
    resolved = store.claim_submission(CLAIM_ID)
    assert resolved.state is ClaimSubmissionState.CONSUMED
    assert resolved.outcome is None
    assert store.unresolved_claim_submissions() == ()


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (409, "claim_already_consumed"),
        (403, "claim_refused"),
        (503, "hosted_unavailable"),
    ],
)
def test_a_hosted_refusal_resolves_the_claim_record_with_its_reason(
    store: SQLiteAuthenticationStore,
    grants: SignedGrants,
    status: int,
    reason: str,
) -> None:
    transport = HostedClaimTransport(grants, status=status)

    assert submission(store, grants, transport)(package=PACKAGE) == reason

    recorded = store.claim_submission(CLAIM_ID)
    assert recorded.state is ClaimSubmissionState.REFUSED
    assert recorded.outcome == reason
    assert recorded.outcome in NONSECRET_REASONS
    assert store.unresolved_claim_submissions() == ()


def test_a_claim_consumed_without_a_usable_local_result_stays_recoverable(
    store: SQLiteAuthenticationStore, grants: SignedGrants
) -> None:
    """The hosted plane spent the claim; nothing local can say it did not."""

    transport = HostedClaimTransport(grants)
    submit = durable_claim_submission(
        lambda: BrokenGrantStore(store),
        hosted_origin=HOSTED_ORIGIN,
        transport_factory=_transport_factory(transport),
        proof_authority_factory=lambda _: FakeProofAuthority(
            grants.installation_key
        ),
        device_display_name=lambda: DEVICE_NAME,
        trust_set=grants.trust_set,
    )

    with pytest.raises(GrantStorageFailure):
        submit(package=PACKAGE)

    assert transport.consumed is True
    assert store.verified_grants() == ()
    unresolved = store.unresolved_claim_submissions()
    assert [record.claim_id for record in unresolved] == [CLAIM_ID]
    assert unresolved[0].onboarding_transaction_id == ONBOARDING_TRANSACTION_ID
    assert unresolved[0].resolved_at is None


@pytest.mark.parametrize(
    ("pasted", "hosted_origin"),
    [
        ("not a package", HOSTED_ORIGIN),
        (package({**claim_document(), "claim_secret": "short"}), HOSTED_ORIGIN),
        (PACKAGE, ""),
    ],
)
def test_a_claim_that_cannot_reach_the_plane_is_never_recorded(
    store: SQLiteAuthenticationStore,
    grants: SignedGrants,
    pasted: str,
    hosted_origin: str,
) -> None:
    """Only a claim the hosted plane could have consumed becomes durable."""

    transport = HostedClaimTransport(grants)

    def open_transport(origin: str) -> Any:
        # The real transport parses the origin; an empty one is unusable.
        if not origin:
            raise ValueError("hosted origin is unusable")
        return _transport_factory(transport)(origin)

    submit = durable_claim_submission(
        lambda: store,
        hosted_origin=hosted_origin,
        transport_factory=open_transport,
        proof_authority_factory=lambda _: FakeProofAuthority(
            grants.installation_key
        ),
        device_display_name=lambda: DEVICE_NAME,
        trust_set=grants.trust_set,
    )

    assert submit(package=pasted) is not None
    assert transport.requests == []
    assert store.claim_submission(CLAIM_ID) is None
    assert store.unresolved_claim_submissions() == ()


def test_the_claim_record_holds_only_the_nonsecret_coordinates(
    store: SQLiteAuthenticationStore, tmp_path: Path
) -> None:
    """The persisted set is exactly what the decoder calls durable."""

    decoded = decode_claim_package(PACKAGE)
    try:
        coordinates = set(decoded.durable_state())
    finally:
        decoded.clear()
    with sqlite3.connect(tmp_path / DATABASE_FILENAME) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({CLAIM_RECORD_TABLE})")
        }

    assert columns
    assert coordinates == CLAIM_RECORD_COORDINATES
    assert columns - CLAIM_RECORD_LOCAL_COLUMNS == CLAIM_RECORD_COORDINATES


def test_no_claim_secret_reaches_the_authentication_database(
    store: SQLiteAuthenticationStore, grants: SignedGrants, tmp_path: Path
) -> None:
    """The whole database file, not just the claim record, is searched."""

    transport = HostedClaimTransport(grants)
    assert submission(store, grants, transport)(package=PACKAGE) is None
    assert store.claim_submission(CLAIM_ID) is not None

    written = b"".join(
        path.read_bytes() for path in sorted(tmp_path.glob(f"{DATABASE_FILENAME}*"))
    )

    # The identifier proves the search reaches the row the secret was stripped from.
    assert CLAIM_ID.encode("ascii") in written
    assert CLAIM_SECRET.encode("ascii") not in written
    assert CHALLENGE.encode("ascii") not in written


def _emulate_platform(
    monkeypatch, provider: EmulatedPlatformProvider, transport: HostedClaimTransport
) -> None:
    """Replace the network and the platform key device, nothing else.

    Composition passes no factories, so the seam is the concrete implementation
    each default factory resolves when the client is built.
    """

    monkeypatch.setattr(
        submission_module, "HTTPXHostedTransport", _transport_factory(transport)
    )
    monkeypatch.setattr(
        submission_module, "WindowsCNGInstallationKeyProvider", lambda: provider
    )


def _composed_store(data_directory: Path) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(data_directory / DATABASE_FILENAME)


def _composed_installation_key(
    data_directory: Path, provider: EmulatedPlatformProvider
) -> InstallationKeyReference:
    """Reserve the installation key the composed application will find."""

    data_directory.mkdir(parents=True, exist_ok=True)
    return InstallationKeyAuthority(_composed_store(data_directory), provider).ensure_ready()


def _bounded_composed_session(
    data_directory: Path, monkeypatch
) -> tuple[TestClient, dict[str, str]]:
    """Boot the packaged provisioning surface and open one bounded session."""

    monkeypatch.setenv(
        packaged_entry.PROVISIONING_HANDOFF_ENVIRONMENT_VARIABLE, HANDOFF_TOKEN
    )
    monkeypatch.setenv(
        packaged_entry.PROVISIONING_HOSTED_ORIGIN_ENVIRONMENT_VARIABLE, HOSTED_ORIGIN
    )
    data_directory.mkdir(parents=True, exist_ok=True)
    application = packaged_entry.select_brain_application(data_directory)
    client = TestClient(application, base_url=PROVISIONING_ORIGIN)
    handoff = client.post(
        "/api/v1/provisioning/handoff",
        headers={"Authorization": "Provisioning " + HANDOFF_TOKEN},
    )
    redeemed = client.get(
        f"/provisioning/handoff?code={handoff.json()['handoff_code']}",
        follow_redirects=False,
    )
    cookie = (
        f"{PROVISIONING_SESSION_COOKIE_NAME}="
        f"{redeemed.cookies[PROVISIONING_SESSION_COOKIE_NAME]}"
    )
    token = (
        client.get("/provisioning", headers={"Cookie": cookie})
        .text.split('data-provisioning-csrf="')[1]
        .split('"')[0]
    )
    return client, {
        "Cookie": cookie,
        "Origin": PROVISIONING_ORIGIN,
        PROVISIONING_CSRF_HEADER: token,
    }


def _transport_factory(transport: HostedClaimTransport) -> Callable[[str], Any]:
    def open_transport(hosted_origin: str) -> HostedClaimTransport:
        transport.origin = hosted_origin
        return transport

    return open_transport


def _json_response(status_code: int, value: dict[str, object]) -> TransportResponse:
    return TransportResponse(
        status_code,
        json.dumps(value, separators=(",", ":")).encode("utf-8"),
        "application/json",
    )


def _signed_grants(installation_key: InstallationKeyReference) -> SignedGrants:
    """Sign one hosted claim result valid at the verifying installation's clock."""

    purpose_keys = {
        purpose: ec.generate_private_key(ec.SECP256R1())
        for purpose in ("installation-binding", "membership", "license")
    }
    entries = {
        purpose: _trust_entry(purpose, key) for purpose, key in purpose_keys.items()
    }
    issued = int(datetime.now(timezone.utc).timestamp())
    common: dict[str, object] = {
        "profile": "urn:bridge-clean:grant-profile:v1",
        "iss": "urn:bridge-clean:commercial-control-plane",
        "organization_id": ORGANIZATION_ID,
        "installation_id": INSTALLATION_ID,
        "installation_key_id": installation_key.installation_key_id,
        "installation_key_jkt": installation_key.installation_key_jkt,
        "iat": issued,
        "nbf": issued,
    }
    tokens = {
        "installation_grant": _token(
            {
                **common,
                "aud": "urn:bridge-clean:local-brain:installation",
                "exp": issued + GRANT_LIFETIMES["installation_grant"],
                "grant_type": "installation_grant",
                "jti": "0198b2c3-d4e5-7100-8000-000000000001",
                "sub": f"installation:{INSTALLATION_ID}",
            },
            "urn:bridge-clean:grant:installation:v1",
            entries["installation-binding"],
            purpose_keys["installation-binding"],
        ),
        "membership_snapshot": _token(
            {
                **common,
                "aud": "urn:bridge-clean:local-brain:membership",
                "allowed_creator_account_ids": [ACCOUNT_ID],
                "ciam_issuer": EXTERNAL_ISSUER,
                "ciam_subject": EXTERNAL_SUBJECT,
                "exp": issued + GRANT_LIFETIMES["membership_snapshot"],
                "grant_type": "membership_snapshot",
                "jti": "0198b2c3-d4e5-7100-8000-000000000002",
                "membership_id": "0198b2c3-d4e5-7200-8000-000000000001",
                "roles": ["creator_operator"],
                "scopes": ["runtime:existing-data", "runtime:licensed-processing"],
                "sub": "principal:"
                + _b64url(
                    _canonical(
                        {"issuer": EXTERNAL_ISSUER, "subject": EXTERNAL_SUBJECT}
                    )
                ),
            },
            "urn:bridge-clean:grant:membership:v1",
            entries["membership"],
            purpose_keys["membership"],
        ),
        "license_entitlement": _token(
            {
                **common,
                "aud": "urn:bridge-clean:local-brain:license",
                "entitlement_id": "0198b2c3-d4e5-7200-8000-000000000002",
                "exp": issued + GRANT_LIFETIMES["license_entitlement"],
                "features": ["analytics-processing", "ingestion"],
                "grant_type": "license_entitlement",
                "jti": "0198b2c3-d4e5-7100-8000-000000000003",
                "product_id": "bridge-clean",
                "sub": (
                    f"organization:{ORGANIZATION_ID}:installation:{INSTALLATION_ID}"
                ),
            },
            "urn:bridge-clean:grant:license:v1",
            entries["license"],
            purpose_keys["license"],
        ),
    }
    trust_set: dict[str, object] = {
        "profile": "urn:bridge-clean:grant-profile:v1",
        "keys": list(entries.values()),
        "production_usable": False,
    }
    return SignedGrants(tokens, trust_set, installation_key)


def _trust_entry(purpose: str, key: ec.EllipticCurvePrivateKey) -> dict[str, object]:
    numbers = key.public_key().public_numbers()
    jwk: dict[str, str] = {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }
    thumbprint = hashlib.sha256(_canonical(jwk)).digest()
    code = {"installation-binding": "ib", "membership": "ms", "license": "le"}[purpose]
    jwk["kid"] = f"bc1.{code}.{_b64url(thumbprint[:16])}"
    return {
        "fixture_only": True,
        "jwk": jwk,
        "purpose": purpose,
        "thumbprint": _b64url(thumbprint),
    }


def _token(
    payload: dict[str, object],
    typ: str,
    entry: dict[str, object],
    key: ec.EllipticCurvePrivateKey,
) -> str:
    jwk = entry["jwk"]
    assert isinstance(jwk, dict)
    header = {"alg": "ES256", "kid": jwk["kid"], "typ": typ}
    signing_input = b".".join(
        (
            _b64url(_canonical(header)).encode("ascii"),
            _b64url(_canonical(payload)).encode("ascii"),
        )
    )
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = r.to_bytes(32, "big") + min(s, _P256_ORDER - s).to_bytes(32, "big")
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


def _tampered(token: str) -> str:
    header, payload, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    return f"{header}.{payload}.{replacement}{signature[1:]}"


def _installation_key_reference() -> InstallationKeyReference:
    numbers = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    members = {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }
    thumbprint = hashlib.sha256(_canonical(members)).digest()
    key_id = f"ik1.{_b64url(thumbprint[:16])}"
    now = datetime.now(timezone.utc)
    return InstallationKeyReference(
        provider_name="test-provider",
        provider_key_name="test-key",
        algorithm=INSTALLATION_KEY_ALGORITHM,
        installation_key_id=key_id,
        installation_key_jkt=_b64url(thumbprint),
        public_key_jwk=_canonical({**members, "kid": key_id}).decode("ascii"),
        created_at=now,
        activated_at=now,
    )
