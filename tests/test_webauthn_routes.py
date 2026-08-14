from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.activation import require_activated_runtime
from app.api import security as api_security
from app.api.endpoints import webauthn as webauthn_endpoints
from app.api.security import (
    csrf_token,
    get_authenticated_runtime_policy,
    get_runtime_policy,
)
from app.core.config import settings
from app.persistence.auth import (
    InstallationKeyReference,
    InstallationKeyReservation,
    RevocationKey,
    RevocationScopeType,
    SQLiteAuthenticationStore,
    VerifiedGrantReference,
    WebAuthnCredential,
)
from app.security.runtime_policy import AuthContext, RuntimePolicy
from app.security.webauthn import (
    RegistrationAuthority,
    WebAuthnAuthorityDecision,
    WebAuthnAuthorityPort,
    WebAuthnAuthorityResult,
    WebAuthnService,
)


INSTANT = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
ORIGIN = "http://bridge.localhost:17871"
RP_ID = "bridge.localhost"
PRINCIPAL_ID = "principal-http"
ACCOUNT_ID = "creator-http"
ISSUER = "https://identity.example"
SUBJECT = "subject-http"
INSTALLATION_ID = "installation-http"
INSTALLATION_KEY_ID = "installation-key-http"
INSTALLATION_KEY_JKT = "installation-thumbprint-http"
CREDENTIAL_BYTES = b"credential-http"
CREDENTIAL_ID = base64.urlsafe_b64encode(CREDENTIAL_BYTES).rstrip(b"=").decode()


@pytest.fixture
def store(tmp_path: Path) -> SQLiteAuthenticationStore:
    return _seeded_store(tmp_path / "auth.sqlite3", INSTANT)


def _seeded_store(path: Path, instant: datetime) -> SQLiteAuthenticationStore:
    authentication_store = SQLiteAuthenticationStore(path, clock=lambda: instant)
    authentication_store.reserve_installation_key(
        InstallationKeyReservation(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            created_at=instant - timedelta(minutes=10),
        )
    )
    authentication_store.activate_installation_key(
        InstallationKeyReference(
            provider_name="test-provider",
            provider_key_name="test-key",
            algorithm="ES256",
            installation_key_id=INSTALLATION_KEY_ID,
            installation_key_jkt=INSTALLATION_KEY_JKT,
            public_key_jwk='{"kty":"EC"}',
            created_at=instant - timedelta(minutes=10),
            activated_at=instant - timedelta(minutes=9),
        )
    )
    for grant_type in (
        "installation_grant",
        "membership_snapshot",
        "creator_account_binding",
    ):
        authentication_store.record_verified_grant(
            VerifiedGrantReference(
                reference_id=f"{grant_type}-http",
                grant_identifier=f"{grant_type}-identifier-http",
                grant_type=grant_type,
                grant_digest=hashlib.sha256(grant_type.encode()).hexdigest(),
                issuer=ISSUER,
                subject=SUBJECT,
                installation_id=INSTALLATION_ID,
                creator_account_id=(
                    ACCOUNT_ID if grant_type == "creator_account_binding" else None
                ),
                valid_from=instant - timedelta(hours=1),
                expires_at=instant + timedelta(hours=1),
                verified_at=instant - timedelta(minutes=5),
                organization_id="organization-http",
                installation_key_id=INSTALLATION_KEY_ID,
                installation_key_jkt=INSTALLATION_KEY_JKT,
                membership_id=(
                    "membership-http" if grant_type == "membership_snapshot" else None
                ),
                allowed_creator_account_ids=(
                    (ACCOUNT_ID,) if grant_type == "membership_snapshot" else None
                ),
                membership_roles=(
                    ("owner",) if grant_type == "membership_snapshot" else None
                ),
            )
        )
    return authentication_store


def _policy(store: SQLiteAuthenticationStore) -> RuntimePolicy:
    return store.build_runtime_policy(
        AuthContext(
            principal_id=PRINCIPAL_ID,
            creator_account_id=ACCOUNT_ID,
            role="creator",
            platform_creator_id="platform-http",
            session_id="bootstrap-session-http",
        )
    )


def _service(store: SQLiteAuthenticationStore) -> WebAuthnService:
    return WebAuthnService(
        store,
        relying_party_id=RP_ID,
        expected_origin=ORIGIN,
        relying_party_name="Local analytics",
        challenge_ttl_seconds=300,
        session_ttl_seconds=3_600,
        clock=lambda: INSTANT,
    )


def _application(
    *,
    policy: RuntimePolicy | Callable[[], RuntimePolicy] | None = None,
    authority_port: object | None = None,
    service: WebAuthnService | None = None,
) -> FastAPI:
    application = FastAPI()
    application.include_router(webauthn_endpoints.router)
    application.dependency_overrides[require_activated_runtime] = lambda: None
    if policy is not None:
        application.dependency_overrides[get_runtime_policy] = (
            policy if callable(policy) else lambda: policy
        )
    if authority_port is not None:
        application.dependency_overrides[
            webauthn_endpoints.webauthn_authority_port
        ] = lambda: authority_port
    if service is not None:
        application.dependency_overrides[webauthn_endpoints.webauthn_service] = (
            lambda: service
        )
    return application


class _RefusingPort:
    def __init__(self, result: WebAuthnAuthorityResult) -> None:
        self._decision = WebAuthnAuthorityDecision(result)

    def registration_authority(
        self, policy: RuntimePolicy
    ) -> WebAuthnAuthorityDecision:
        return self._decision

    def session_authority(self, policy: RuntimePolicy) -> WebAuthnAuthorityDecision:
        return self._decision


class _AuthorizedRegistrationPort:
    def registration_authority(
        self, policy: RuntimePolicy
    ) -> WebAuthnAuthorityDecision:
        return WebAuthnAuthorityDecision(
            WebAuthnAuthorityResult.AUTHORIZED,
            RegistrationAuthority(
                principal_id=PRINCIPAL_ID,
                external_issuer=ISSUER,
                external_subject=SUBJECT,
                installation_id=INSTALLATION_ID,
            ),
        )

    def session_authority(self, policy: RuntimePolicy) -> WebAuthnAuthorityDecision:
        return WebAuthnAuthorityDecision(WebAuthnAuthorityResult.CREDENTIAL_MISSING)


def _finish_body(*, login: bool) -> dict[str, object]:
    response = (
        {
            "clientDataJSON": "placeholder",
            "authenticatorData": "placeholder",
            "signature": "placeholder",
            "userHandle": None,
        }
        if login
        else {
            "clientDataJSON": "placeholder",
            "attestationObject": "placeholder",
        }
    )
    return {
        "id": "placeholder",
        "rawId": "placeholder",
        "type": "public-key",
        "response": response,
    }


def test_login_begin_succeeds_without_a_bridge_session_cookie_with_real_authority_port(
    monkeypatch: pytest.MonkeyPatch,
    store: SQLiteAuthenticationStore,
) -> None:
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    monkeypatch.setattr(api_security, "build_runtime_policy", store.build_runtime_policy)
    _register_public_key(store, ec.generate_private_key(ec.SECP256R1()))
    with TestClient(
        _application(
            authority_port=WebAuthnAuthorityPort(store, clock=lambda: INSTANT),
            service=_service(store),
        ),
        base_url=ORIGIN,
    ) as client:
        response = client.post(
            "/api/v1/webauthn/login/begin",
            headers={"Origin": ORIGIN},
        )

    assert response.status_code == 200
    assert response.json()["challenge"]


def test_registration_begin_succeeds_without_a_bridge_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
    store: SQLiteAuthenticationStore,
) -> None:
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    monkeypatch.setattr(settings, "auth_database_path", store.database.path)
    monkeypatch.setattr(api_security, "build_runtime_policy", store.build_runtime_policy)
    application = _application(
        authority_port=WebAuthnAuthorityPort(store, clock=lambda: INSTANT),
        service=_service(store),
    )

    with TestClient(application, base_url=ORIGIN) as client:
        assert settings.bridge_session_cookie_name not in client.cookies
        response = client.post(
            "/api/v1/webauthn/registration/begin",
            headers={"Origin": ORIGIN},
        )

    assert response.status_code == 200
    assert response.json()["challenge"]


def test_login_finish_without_a_prior_session_mints_grant_authority_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    instant = datetime.now(timezone.utc)
    store = _seeded_store(tmp_path / "webauthn-login.sqlite3", instant)
    monkeypatch.setattr(settings, "websocket_auth_mode", "local_session")
    monkeypatch.setattr(settings, "auth_database_path", store.database.path)
    monkeypatch.setattr(settings, "local_principal_id", "configured-principal")
    monkeypatch.setattr(settings, "local_bridge_role", "operator")
    monkeypatch.setattr(api_security, "build_runtime_policy", store.build_runtime_policy)
    private_key = ec.generate_private_key(ec.SECP256R1())
    _register_public_key(store, private_key)
    application = _application()

    @application.get("/test/session-identity")
    def session_identity(
        policy: RuntimePolicy = Depends(get_authenticated_runtime_policy),
    ) -> dict[str, str | None]:
        assert policy.identity is not None
        return {
            "principal_id": policy.identity.principal_id,
            "creator_account_id": policy.identity.creator_account_id,
            "role": policy.identity.role,
            "platform_creator_id": policy.identity.platform_creator_id,
        }

    with TestClient(application, base_url=ORIGIN) as client:
        assert settings.bridge_session_cookie_name not in client.cookies
        begun = client.post(
            "/api/v1/webauthn/login/begin", headers={"Origin": ORIGIN}
        )
        finished = client.post(
            "/api/v1/webauthn/login/finish",
            json=_authentication_payload(private_key, begun.json()["challenge"]),
            headers={"Origin": ORIGIN},
        )
        session_cookie = finished.cookies[settings.bridge_session_cookie_name]
        authenticated = client.get(
            "/test/session-identity",
            headers={
                "Cookie": f"{settings.bridge_session_cookie_name}={session_cookie}"
            },
        )
        reauthentication = client.post(
            "/api/v1/webauthn/login/begin",
            headers={
                "Cookie": f"{settings.bridge_session_cookie_name}={session_cookie}",
                "Origin": ORIGIN,
                "X-CSRF-Token": finished.json()["csrf_token"],
            },
        )
        with store.database.read() as connection:
            session_id = connection.execute(
                "SELECT session_id FROM bridge_sessions"
            ).fetchone()["session_id"]
        store.revoke(
            RevocationKey(RevocationScopeType.BRIDGE_SESSION, session_id),
            reason="test revocation",
        )
        revoked = client.get(
            "/test/session-identity",
            headers={
                "Cookie": f"{settings.bridge_session_cookie_name}={session_cookie}"
            },
        )
        recovery = client.post(
            "/api/v1/webauthn/login/begin",
            headers={
                "Cookie": f"{settings.bridge_session_cookie_name}={session_cookie}",
                "Origin": ORIGIN,
            },
        )

    assert begun.status_code == 200
    assert finished.status_code == 200
    assert authenticated.status_code == 200
    assert revoked.status_code == 401
    assert revoked.json() == {"detail": "Authenticated session is invalid"}
    assert recovery.status_code == 200
    assert recovery.json()["challenge"]
    assert reauthentication.status_code == 200
    assert authenticated.json() == {
        "principal_id": PRINCIPAL_ID,
        "creator_account_id": ACCOUNT_ID,
        "role": "creator",
        "platform_creator_id": None,
    }


def test_identity_carrying_webauthn_request_rejects_wrong_csrf_value(
    store: SQLiteAuthenticationStore,
) -> None:
    application = _application(
        policy=_policy(store),
        authority_port=_AuthorizedRegistrationPort(),
        service=_service(store),
    )

    with TestClient(application, base_url=ORIGIN) as client:
        response = client.post(
            "/api/v1/webauthn/registration/begin",
            headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "CSRF token is invalid"}


def test_port_refusal_is_non_successful_and_issues_no_session(
    store: SQLiteAuthenticationStore,
) -> None:
    policy = _policy(store)
    application = _application(
        policy=lambda: _policy(store),
        authority_port=_RefusingPort(WebAuthnAuthorityResult.REQUIRED_GRANT_REVOKED),
        service=_service(store),
    )
    with TestClient(application, base_url=ORIGIN) as client:
        response = client.post(
            "/api/v1/webauthn/login/finish",
            json=_finish_body(login=True),
            headers={"X-CSRF-Token": csrf_token(policy)},
        )

    with store.database.read() as connection:
        session_count = connection.execute(
            "SELECT COUNT(*) FROM bridge_sessions"
        ).fetchone()[0]
    assert response.status_code == 403
    assert session_count == 0
    assert "set-cookie" not in response.headers


def test_registration_routes_complete_one_http_ceremony(
    store: SQLiteAuthenticationStore,
) -> None:
    policy = _policy(store)
    private_key = ec.generate_private_key(ec.SECP256R1())
    application = _application(
        policy=lambda: _policy(store),
        authority_port=WebAuthnAuthorityPort(store, clock=lambda: INSTANT),
        service=_service(store),
    )
    headers = {"X-CSRF-Token": csrf_token(policy)}
    with TestClient(application, base_url=ORIGIN) as client:
        begun = client.post(
            "/api/v1/webauthn/registration/begin", headers=headers
        )
        finished = client.post(
            "/api/v1/webauthn/registration/finish",
            json=_registration_payload(private_key, begun.json()["challenge"]),
            headers=headers,
        )

    assert begun.status_code == 200
    assert begun.headers["cache-control"] == "no-store"
    assert finished.status_code == 200
    assert finished.json() == {"status": "registered"}
    assert store.webauthn_credential(CREDENTIAL_ID, principal_id=PRINCIPAL_ID)


def test_consumed_http_login_challenge_cannot_be_replayed(
    store: SQLiteAuthenticationStore,
) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    _register_public_key(store, private_key)
    policy = _policy(store)
    application = _application(
        policy=lambda: _policy(store),
        authority_port=WebAuthnAuthorityPort(store, clock=lambda: INSTANT),
        service=_service(store),
    )
    headers = {"X-CSRF-Token": csrf_token(policy)}
    with TestClient(application, base_url=ORIGIN) as client:
        begun = client.post("/api/v1/webauthn/login/begin", headers=headers)
        assertion = _authentication_payload(private_key, begun.json()["challenge"])
        first = client.post(
            "/api/v1/webauthn/login/finish", json=assertion, headers=headers
        )
        replay = client.post(
            "/api/v1/webauthn/login/finish", json=assertion, headers=headers
        )

    with store.database.read() as connection:
        session_count = connection.execute(
            "SELECT COUNT(*) FROM bridge_sessions"
        ).fetchone()[0]
    assert begun.status_code == 200
    assert first.status_code == 200
    assert first.json()["csrf_token"]
    assert settings.bridge_session_cookie_name in first.headers["set-cookie"]
    assert "HttpOnly" in first.headers["set-cookie"]
    assert "Secure" in first.headers["set-cookie"]
    assert "SameSite=strict" in first.headers["set-cookie"]
    assert replay.status_code == 400
    assert replay.json() == {"detail": "WebAuthn ceremony could not be completed"}
    assert "challenge is invalid" not in replay.text
    assert session_count == 1


@pytest.mark.parametrize(
    "result",
    [
        result
        for result in WebAuthnAuthorityResult
        if result is not WebAuthnAuthorityResult.AUTHORIZED
    ],
)
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/webauthn/registration/begin", None),
        ("/api/v1/webauthn/registration/finish", _finish_body(login=False)),
        ("/api/v1/webauthn/login/begin", None),
        ("/api/v1/webauthn/login/finish", _finish_body(login=True)),
    ],
)
def test_internal_authority_result_identifier_never_appears_in_response_body(
    store: SQLiteAuthenticationStore,
    result: WebAuthnAuthorityResult,
    path: str,
    body: dict[str, object] | None,
) -> None:
    policy = _policy(store)
    application = _application(
        policy=policy,
        authority_port=_RefusingPort(result),
        service=_service(store),
    )
    with TestClient(application, base_url=ORIGIN) as client:
        response = client.post(
            path,
            json=body,
            headers={"X-CSRF-Token": csrf_token(policy)},
        )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "WebAuthn is not available for this session"
    }
    assert result.value not in response.text


@pytest.mark.parametrize(
    ("login", "field_path", "maximum"),
    [
        (False, ("id",), 2_048),
        (False, ("rawId",), 2_048),
        (False, ("response", "clientDataJSON"), 100_000),
        (False, ("response", "attestationObject"), 100_000),
        (True, ("id",), 2_048),
        (True, ("rawId",), 2_048),
        (True, ("response", "clientDataJSON"), 100_000),
        (True, ("response", "authenticatorData"), 100_000),
        (True, ("response", "signature"), 100_000),
        (True, ("response", "userHandle"), 100_000),
    ],
)
def test_invalid_webauthn_proof_is_not_echoed_by_request_validation(
    store: SQLiteAuthenticationStore,
    login: bool,
    field_path: tuple[str, ...],
    maximum: int,
) -> None:
    policy = _policy(store)
    marker = "proof-must-not-leak-" + "x" * maximum
    body = _finish_body(login=login)
    target = body
    for key in field_path[:-1]:
        child = target[key]
        assert isinstance(child, dict)
        target = child
    target[field_path[-1]] = marker
    application = _application(
        policy=policy,
        authority_port=_RefusingPort(WebAuthnAuthorityResult.REQUIRED_GRANT_REVOKED),
        service=_service(store),
    )

    with TestClient(application, base_url=ORIGIN) as client:
        response = client.post(
            (
                "/api/v1/webauthn/login/finish"
                if login
                else "/api/v1/webauthn/registration/finish"
            ),
            json=body,
            headers={"X-CSRF-Token": csrf_token(policy)},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "WebAuthn request is invalid"}
    assert response.headers["cache-control"] == "no-store"
    assert marker not in response.text


def _register_public_key(
    store: SQLiteAuthenticationStore,
    private_key: ec.EllipticCurvePrivateKey,
) -> None:
    store.register_webauthn_credential(
        WebAuthnCredential(
            credential_id=CREDENTIAL_ID,
            principal_id=PRINCIPAL_ID,
            external_issuer=ISSUER,
            external_subject=SUBJECT,
            installation_id=INSTALLATION_ID,
            public_key=private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
            signature_count=0,
            enrolled_at=INSTANT - timedelta(minutes=1),
        )
    )


def _registration_payload(
    private_key: ec.EllipticCurvePrivateKey,
    challenge: str,
) -> dict[str, object]:
    public_numbers = private_key.public_key().public_numbers()
    cose_key = {
        1: 2,
        3: -7,
        -1: 1,
        -2: public_numbers.x.to_bytes(32, "big"),
        -3: public_numbers.y.to_bytes(32, "big"),
    }
    authenticator_data = b"".join(
        (
            hashlib.sha256(RP_ID.encode()).digest(),
            b"\x45",
            (0).to_bytes(4, "big"),
            bytes(16),
            len(CREDENTIAL_BYTES).to_bytes(2, "big"),
            CREDENTIAL_BYTES,
            _cbor(cose_key),
        )
    )
    attestation = _cbor(
        {"fmt": "none", "authData": authenticator_data, "attStmt": {}}
    )
    return {
        "id": CREDENTIAL_ID,
        "rawId": CREDENTIAL_ID,
        "type": "public-key",
        "response": {
            "clientDataJSON": _client_data("webauthn.create", challenge),
            "attestationObject": _encode(attestation),
        },
    }


def _authentication_payload(
    private_key: ec.EllipticCurvePrivateKey,
    challenge: str,
) -> dict[str, object]:
    encoded_client_data = _client_data("webauthn.get", challenge)
    raw_client_data = _decode(encoded_client_data)
    authenticator_data = b"".join(
        (
            hashlib.sha256(RP_ID.encode()).digest(),
            b"\x05",
            (1).to_bytes(4, "big"),
        )
    )
    signature = private_key.sign(
        authenticator_data + hashlib.sha256(raw_client_data).digest(),
        ec.ECDSA(hashes.SHA256()),
    )
    return {
        "id": CREDENTIAL_ID,
        "rawId": CREDENTIAL_ID,
        "type": "public-key",
        "response": {
            "clientDataJSON": encoded_client_data,
            "authenticatorData": _encode(authenticator_data),
            "signature": _encode(signature),
            "userHandle": _encode(hashlib.sha256(PRINCIPAL_ID.encode()).digest()),
        },
    }


def _client_data(kind: str, challenge: str) -> str:
    return _encode(
        json.dumps(
            {
                "type": kind,
                "challenge": challenge,
                "origin": ORIGIN,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode()
    )


def _cbor(value: object) -> bytes:
    if isinstance(value, int):
        return _cbor_head(0, value) if value >= 0 else _cbor_head(1, -1 - value)
    if isinstance(value, bytes):
        return _cbor_head(2, len(value)) + value
    if isinstance(value, str):
        encoded = value.encode()
        return _cbor_head(3, len(encoded)) + encoded
    if isinstance(value, dict):
        return _cbor_head(5, len(value)) + b"".join(
            _cbor(key) + _cbor(item) for key, item in value.items()
        )
    raise TypeError(f"unsupported CBOR test value: {type(value)!r}")


def _cbor_head(major: int, argument: int) -> bytes:
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
