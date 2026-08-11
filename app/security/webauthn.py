"""WebAuthn registration and authentication for local Bridge sessions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import Settings, settings
from app.persistence.auth import (
    AuthenticationStateError,
    AuthenticationStore,
    BridgeSessionIssue,
    IssuedBridgeSession,
    WebAuthnChallengeBinding,
    WebAuthnCredential,
)


_CLIENT_DATA_LIMIT = 65_536
_AUTHENTICATOR_DATA_LIMIT = 16_384
_SIGNATURE_LIMIT = 256
_FLAG_USER_PRESENT = 0x01
_FLAG_USER_VERIFIED = 0x04
_FLAG_BACKUP_ELIGIBLE = 0x08
_FLAG_BACKUP_STATE = 0x10
_FLAG_ATTESTED_CREDENTIAL_DATA = 0x40
_FLAG_EXTENSION_DATA = 0x80


class WebAuthnVerificationError(ValueError):
    """Raised when a WebAuthn ceremony cannot be verified."""


@dataclass(frozen=True, slots=True)
class RegistrationAuthority:
    principal_id: str
    external_issuer: str
    external_subject: str
    installation_id: str


@dataclass(frozen=True, slots=True)
class SessionAuthority:
    principal_id: str
    credential_id: str
    creator_account_id: str
    role: Literal["creator", "operator"]
    grant_reference_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegistrationCredential:
    credential_id: str
    raw_id: str
    credential_type: str
    client_data_json: str
    attestation_object: str


@dataclass(frozen=True, slots=True)
class AuthenticationCredential:
    credential_id: str
    raw_id: str
    credential_type: str
    client_data_json: str
    authenticator_data: str
    signature: str
    user_handle: str | None = None


class WebAuthnService:
    """Issue and verify WebAuthn ceremonies against durable authentication state."""

    def __init__(
        self,
        store: AuthenticationStore,
        *,
        relying_party_id: str,
        expected_origin: str,
        relying_party_name: str,
        challenge_ttl_seconds: int,
        session_ttl_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not relying_party_id or not relying_party_name:
            raise ValueError("WebAuthn relying-party values must not be empty")
        if challenge_ttl_seconds <= 0 or session_ttl_seconds <= 0:
            raise ValueError("WebAuthn lifetimes must be positive")
        exact_origin = _exact_origin(expected_origin)
        origin_host = urlsplit(exact_origin).hostname or ""
        if origin_host != relying_party_id and not origin_host.endswith(
            f".{relying_party_id}"
        ):
            raise ValueError("WebAuthn relying party must contain the origin host")
        self._store = store
        self._relying_party_id = relying_party_id
        self._expected_origin = exact_origin
        self._relying_party_name = relying_party_name
        self._challenge_ttl_seconds = challenge_ttl_seconds
        self._session_ttl_seconds = session_ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def begin_registration(
        self, authority: RegistrationAuthority
    ) -> dict[str, object]:
        _require_registration_authority(authority)
        challenge = self._store.issue_webauthn_challenge(
            self._registration_binding(authority),
            expires_at=self._challenge_expiry(),
        )
        return {
            "challenge": challenge.value,
            "rp": {
                "id": self._relying_party_id,
                "name": self._relying_party_name,
            },
            "user": {
                "id": _base64url(_user_handle(authority.principal_id)),
                "name": authority.principal_id,
                "displayName": authority.principal_id,
            },
            "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
            "timeout": self._challenge_ttl_seconds * 1_000,
            "authenticatorSelection": {
                "authenticatorAttachment": "platform",
                "residentKey": "preferred",
                "userVerification": "required",
            },
            "attestation": "none",
        }

    def complete_registration(
        self,
        authority: RegistrationAuthority,
        response: RegistrationCredential,
    ) -> WebAuthnCredential:
        """Verify registration data and persist only the credential public key."""
        _require_registration_authority(authority)
        raw_credential_id = _credential_id(
            response.credential_id, response.raw_id, response.credential_type
        )
        _, challenge = _verify_client_data(
            response.client_data_json,
            expected_type="webauthn.create",
            expected_origin=self._expected_origin,
        )
        authenticator_data = _registration_authenticator_data(
            response.attestation_object,
            relying_party_id=self._relying_party_id,
            expected_credential_id=raw_credential_id,
        )
        if self._store.consume_webauthn_challenge(
            challenge, self._registration_binding(authority)
        ) is None:
            raise WebAuthnVerificationError("WebAuthn challenge is invalid or expired")
        credential = WebAuthnCredential(
            credential_id=_base64url(raw_credential_id),
            principal_id=authority.principal_id,
            external_issuer=authority.external_issuer,
            external_subject=authority.external_subject,
            installation_id=authority.installation_id,
            public_key=authenticator_data.public_key,
            signature_count=authenticator_data.signature_count,
            enrolled_at=self._now(),
        )
        try:
            self._store.register_webauthn_credential(credential)
        except AuthenticationStateError as error:
            raise WebAuthnVerificationError("WebAuthn credential cannot be registered") from error
        return credential

    def begin_authentication(
        self, authority: SessionAuthority
    ) -> dict[str, object]:
        _require_session_authority(authority)
        credential_id = _canonical_credential_id(authority.credential_id)
        challenge = self._store.issue_webauthn_challenge(
            self._authentication_binding(authority, credential_id),
            expires_at=self._challenge_expiry(),
        )
        return {
            "challenge": challenge.value,
            "rpId": self._relying_party_id,
            "allowCredentials": [
                {"type": "public-key", "id": credential_id}
            ],
            "timeout": self._challenge_ttl_seconds * 1_000,
            "userVerification": "required",
        }

    def complete_authentication(
        self,
        authority: SessionAuthority,
        response: AuthenticationCredential,
    ) -> IssuedBridgeSession:
        """Verify an assertion, advance its counter, and mint an opaque session."""
        _require_session_authority(authority)
        raw_credential_id = _credential_id(
            response.credential_id, response.raw_id, response.credential_type
        )
        credential_id = _base64url(raw_credential_id)
        if credential_id != _canonical_credential_id(authority.credential_id):
            raise WebAuthnVerificationError("WebAuthn credential does not match")
        credential = self._store.webauthn_credential(
            credential_id, principal_id=authority.principal_id
        )
        if credential is None:
            raise WebAuthnVerificationError("WebAuthn credential is not active")
        client_data, challenge = _verify_client_data(
            response.client_data_json,
            expected_type="webauthn.get",
            expected_origin=self._expected_origin,
        )
        user_handle = (
            None if response.user_handle is None else _base64url_decode(response.user_handle)
        )
        if user_handle is not None and not hmac.compare_digest(
            user_handle, _user_handle(authority.principal_id)
        ):
            raise WebAuthnVerificationError("WebAuthn user handle does not match")
        authenticator_data = _assertion_authenticator_data(
            response.authenticator_data,
            relying_party_id=self._relying_party_id,
        )
        signature = _base64url_decode(response.signature, maximum=_SIGNATURE_LIMIT)
        _verify_assertion_signature(
            credential.public_key,
            authenticator_data.raw,
            hashlib.sha256(client_data).digest(),
            signature,
        )
        binding = self._authentication_binding(authority, credential_id)
        if self._store.consume_webauthn_challenge(challenge, binding) is None:
            raise WebAuthnVerificationError("WebAuthn challenge is invalid or expired")
        if not self._store.advance_webauthn_signature_count(
            credential_id,
            principal_id=authority.principal_id,
            expected_count=credential.signature_count,
            asserted_count=authenticator_data.signature_count,
        ):
            raise WebAuthnVerificationError("WebAuthn signature counter did not advance")
        return self._store.issue_bridge_session(
            BridgeSessionIssue(
                credential_id=credential_id,
                principal_id=authority.principal_id,
                creator_account_id=authority.creator_account_id,
                role=authority.role,
                expires_at=self._now() + timedelta(seconds=self._session_ttl_seconds),
                grant_reference_ids=authority.grant_reference_ids,
            )
        )

    def _registration_binding(
        self, authority: RegistrationAuthority
    ) -> WebAuthnChallengeBinding:
        return WebAuthnChallengeBinding(
            principal_id=authority.principal_id,
            credential_id=None,
            relying_party_id=self._relying_party_id,
            expected_origin=self._expected_origin,
        )

    def _authentication_binding(
        self, authority: SessionAuthority, credential_id: str
    ) -> WebAuthnChallengeBinding:
        return WebAuthnChallengeBinding(
            principal_id=authority.principal_id,
            credential_id=credential_id,
            relying_party_id=self._relying_party_id,
            expected_origin=self._expected_origin,
        )

    def _challenge_expiry(self) -> datetime:
        return self._now() + timedelta(seconds=self._challenge_ttl_seconds)

    def _now(self) -> datetime:
        instant = self._clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("WebAuthn clock must return an aware datetime")
        return instant.astimezone(timezone.utc)


def configured_webauthn_service(
    store: AuthenticationStore,
    *,
    configuration: Settings = settings,
    clock: Callable[[], datetime] | None = None,
) -> WebAuthnService:
    """Build the local WebAuthn service from runtime configuration."""
    origin = _exact_origin(configuration.bridge_origin)
    relying_party_id = urlsplit(origin).hostname
    if relying_party_id is None:
        raise ValueError("Bridge origin must contain a host")
    return WebAuthnService(
        store,
        relying_party_id=relying_party_id,
        expected_origin=origin,
        relying_party_name=configuration.app_name,
        challenge_ttl_seconds=configuration.webauthn_challenge_ttl_seconds,
        session_ttl_seconds=configuration.bridge_session_ttl_seconds,
        clock=clock,
    )


@dataclass(frozen=True, slots=True)
class _RegistrationData:
    public_key: bytes
    signature_count: int


@dataclass(frozen=True, slots=True)
class _AssertionData:
    raw: bytes
    signature_count: int


def _verify_client_data(
    encoded: str, *, expected_type: str, expected_origin: str
) -> tuple[bytes, str]:
    raw = _base64url_decode(encoded, maximum=_CLIENT_DATA_LIMIT)

    def unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise WebAuthnVerificationError("WebAuthn client data is invalid")
            result[key] = value
        return result

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_members)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WebAuthnVerificationError("WebAuthn client data is invalid") from error
    if not isinstance(document, dict):
        raise WebAuthnVerificationError("WebAuthn client data is invalid")
    if document.get("type") != expected_type:
        raise WebAuthnVerificationError("WebAuthn client data type is invalid")
    if document.get("origin") != expected_origin:
        raise WebAuthnVerificationError("WebAuthn origin does not match")
    if "crossOrigin" in document and document["crossOrigin"] is not False:
        raise WebAuthnVerificationError("Cross-origin WebAuthn data is not accepted")
    challenge = document.get("challenge")
    if not isinstance(challenge, str):
        raise WebAuthnVerificationError("WebAuthn challenge is invalid")
    _base64url_decode(challenge, maximum=64)
    return raw, challenge


def _registration_authenticator_data(
    encoded_attestation: str,
    *,
    relying_party_id: str,
    expected_credential_id: bytes,
) -> _RegistrationData:
    attestation = _base64url_decode(
        encoded_attestation, maximum=_AUTHENTICATOR_DATA_LIMIT
    )
    decoder = _CborDecoder(attestation)
    value = decoder.decode()
    decoder.require_end()
    if (
        not isinstance(value, dict)
        or set(value) != {"fmt", "authData", "attStmt"}
        or value.get("fmt") != "none"
        or value.get("attStmt") != {}
        or not isinstance(value.get("authData"), bytes)
    ):
        raise WebAuthnVerificationError("WebAuthn attestation is invalid")
    authenticator_data = value["authData"]
    flags, signature_count = _authenticator_header(
        authenticator_data, relying_party_id=relying_party_id
    )
    if flags & _FLAG_ATTESTED_CREDENTIAL_DATA == 0:
        raise WebAuthnVerificationError("Attested credential data is required")
    if len(authenticator_data) < 55:
        raise WebAuthnVerificationError("Attested credential data is invalid")
    credential_id_length = int.from_bytes(authenticator_data[53:55], "big")
    credential_id_end = 55 + credential_id_length
    if credential_id_length == 0 or credential_id_end > len(authenticator_data):
        raise WebAuthnVerificationError("Attested credential ID is invalid")
    credential_id = authenticator_data[55:credential_id_end]
    if not hmac.compare_digest(credential_id, expected_credential_id):
        raise WebAuthnVerificationError("Attested credential ID does not match")
    key_decoder = _CborDecoder(authenticator_data, offset=credential_id_end)
    cose_key = key_decoder.decode()
    public_key = _decode_es256_cose_key(cose_key)
    _verify_extensions(authenticator_data, flags, key_decoder)
    return _RegistrationData(public_key, signature_count)


def _assertion_authenticator_data(
    encoded: str, *, relying_party_id: str
) -> _AssertionData:
    raw = _base64url_decode(encoded, maximum=_AUTHENTICATOR_DATA_LIMIT)
    flags, signature_count = _authenticator_header(raw, relying_party_id=relying_party_id)
    if flags & _FLAG_ATTESTED_CREDENTIAL_DATA:
        raise WebAuthnVerificationError("Assertion authenticator data is invalid")
    decoder = _CborDecoder(raw, offset=37)
    _verify_extensions(raw, flags, decoder)
    return _AssertionData(raw, signature_count)


def _authenticator_header(data: bytes, *, relying_party_id: str) -> tuple[int, int]:
    if len(data) < 37:
        raise WebAuthnVerificationError("WebAuthn authenticator data is invalid")
    expected_rp_hash = hashlib.sha256(relying_party_id.encode("utf-8")).digest()
    if not hmac.compare_digest(data[:32], expected_rp_hash):
        raise WebAuthnVerificationError("WebAuthn relying party does not match")
    flags = data[32]
    if flags & _FLAG_USER_PRESENT == 0:
        raise WebAuthnVerificationError("WebAuthn user presence is required")
    if flags & _FLAG_USER_VERIFIED == 0:
        raise WebAuthnVerificationError("WebAuthn user verification is required")
    if flags & _FLAG_BACKUP_STATE and flags & _FLAG_BACKUP_ELIGIBLE == 0:
        raise WebAuthnVerificationError("WebAuthn backup flags are invalid")
    return flags, int.from_bytes(data[33:37], "big")


def _verify_extensions(data: bytes, flags: int, decoder: "_CborDecoder") -> None:
    if flags & _FLAG_EXTENSION_DATA:
        decoder.decode()
    decoder.require_end()


def _decode_es256_cose_key(value: object) -> bytes:
    if not isinstance(value, dict):
        raise WebAuthnVerificationError("WebAuthn credential public key is invalid")
    if (
        value.get(1) != 2
        or value.get(3) != -7
        or value.get(-1) != 1
        or not isinstance(value.get(-2), bytes)
        or not isinstance(value.get(-3), bytes)
        or len(value[-2]) != 32
        or len(value[-3]) != 32
    ):
        raise WebAuthnVerificationError("WebAuthn credential must use ES256")
    try:
        public_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(value[-2], "big"),
            int.from_bytes(value[-3], "big"),
            ec.SECP256R1(),
        ).public_key()
    except ValueError as error:
        raise WebAuthnVerificationError("WebAuthn credential public key is invalid") from error
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _verify_assertion_signature(
    encoded_public_key: bytes,
    authenticator_data: bytes,
    client_data_hash: bytes,
    signature: bytes,
) -> None:
    try:
        public_key = serialization.load_der_public_key(encoded_public_key)
    except (TypeError, ValueError) as error:
        raise WebAuthnVerificationError("WebAuthn credential public key is invalid") from error
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise WebAuthnVerificationError("WebAuthn credential must use ES256")
    try:
        public_key.verify(
            signature,
            authenticator_data + client_data_hash,
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError) as error:
        raise WebAuthnVerificationError("WebAuthn assertion signature is invalid") from error


class _CborDecoder:
    """Decode the bounded definite-length CBOR subset emitted by authenticators."""

    def __init__(self, data: bytes, *, offset: int = 0) -> None:
        if len(data) > _AUTHENTICATOR_DATA_LIMIT or not 0 <= offset <= len(data):
            raise WebAuthnVerificationError("WebAuthn CBOR data is invalid")
        self._data = data
        self.offset = offset

    def decode(self, *, depth: int = 0) -> object:
        if depth > 8 or self.offset >= len(self._data):
            raise WebAuthnVerificationError("WebAuthn CBOR data is invalid")
        initial = self._read(1)[0]
        major = initial >> 5
        argument = self._argument(initial & 0x1F)
        if major == 0:
            return argument
        if major == 1:
            return -1 - argument
        if major == 2:
            return self._read(argument)
        if major == 3:
            try:
                return self._read(argument).decode("utf-8")
            except UnicodeError as error:
                raise WebAuthnVerificationError("WebAuthn CBOR data is invalid") from error
        if major == 4:
            if argument > 64:
                raise WebAuthnVerificationError("WebAuthn CBOR data is invalid")
            return [self.decode(depth=depth + 1) for _ in range(argument)]
        if major == 5:
            if argument > 64:
                raise WebAuthnVerificationError("WebAuthn CBOR data is invalid")
            result: dict[object, object] = {}
            for _ in range(argument):
                key = self.decode(depth=depth + 1)
                try:
                    if key in result:
                        raise WebAuthnVerificationError("WebAuthn CBOR data is invalid")
                    result[key] = self.decode(depth=depth + 1)
                except TypeError as error:
                    raise WebAuthnVerificationError("WebAuthn CBOR data is invalid") from error
            return result
        if major == 7 and argument in {20, 21, 22}:
            return {20: False, 21: True, 22: None}[argument]
        raise WebAuthnVerificationError("WebAuthn CBOR data is invalid")

    def require_end(self) -> None:
        if self.offset != len(self._data):
            raise WebAuthnVerificationError("WebAuthn CBOR data is invalid")

    def _argument(self, additional: int) -> int:
        if additional < 24:
            return additional
        widths = {24: 1, 25: 2, 26: 4, 27: 8}
        width = widths.get(additional)
        if width is None:
            raise WebAuthnVerificationError("WebAuthn CBOR data is invalid")
        value = int.from_bytes(self._read(width), "big")
        minimum = {1: 24, 2: 256, 4: 65_536, 8: 4_294_967_296}[width]
        if value < minimum:
            raise WebAuthnVerificationError("WebAuthn CBOR data is not canonical")
        return value

    def _read(self, length: int) -> bytes:
        end = self.offset + length
        if length < 0 or end > len(self._data):
            raise WebAuthnVerificationError("WebAuthn CBOR data is invalid")
        value = self._data[self.offset:end]
        self.offset = end
        return value


def _credential_id(credential_id: str, raw_id: str, credential_type: str) -> bytes:
    if credential_type != "public-key":
        raise WebAuthnVerificationError("WebAuthn credential type is invalid")
    decoded_id = _base64url_decode(credential_id, maximum=1_024)
    decoded_raw_id = _base64url_decode(raw_id, maximum=1_024)
    if not decoded_id or not hmac.compare_digest(decoded_id, decoded_raw_id):
        raise WebAuthnVerificationError("WebAuthn credential ID is invalid")
    return decoded_id


def _canonical_credential_id(value: str) -> str:
    decoded = _base64url_decode(value, maximum=1_024)
    if not decoded:
        raise WebAuthnVerificationError("WebAuthn credential ID is invalid")
    return _base64url(decoded)


def _base64url_decode(value: str, *, maximum: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise WebAuthnVerificationError("WebAuthn base64url value is invalid")
    if any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in value
    ):
        raise WebAuthnVerificationError("WebAuthn base64url value is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as error:
        raise WebAuthnVerificationError("WebAuthn base64url value is invalid") from error
    if _base64url(decoded) != value or (maximum is not None and len(decoded) > maximum):
        raise WebAuthnVerificationError("WebAuthn base64url value is invalid")
    return decoded


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _user_handle(principal_id: str) -> bytes:
    return hashlib.sha256(principal_id.encode("utf-8")).digest()


def _exact_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Bridge origin must be an exact HTTP origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _require_registration_authority(authority: RegistrationAuthority) -> None:
    if not all(
        (
            authority.principal_id,
            authority.external_issuer,
            authority.external_subject,
            authority.installation_id,
        )
    ):
        raise ValueError("WebAuthn registration authority must be complete")


def _require_session_authority(authority: SessionAuthority) -> None:
    if (
        not authority.principal_id
        or not authority.credential_id
        or not authority.creator_account_id
        or authority.role not in {"creator", "operator"}
        or not authority.grant_reference_ids
    ):
        raise ValueError("WebAuthn session authority must be complete")
    if len(set(authority.grant_reference_ids)) != len(authority.grant_reference_ids):
        raise ValueError("WebAuthn session grant references must be unique")
