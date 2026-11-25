"""Verify the two native Windows Hello purposes against exact local ceremony data."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from app.security.signed_objects import SignedObjectRejected, decode, encode, parse
from app.security.webauthn import (
    WebAuthnVerificationError,
    _CborDecoder,
    _assertion_authenticator_data,
    _decode_es256_cose_key,
    _registration_authenticator_data,
    _verify_assertion_signature,
)

ENROLLMENT_ORIGIN = "dipsy-desktop://passkey-enrollment"
SAVED_DATA_ORIGIN = "dipsy-desktop://saved-data"


@dataclass(frozen=True, slots=True, repr=False)
class NativeRegistration:
    credential_id: str
    client_data_json: str
    attestation_object: str

    def wire(self) -> dict[str, str]:
        return {
            "credential_id": self.credential_id,
            "client_data_json": self.client_data_json,
            "attestation_object": self.attestation_object,
        }


@dataclass(frozen=True, slots=True, repr=False)
class NativeAssertion:
    credential_id: str
    client_data_json: str
    authenticator_data: str
    signature: str
    user_handle: str | None = None


def _client_data(encoded: str, *, ceremony_type: str, origin: str, challenge: bytes) -> bytes:
    raw = decode(encoded, 3072)
    value = parse(raw, maximum=3072)
    if (
        len(challenge) != 32
        or not isinstance(value, dict)
        or set(value)
        != {
            "type",
            "challenge",
            "origin",
            "crossOrigin",
        }
    ):
        raise SignedObjectRejected()
    if (
        value["type"] != ceremony_type
        or value["origin"] != origin
        or value["crossOrigin"] is not False
        or value["challenge"] != encode(challenge)
    ):
        raise SignedObjectRejected()
    return raw


def verify_registration(
    registration: NativeRegistration,
    *,
    rp_id: str,
    challenge: bytes,
) -> bytes:
    """Return the public key after checking the exact native registration result."""
    try:
        credential_id = decode(registration.credential_id, 1023)
        _client_data(
            registration.client_data_json,
            ceremony_type="webauthn.create",
            origin=ENROLLMENT_ORIGIN,
            challenge=challenge,
        )
        decode(registration.attestation_object, 8192)
        return _registration_authenticator_data(
            registration.attestation_object,
            relying_party_id=rp_id,
            expected_credential_id=credential_id,
            verify_cose=_p256_cose,
        ).public_key
    except (SignedObjectRejected, WebAuthnVerificationError):
        raise WebAuthnVerificationError("Native account passkey registration is invalid") from None


def binding_public_key(cose: str) -> bytes:
    """Return the ES256 public key from a verified record's closed COSE key."""
    try:
        raw = decode(cose, 1024)
        decoder = _CborDecoder(raw)
        value = decoder.decode()
        decoder.require_end()
        return _p256_cose(value, raw)
    except (SignedObjectRejected, WebAuthnVerificationError):
        raise WebAuthnVerificationError("Account passkey binding key is invalid") from None


def _p256_cose(value: object, encoded: bytes) -> bytes:
    if not isinstance(value, dict) or set(value) != {1, 3, -1, -2, -3}:
        raise WebAuthnVerificationError("Account passkey COSE key is invalid")
    key = _decode_es256_cose_key(value)
    # Deterministic CBOR for the closed map {1: 2, 3: -7, -1: 1, -2: x, -3: y}.
    expected = b"\xa5\x01\x02\x03\x26\x20\x01\x21\x58\x20" + value[-2] + b"\x22\x58\x20" + value[-3]
    if not hmac.compare_digest(expected, encoded):
        raise WebAuthnVerificationError("Account passkey COSE encoding is invalid")
    return key


def verify_saved_data_assertion(
    assertion: NativeAssertion,
    *,
    rp_id: str,
    credential_id: str,
    public_key: bytes,
    challenge: bytes,
    expected_user_handle: str,
) -> None:
    """Verify a caller-owned one-use challenge for saved-data access."""
    try:
        if not hmac.compare_digest(
            decode(assertion.credential_id, 1023), decode(credential_id, 1023)
        ):
            raise SignedObjectRejected()
        if assertion.user_handle is not None:
            handle = decode(assertion.user_handle, 32)
            if len(handle) != 32 or not hmac.compare_digest(
                handle, decode(expected_user_handle, 32)
            ):
                raise SignedObjectRejected()
        client_data = _client_data(
            assertion.client_data_json,
            ceremony_type="webauthn.get",
            origin=SAVED_DATA_ORIGIN,
            challenge=challenge,
        )
        auth_data = _assertion_authenticator_data(
            assertion.authenticator_data, relying_party_id=rp_id
        )
        signature = decode(assertion.signature, 72)
        _verify_assertion_signature(
            public_key, auth_data.raw, hashlib.sha256(client_data).digest(), signature
        )
    except (SignedObjectRejected, WebAuthnVerificationError):
        raise WebAuthnVerificationError("Native saved-data passkey assertion is invalid") from None
