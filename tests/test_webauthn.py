from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.persistence.auth import SQLiteAuthenticationStore, VerifiedGrantReference
from app.security.webauthn import (
    AuthenticationCredential,
    RegistrationAuthority,
    RegistrationCredential,
    SessionAuthority,
    WebAuthnService,
    WebAuthnVerificationError,
    configured_webauthn_service,
)


RP_ID = "bridge.localhost"
ORIGIN = "http://bridge.localhost:17871"
PRINCIPAL_ID = "principal-1"
CREDENTIAL_ID_BYTES = b"credential-id-1"
CREDENTIAL_ID = base64.urlsafe_b64encode(CREDENTIAL_ID_BYTES).rstrip(b"=").decode()


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def instant() -> datetime:
    return datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(instant: datetime) -> MutableClock:
    return MutableClock(instant)


@pytest.fixture
def store(tmp_path: Path, clock: MutableClock) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(tmp_path / "auth.sqlite3", clock=clock)


@pytest.fixture
def service(
    store: SQLiteAuthenticationStore, clock: MutableClock
) -> WebAuthnService:
    return WebAuthnService(
        store,
        relying_party_id=RP_ID,
        expected_origin=ORIGIN,
        relying_party_name="Local analytics",
        challenge_ttl_seconds=300,
        session_ttl_seconds=3_600,
        clock=clock,
    )


@pytest.fixture
def registration_authority() -> RegistrationAuthority:
    return RegistrationAuthority(
        principal_id=PRINCIPAL_ID,
        external_issuer="https://identity.example",
        external_subject="customer-subject",
        installation_id="installation-1",
    )


def test_configured_challenge_lifetime_defaults_to_300_seconds(
    store: SQLiteAuthenticationStore, clock: MutableClock
) -> None:
    assert settings.webauthn_challenge_ttl_seconds == 300
    configured = configured_webauthn_service(store, clock=clock)
    options = configured.begin_registration(
        RegistrationAuthority(PRINCIPAL_ID, "issuer", "subject", "installation")
    )
    assert options["timeout"] == 300_000
    with pytest.raises(ValidationError):
        Settings(
            websocket_auth_mode="development_stub",
            webauthn_challenge_ttl_seconds=0,
        )
    with pytest.raises(ValidationError):
        Settings(
            websocket_auth_mode="development_stub",
            webauthn_challenge_ttl_seconds=601,
        )


def test_registration_verifies_attested_es256_key_and_persists_public_material_only(
    service: WebAuthnService,
    store: SQLiteAuthenticationStore,
    registration_authority: RegistrationAuthority,
) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    options = service.begin_registration(registration_authority)
    response = registration_response(private_key, str(options["challenge"]))

    credential = service.complete_registration(registration_authority, response)

    stored = store.webauthn_credential(CREDENTIAL_ID, principal_id=PRINCIPAL_ID)
    assert stored == credential
    assert stored is not None
    public_key = serialization.load_der_public_key(stored.public_key)
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    with store.database.read() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(webauthn_credentials)")
        }
    assert "private_key" not in columns
    assert "client_data_json" not in columns
    assert "attestation_object" not in columns


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("type", "type"),
        ("origin", "origin"),
        ("rp", "relying party"),
        ("presence", "presence"),
        ("verification", "verification"),
        ("algorithm", "ES256"),
    ],
)
def test_registration_rejects_wrong_client_and_authenticator_bindings(
    service: WebAuthnService,
    registration_authority: RegistrationAuthority,
    case: str,
    message: str,
) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    options = service.begin_registration(registration_authority)
    challenge = str(options["challenge"])
    valid = registration_response(private_key, challenge)

    with pytest.raises(WebAuthnVerificationError, match=message):
        service.complete_registration(
            registration_authority,
            invalid_registration_response(valid, case, challenge),
        )


def test_authentication_verifies_signature_and_mints_opaque_durable_session(
    service: WebAuthnService,
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    registration_authority: RegistrationAuthority,
) -> None:
    private_key = enrolled_credential(service, registration_authority)
    grants = record_bridge_grants(store, clock.value, registration_authority)
    authority = session_authority(grants)
    options = service.begin_authentication(authority)
    response = authentication_response(private_key, str(options["challenge"]), count=1)

    session = service.complete_authentication(authority, response)

    assert session.principal_id == PRINCIPAL_ID
    assert session.creator_account_id == "creator-1"
    assert session.role == "creator"
    assert len(base64.urlsafe_b64decode(session.session_value + "==")) == 32
    assert store.read_bridge_session(session.session_value) is not None
    database_bytes = store.database.path.read_bytes()
    assert session.session_value.encode() not in database_bytes
    assert session.csrf_value.encode() not in database_bytes
    stored = store.webauthn_credential(CREDENTIAL_ID, principal_id=PRINCIPAL_ID)
    assert stored is not None and stored.signature_count == 1


def test_consumed_authentication_challenge_cannot_be_replayed(
    service: WebAuthnService,
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    registration_authority: RegistrationAuthority,
) -> None:
    private_key = enrolled_credential(service, registration_authority)
    authority = session_authority(
        record_bridge_grants(store, clock.value, registration_authority)
    )
    options = service.begin_authentication(authority)
    response = authentication_response(private_key, str(options["challenge"]), count=0)

    first = service.complete_authentication(authority, response)
    with pytest.raises(WebAuthnVerificationError, match="challenge is invalid or expired"):
        service.complete_authentication(authority, response)

    assert store.read_bridge_session(first.session_value) is not None
    with store.database.read() as connection:
        sessions = connection.execute("SELECT COUNT(*) FROM bridge_sessions").fetchone()[0]
        consumed = connection.execute(
            "SELECT COUNT(*) FROM auth_challenges WHERE consumed_at IS NOT NULL"
        ).fetchone()[0]
    assert sessions == 1
    assert consumed == 2


def test_invalid_signature_does_not_mint_a_session(
    service: WebAuthnService,
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    registration_authority: RegistrationAuthority,
) -> None:
    private_key = enrolled_credential(service, registration_authority)
    authority = session_authority(
        record_bridge_grants(store, clock.value, registration_authority)
    )
    options = service.begin_authentication(authority)
    valid = authentication_response(private_key, str(options["challenge"]), count=1)
    signature = bytearray(_decode(valid.signature))
    signature[-1] ^= 1
    invalid = replace(valid, signature=_encode(bytes(signature)))

    with pytest.raises(WebAuthnVerificationError, match="signature is invalid"):
        service.complete_authentication(authority, invalid)
    session = service.complete_authentication(authority, valid)

    assert store.read_bridge_session(session.session_value) is not None


def test_challenge_expiry_is_enforced_by_atomic_consume(
    service: WebAuthnService,
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    registration_authority: RegistrationAuthority,
) -> None:
    private_key = enrolled_credential(service, registration_authority)
    authority = session_authority(
        record_bridge_grants(store, clock.value, registration_authority)
    )
    options = service.begin_authentication(authority)
    response = authentication_response(private_key, str(options["challenge"]), count=1)
    clock.value += timedelta(seconds=300)

    with pytest.raises(WebAuthnVerificationError, match="challenge is invalid or expired"):
        service.complete_authentication(authority, response)

    with store.database.read() as connection:
        challenge = connection.execute(
            """
            SELECT consumed_at FROM auth_challenges
            WHERE credential_id = ? ORDER BY issued_at DESC LIMIT 1
            """,
            (CREDENTIAL_ID,),
        ).fetchone()
        sessions = connection.execute("SELECT COUNT(*) FROM bridge_sessions").fetchone()[0]
    assert challenge["consumed_at"] is None
    assert sessions == 0


def test_zero_only_authenticator_can_authenticate_repeatedly(
    service: WebAuthnService,
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    registration_authority: RegistrationAuthority,
) -> None:
    private_key = enrolled_credential(service, registration_authority, count=0)
    authority = session_authority(
        record_bridge_grants(store, clock.value, registration_authority)
    )

    for _ in range(2):
        options = service.begin_authentication(authority)
        response = authentication_response(
            private_key, str(options["challenge"]), count=0
        )
        service.complete_authentication(authority, response)

    stored = store.webauthn_credential(CREDENTIAL_ID, principal_id=PRINCIPAL_ID)
    assert stored is not None and stored.signature_count == 0


def test_non_increasing_positive_counter_rejects_the_assertion(
    service: WebAuthnService,
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    registration_authority: RegistrationAuthority,
) -> None:
    private_key = enrolled_credential(service, registration_authority, count=4)
    authority = session_authority(
        record_bridge_grants(store, clock.value, registration_authority)
    )
    first_options = service.begin_authentication(authority)
    service.complete_authentication(
        authority,
        authentication_response(private_key, str(first_options["challenge"]), count=5),
    )
    second_options = service.begin_authentication(authority)

    with pytest.raises(WebAuthnVerificationError, match="counter did not advance"):
        service.complete_authentication(
            authority,
            authentication_response(
                private_key, str(second_options["challenge"]), count=5
            ),
        )

    stored = store.webauthn_credential(CREDENTIAL_ID, principal_id=PRINCIPAL_ID)
    assert stored is not None and stored.signature_count == 5


def test_authentication_rejects_registration_client_data(
    service: WebAuthnService,
    store: SQLiteAuthenticationStore,
    clock: MutableClock,
    registration_authority: RegistrationAuthority,
) -> None:
    private_key = enrolled_credential(service, registration_authority)
    authority = session_authority(
        record_bridge_grants(store, clock.value, registration_authority)
    )
    options = service.begin_authentication(authority)
    response = authentication_response(private_key, str(options["challenge"]), count=1)
    registration_data = client_data("webauthn.create", str(options["challenge"]))

    with pytest.raises(WebAuthnVerificationError, match="type is invalid"):
        service.complete_authentication(
            authority, replace(response, client_data_json=registration_data)
        )


def enrolled_credential(
    service: WebAuthnService,
    authority: RegistrationAuthority,
    *,
    count: int = 0,
) -> ec.EllipticCurvePrivateKey:
    private_key = ec.generate_private_key(ec.SECP256R1())
    options = service.begin_registration(authority)
    service.complete_registration(
        authority,
        registration_response(private_key, str(options["challenge"]), count=count),
    )
    return private_key


def session_authority(grants: tuple[str, ...]) -> SessionAuthority:
    return SessionAuthority(
        principal_id=PRINCIPAL_ID,
        credential_id=CREDENTIAL_ID,
        creator_account_id="creator-1",
        role="creator",
        grant_reference_ids=grants,
    )


def record_bridge_grants(
    store: SQLiteAuthenticationStore,
    instant: datetime,
    authority: RegistrationAuthority,
) -> tuple[str, ...]:
    references: list[str] = []
    for grant_type, creator_account_id in (
        ("installation_grant", None),
        ("membership_snapshot", None),
        ("creator_account_binding", "creator-1"),
    ):
        reference_id = f"{grant_type}-reference"
        references.append(reference_id)
        store.record_verified_grant(
            VerifiedGrantReference(
                reference_id=reference_id,
                grant_identifier=f"{grant_type}-identifier",
                grant_type=grant_type,
                grant_digest=hashlib.sha256(grant_type.encode()).hexdigest(),
                issuer=authority.external_issuer,
                subject=authority.external_subject,
                installation_id=authority.installation_id,
                creator_account_id=creator_account_id,
                valid_from=instant - timedelta(minutes=1),
                expires_at=instant + timedelta(hours=2),
                verified_at=instant - timedelta(minutes=1),
            )
        )
    return tuple(references)


def registration_response(
    private_key: ec.EllipticCurvePrivateKey,
    challenge: str,
    *,
    origin: str = ORIGIN,
    rp_id: str = RP_ID,
    flags: int = 0x45,
    cose_alg: int = -7,
    count: int = 0,
) -> RegistrationCredential:
    numbers = private_key.public_key().public_numbers()
    cose_key = {
        1: 2,
        3: cose_alg,
        -1: 1,
        -2: numbers.x.to_bytes(32, "big"),
        -3: numbers.y.to_bytes(32, "big"),
    }
    authenticator_data = b"".join(
        (
            hashlib.sha256(rp_id.encode()).digest(),
            bytes([flags]),
            count.to_bytes(4, "big"),
            bytes(16),
            len(CREDENTIAL_ID_BYTES).to_bytes(2, "big"),
            CREDENTIAL_ID_BYTES,
            cbor(cose_key),
        )
    )
    attestation = cbor(
        {"fmt": "none", "authData": authenticator_data, "attStmt": {}}
    )
    return RegistrationCredential(
        credential_id=CREDENTIAL_ID,
        raw_id=CREDENTIAL_ID,
        credential_type="public-key",
        client_data_json=client_data("webauthn.create", challenge, origin=origin),
        attestation_object=_encode(attestation),
    )


def invalid_registration_response(
    valid: RegistrationCredential, case: str, challenge: str
) -> RegistrationCredential:
    if case == "type":
        return replace(
            valid, client_data_json=client_data("webauthn.get", challenge)
        )
    if case == "origin":
        return replace(
            valid,
            client_data_json=client_data(
                "webauthn.create", challenge, origin="http://wrong.localhost"
            ),
        )
    private_key = ec.generate_private_key(ec.SECP256R1())
    changes = {
        "rp": {"rp_id": "wrong.localhost"},
        "presence": {"flags": 0x44},
        "verification": {"flags": 0x41},
        "algorithm": {"cose_alg": -257},
    }
    changed = registration_response(private_key, challenge, **changes[case])
    return replace(valid, attestation_object=changed.attestation_object)


def authentication_response(
    private_key: ec.EllipticCurvePrivateKey,
    challenge: str,
    *,
    count: int,
) -> AuthenticationCredential:
    encoded_client_data = client_data("webauthn.get", challenge)
    raw_client_data = _decode(encoded_client_data)
    authenticator_data = b"".join(
        (
            hashlib.sha256(RP_ID.encode()).digest(),
            b"\x05",
            count.to_bytes(4, "big"),
        )
    )
    signature = private_key.sign(
        authenticator_data + hashlib.sha256(raw_client_data).digest(),
        ec.ECDSA(hashes.SHA256()),
    )
    return AuthenticationCredential(
        credential_id=CREDENTIAL_ID,
        raw_id=CREDENTIAL_ID,
        credential_type="public-key",
        client_data_json=encoded_client_data,
        authenticator_data=_encode(authenticator_data),
        signature=_encode(signature),
        user_handle=_encode(hashlib.sha256(PRINCIPAL_ID.encode()).digest()),
    )


def client_data(kind: str, challenge: str, *, origin: str = ORIGIN) -> str:
    raw = json.dumps(
        {
            "type": kind,
            "challenge": challenge,
            "origin": origin,
            "crossOrigin": False,
        },
        separators=(",", ":"),
    ).encode()
    return _encode(raw)


def cbor(value: object) -> bytes:
    if isinstance(value, int):
        return cbor_head(0, value) if value >= 0 else cbor_head(1, -1 - value)
    if isinstance(value, bytes):
        return cbor_head(2, len(value)) + value
    if isinstance(value, str):
        encoded = value.encode()
        return cbor_head(3, len(encoded)) + encoded
    if isinstance(value, dict):
        return cbor_head(5, len(value)) + b"".join(
            cbor(key) + cbor(item) for key, item in value.items()
        )
    raise TypeError(f"unsupported CBOR test value: {type(value)!r}")


def cbor_head(major: int, argument: int) -> bytes:
    if argument < 24:
        return bytes([(major << 5) | argument])
    if argument < 256:
        return bytes([(major << 5) | 24, argument])
    if argument < 65_536:
        return bytes([(major << 5) | 25]) + argument.to_bytes(2, "big")
    return bytes([(major << 5) | 26]) + argument.to_bytes(4, "big")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
