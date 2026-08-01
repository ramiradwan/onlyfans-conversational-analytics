"""TPM-backed installation signing key and proof of possession."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterator, Protocol
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.persistence.auth import (
    AuthenticationStore,
    InstallationKeyReference,
    InstallationKeyReservation,
)


PLATFORM_CRYPTO_PROVIDER = "Microsoft Platform Crypto Provider"
SOFTWARE_KEY_STORAGE_PROVIDER = "Microsoft Software Key Storage Provider"
INSTALLATION_KEY_ALGORITHM = "ECDSA_P256"
INSTALLATION_PROOF_ALGORITHM = "ES256"

_NCRYPT_EXPORT_POLICY_PROPERTY = "Export Policy"
_NCRYPT_IMPL_TYPE_PROPERTY = "Impl Type"
_NCRYPT_ALGORITHM_PROPERTY = "Algorithm Name"
_NCRYPT_KEY_USAGE_PROPERTY = "Key Usage"
_BCRYPT_ECCPUBLIC_BLOB = "ECCPUBLICBLOB"
_BCRYPT_ECDSA_PUBLIC_P256_MAGIC = 0x31534345
_NCRYPT_IMPL_HARDWARE_FLAG = 0x00000001
_NCRYPT_IMPL_SOFTWARE_FLAG = 0x00000002
_NCRYPT_ALLOW_SIGNING_FLAG = 0x00000002
_NCRYPT_PERSIST_FLAG = 0x80000000
_NCRYPT_SILENT_FLAG = 0x00000040
_NTE_EXISTS = 0x8009000F
_NTE_BAD_KEYSET = 0x80090016
_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551",
    16,
)


class InstallationKeyError(RuntimeError):
    """Base failure for installation-key startup and use."""


class InstallationKeyUnavailable(InstallationKeyError):
    """The required TPM-backed key provider or key cannot be used."""


class InstallationKeyPolicyError(InstallationKeyError):
    """A key does not meet the installation-key security policy."""


class InstallationKeyAlreadyExists(InstallationKeyError):
    """The requested persistent provider key name is already occupied."""


@dataclass(frozen=True, slots=True)
class ProviderKeyInfo:
    provider_name: str
    provider_key_name: str
    algorithm: str
    export_policy: int
    hardware_backed: bool
    x: bytes
    y: bytes


@dataclass(frozen=True, slots=True)
class InstallationProof:
    installation_key_id: str
    algorithm: str
    signature: bytes


class InstallationKeyProvider(Protocol):
    provider_name: str

    def key_info(self, provider_key_name: str) -> ProviderKeyInfo | None: ...

    def create_non_exportable_key(
        self, provider_key_name: str
    ) -> ProviderKeyInfo: ...

    def sign_digest(self, provider_key_name: str, digest: bytes) -> bytes: ...


class WindowsCNGInstallationKeyProvider:
    """CNG adapter fixed to the Microsoft TPM platform provider."""

    def __init__(self, provider_name: str = PLATFORM_CRYPTO_PROVIDER) -> None:
        if provider_name != PLATFORM_CRYPTO_PROVIDER:
            raise InstallationKeyPolicyError(
                "Installation keys require the TPM-backed platform provider"
            )
        if os.name != "nt":
            raise InstallationKeyUnavailable(
                "The TPM-backed platform provider is available only on Windows"
            )
        self.provider_name = provider_name
        self._api = _NCryptAPI()

    def key_info(self, provider_key_name: str) -> ProviderKeyInfo | None:
        with self._provider_handle() as provider:
            key = ctypes.c_size_t()
            status = self._api.open_key(
                provider,
                ctypes.byref(key),
                provider_key_name,
                0,
                _NCRYPT_SILENT_FLAG,
            )
            if _status_code(status) == _NTE_BAD_KEYSET:
                return None
            _require_success(status, "opening the persisted installation key")
            try:
                return self._describe_key(provider, key.value, provider_key_name)
            finally:
                self._api.free_object(key.value)

    def create_non_exportable_key(
        self, provider_key_name: str
    ) -> ProviderKeyInfo:
        with self._provider_handle() as provider:
            key = ctypes.c_size_t()
            status = self._api.create_persisted_key(
                provider,
                ctypes.byref(key),
                INSTALLATION_KEY_ALGORITHM,
                provider_key_name,
                0,
                0,
            )
            if _status_code(status) == _NTE_EXISTS:
                raise InstallationKeyAlreadyExists(
                    "The persisted installation key name is already occupied"
                )
            _require_success(status, "creating the persisted installation key")
            try:
                export_policy = ctypes.c_ulong(0)
                _require_success(
                    self._api.set_property(
                        key.value,
                        _NCRYPT_EXPORT_POLICY_PROPERTY,
                        ctypes.byref(export_policy),
                        ctypes.sizeof(export_policy),
                        _NCRYPT_PERSIST_FLAG,
                    ),
                    "setting the installation key export policy",
                )
                key_usage = ctypes.c_ulong(_NCRYPT_ALLOW_SIGNING_FLAG)
                _require_success(
                    self._api.set_property(
                        key.value,
                        _NCRYPT_KEY_USAGE_PROPERTY,
                        ctypes.byref(key_usage),
                        ctypes.sizeof(key_usage),
                        _NCRYPT_PERSIST_FLAG,
                    ),
                    "setting the installation key usage",
                )
                _require_success(
                    self._api.finalize_key(key.value, _NCRYPT_SILENT_FLAG),
                    "finalizing the installation key",
                )
                return self._describe_key(provider, key.value, provider_key_name)
            except Exception:
                if key.value:
                    delete_status = self._api.delete_key(key.value, 0)
                    if _status_code(delete_status) == 0:
                        key.value = 0
                raise
            finally:
                if key.value:
                    self._api.free_object(key.value)

    def sign_digest(self, provider_key_name: str, digest: bytes) -> bytes:
        if len(digest) != hashlib.sha256().digest_size:
            raise ValueError("ES256 signing requires a SHA-256 digest")
        with self._provider_handle() as provider:
            key = ctypes.c_size_t()
            _require_success(
                self._api.open_key(
                    provider,
                    ctypes.byref(key),
                    provider_key_name,
                    0,
                    _NCRYPT_SILENT_FLAG,
                ),
                "opening the persisted installation key for signing",
            )
            try:
                self._require_key_policy(
                    self._describe_key(provider, key.value, provider_key_name)
                )
                digest_buffer = ctypes.create_string_buffer(digest)
                size = ctypes.c_ulong()
                _require_success(
                    self._api.sign_hash(
                        key.value,
                        None,
                        digest_buffer,
                        len(digest),
                        None,
                        0,
                        ctypes.byref(size),
                        _NCRYPT_SILENT_FLAG,
                    ),
                    "measuring the installation proof signature",
                )
                signature = ctypes.create_string_buffer(size.value)
                _require_success(
                    self._api.sign_hash(
                        key.value,
                        None,
                        digest_buffer,
                        len(digest),
                        signature,
                        len(signature),
                        ctypes.byref(size),
                        _NCRYPT_SILENT_FLAG,
                    ),
                    "signing the installation proof",
                )
                return bytes(signature.raw[: size.value])
            finally:
                self._api.free_object(key.value)

    @contextmanager
    def _provider_handle(self) -> Iterator[int]:
        provider = ctypes.c_size_t()
        _require_success(
            self._api.open_storage_provider(
                ctypes.byref(provider), self.provider_name, 0
            ),
            "opening the TPM-backed platform provider",
        )
        try:
            implementation = _get_dword(
                self._api, provider.value, _NCRYPT_IMPL_TYPE_PROPERTY
            )
            hardware = bool(implementation & _NCRYPT_IMPL_HARDWARE_FLAG)
            software = bool(implementation & _NCRYPT_IMPL_SOFTWARE_FLAG)
            if not hardware or software:
                raise InstallationKeyPolicyError(
                    "The installation key provider is not hardware-backed"
                )
            yield provider.value
        finally:
            self._api.free_object(provider.value)

    def _describe_key(
        self, provider: int, key: int, provider_key_name: str
    ) -> ProviderKeyInfo:
        implementation = _get_dword(
            self._api, provider, _NCRYPT_IMPL_TYPE_PROPERTY
        )
        public_blob = _export_public_key(self._api, key)
        if len(public_blob) != 72:
            raise InstallationKeyPolicyError(
                "The installation key has an invalid P-256 public key"
            )
        magic, coordinate_size = struct.unpack_from("<II", public_blob)
        if magic != _BCRYPT_ECDSA_PUBLIC_P256_MAGIC or coordinate_size != 32:
            raise InstallationKeyPolicyError(
                "The installation key is not an ECDSA P-256 key"
            )
        algorithm = _get_text(self._api, key, _NCRYPT_ALGORITHM_PROPERTY)
        if algorithm not in {"ECDSA", INSTALLATION_KEY_ALGORITHM}:
            raise InstallationKeyPolicyError(
                "The installation key is not an ECDSA P-256 key"
            )
        return ProviderKeyInfo(
            provider_name=self.provider_name,
            provider_key_name=provider_key_name,
            algorithm=INSTALLATION_KEY_ALGORITHM,
            export_policy=_get_dword(
                self._api, key, _NCRYPT_EXPORT_POLICY_PROPERTY
            ),
            hardware_backed=bool(
                implementation & _NCRYPT_IMPL_HARDWARE_FLAG
                and not implementation & _NCRYPT_IMPL_SOFTWARE_FLAG
            ),
            x=public_blob[8:40],
            y=public_blob[40:72],
        )

    @staticmethod
    def _require_key_policy(info: ProviderKeyInfo) -> None:
        _require_provider_key_policy(info)


class InstallationKeyAuthority:
    """Coordinates one durable provider key reference with the auth store."""

    def __init__(
        self,
        store: AuthenticationStore,
        provider: InstallationKeyProvider,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if provider.provider_name != PLATFORM_CRYPTO_PROVIDER:
            raise InstallationKeyPolicyError(
                "Installation keys require the TPM-backed platform provider"
            )
        self._store = store
        self._provider = provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def ensure_ready(self) -> InstallationKeyReference:
        active = self._store.installation_key_reference()
        reservation = self._store.installation_key_reservation()
        if reservation is None:
            reservation = self._store.reserve_installation_key(
                InstallationKeyReservation(
                    provider_name=PLATFORM_CRYPTO_PROVIDER,
                    provider_key_name=f"bridge-clean.installation.v1.{uuid4()}",
                    algorithm=INSTALLATION_KEY_ALGORITHM,
                    created_at=self._now(),
                )
            )
        self._require_reservation_policy(reservation)

        info = self._provider.key_info(reservation.provider_key_name)
        if info is None:
            if active is not None:
                raise InstallationKeyUnavailable(
                    "The persisted installation key is no longer available"
                )
            try:
                info = self._provider.create_non_exportable_key(
                    reservation.provider_key_name
                )
            except InstallationKeyAlreadyExists:
                info = self._provider.key_info(reservation.provider_key_name)
                if info is None:
                    raise InstallationKeyUnavailable(
                        "The persisted installation key cannot be reopened"
                    ) from None

        _require_provider_key_policy(info)
        activated_at = active.activated_at if active is not None else self._now()
        reference = _installation_key_reference(reservation, info, activated_at)
        if active is not None and active != reference:
            raise InstallationKeyPolicyError(
                "The persisted installation key does not match its durable reference"
            )

        self._prove_usable(reference)
        if active is None:
            self._store.activate_installation_key(reference)
        return reference

    def sign_challenge(self, challenge: bytes) -> InstallationProof:
        if not isinstance(challenge, bytes):
            raise TypeError("Installation proof challenge must be bytes")
        reference = self._store.installation_key_reference()
        if reference is None:
            raise InstallationKeyUnavailable("The installation key is not active")
        info = self._provider.key_info(reference.provider_key_name)
        if info is None:
            raise InstallationKeyUnavailable(
                "The persisted installation key is no longer available"
            )
        _require_provider_key_policy(info)
        current = _installation_key_reference(
            InstallationKeyReservation(
                reference.provider_name,
                reference.provider_key_name,
                reference.algorithm,
                reference.created_at,
            ),
            info,
            reference.activated_at,
        )
        if current != reference:
            raise InstallationKeyPolicyError(
                "The persisted installation key does not match its durable reference"
            )
        signature = self._provider.sign_digest(
            reference.provider_key_name, hashlib.sha256(challenge).digest()
        )
        return InstallationProof(
            reference.installation_key_id,
            INSTALLATION_PROOF_ALGORITHM,
            _canonical_es256_signature(signature),
        )

    def _prove_usable(self, reference: InstallationKeyReference) -> None:
        challenge = secrets.token_bytes(32)
        signature = self._provider.sign_digest(
            reference.provider_key_name, hashlib.sha256(challenge).digest()
        )
        proof = InstallationProof(
            reference.installation_key_id,
            INSTALLATION_PROOF_ALGORITHM,
            _canonical_es256_signature(signature),
        )
        if not verify_installation_proof(reference.public_key_jwk, challenge, proof):
            raise InstallationKeyUnavailable(
                "The persisted installation key failed its proof check"
            )

    @staticmethod
    def _require_reservation_policy(
        reservation: InstallationKeyReservation,
    ) -> None:
        if (
            reservation.provider_name != PLATFORM_CRYPTO_PROVIDER
            or reservation.algorithm != INSTALLATION_KEY_ALGORITHM
        ):
            raise InstallationKeyPolicyError(
                "The installation key reservation violates provider policy"
            )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Installation key timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)


def verify_installation_proof(
    public_key_jwk: str, challenge: bytes, proof: InstallationProof
) -> bool:
    """Verify a provider-created ES256 proof using public key data only."""
    try:
        jwk = json.loads(public_key_jwk)
        if (
            not isinstance(jwk, dict)
            or set(jwk) != {"crv", "kid", "kty", "x", "y"}
            or jwk["crv"] != "P-256"
            or jwk["kty"] != "EC"
            or jwk["kid"] != proof.installation_key_id
            or proof.algorithm != INSTALLATION_PROOF_ALGORITHM
            or len(proof.signature) != 64
        ):
            return False
        x = int.from_bytes(_base64url_decode(jwk["x"]), "big")
        y = int.from_bytes(_base64url_decode(jwk["y"]), "big")
        public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        r = int.from_bytes(proof.signature[:32], "big")
        s = int.from_bytes(proof.signature[32:], "big")
        public_key.verify(
            encode_dss_signature(r, s), challenge, ec.ECDSA(hashes.SHA256())
        )
        return True
    except (InvalidSignature, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return False


def _require_provider_key_policy(info: ProviderKeyInfo) -> None:
    if info.provider_name != PLATFORM_CRYPTO_PROVIDER or not info.hardware_backed:
        raise InstallationKeyPolicyError(
            "The installation key is not TPM-backed"
        )
    if info.algorithm != INSTALLATION_KEY_ALGORITHM:
        raise InstallationKeyPolicyError(
            "The installation key is not an ECDSA P-256 key"
        )
    if info.export_policy != 0:
        raise InstallationKeyPolicyError(
            "The installation key permits private-key export"
        )
    if len(info.x) != 32 or len(info.y) != 32:
        raise InstallationKeyPolicyError(
            "The installation key has invalid public coordinates"
        )


def _installation_key_reference(
    reservation: InstallationKeyReservation,
    info: ProviderKeyInfo,
    activated_at: datetime,
) -> InstallationKeyReference:
    public_members = {
        "crv": "P-256",
        "kty": "EC",
        "x": _base64url(info.x),
        "y": _base64url(info.y),
    }
    canonical_public = json.dumps(
        public_members, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    thumbprint_bytes = hashlib.sha256(canonical_public).digest()
    installation_key_id = f"ik1.{_base64url(thumbprint_bytes[:16])}"
    public_jwk = json.dumps(
        {**public_members, "kid": installation_key_id},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return InstallationKeyReference(
        provider_name=reservation.provider_name,
        provider_key_name=reservation.provider_key_name,
        algorithm=reservation.algorithm,
        installation_key_id=installation_key_id,
        installation_key_jkt=_base64url(thumbprint_bytes),
        public_key_jwk=public_jwk,
        created_at=reservation.created_at,
        activated_at=activated_at,
    )


def _canonical_es256_signature(signature: bytes) -> bytes:
    if len(signature) != 64:
        raise InstallationKeyUnavailable(
            "The installation key returned an invalid ES256 signature"
        )
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not 0 < r < _P256_ORDER or not 0 < s < _P256_ORDER:
        raise InstallationKeyUnavailable(
            "The installation key returned an invalid ES256 signature"
        )
    return r.to_bytes(32, "big") + min(s, _P256_ORDER - s).to_bytes(32, "big")


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: object) -> bytes:
    if not isinstance(value, str):
        raise TypeError("JWK coordinate must be a string")
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


class _NCryptAPI:
    def __init__(self) -> None:
        library = ctypes.WinDLL("ncrypt.dll")
        handle = ctypes.c_size_t
        dword = ctypes.c_ulong
        status = ctypes.c_long

        self.open_storage_provider = library.NCryptOpenStorageProvider
        self.open_storage_provider.argtypes = [
            ctypes.POINTER(handle),
            ctypes.c_wchar_p,
            dword,
        ]
        self.open_storage_provider.restype = status

        self.open_key = library.NCryptOpenKey
        self.open_key.argtypes = [
            handle,
            ctypes.POINTER(handle),
            ctypes.c_wchar_p,
            dword,
            dword,
        ]
        self.open_key.restype = status

        self.create_persisted_key = library.NCryptCreatePersistedKey
        self.create_persisted_key.argtypes = [
            handle,
            ctypes.POINTER(handle),
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            dword,
            dword,
        ]
        self.create_persisted_key.restype = status

        self.set_property = library.NCryptSetProperty
        self.set_property.argtypes = [
            handle,
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            dword,
            dword,
        ]
        self.set_property.restype = status

        self.get_property = library.NCryptGetProperty
        self.get_property.argtypes = [
            handle,
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            dword,
            ctypes.POINTER(dword),
            dword,
        ]
        self.get_property.restype = status

        self.finalize_key = library.NCryptFinalizeKey
        self.finalize_key.argtypes = [handle, dword]
        self.finalize_key.restype = status

        self.export_key = library.NCryptExportKey
        self.export_key.argtypes = [
            handle,
            handle,
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            dword,
            ctypes.POINTER(dword),
            dword,
        ]
        self.export_key.restype = status

        self.sign_hash = library.NCryptSignHash
        self.sign_hash.argtypes = [
            handle,
            ctypes.c_void_p,
            ctypes.c_void_p,
            dword,
            ctypes.c_void_p,
            dword,
            ctypes.POINTER(dword),
            dword,
        ]
        self.sign_hash.restype = status

        self.delete_key = library.NCryptDeleteKey
        self.delete_key.argtypes = [handle, dword]
        self.delete_key.restype = status

        self.free_object = library.NCryptFreeObject
        self.free_object.argtypes = [handle]
        self.free_object.restype = status


def _get_property(api: _NCryptAPI, handle: int, name: str) -> bytes:
    size = ctypes.c_ulong()
    _require_success(
        api.get_property(handle, name, None, 0, ctypes.byref(size), 0),
        "reading an installation key property",
    )
    output = ctypes.create_string_buffer(size.value)
    _require_success(
        api.get_property(
            handle, name, output, len(output), ctypes.byref(size), 0
        ),
        "reading an installation key property",
    )
    return bytes(output.raw[: size.value])


def _get_dword(api: _NCryptAPI, handle: int, name: str) -> int:
    value = _get_property(api, handle, name)
    if len(value) != 4:
        raise InstallationKeyPolicyError(
            "The installation key provider returned an invalid property"
        )
    return struct.unpack("<I", value)[0]


def _get_text(api: _NCryptAPI, handle: int, name: str) -> str:
    value = _get_property(api, handle, name)
    try:
        return value.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError as error:
        raise InstallationKeyPolicyError(
            "The installation key provider returned an invalid property"
        ) from error


def _export_public_key(api: _NCryptAPI, key: int) -> bytes:
    size = ctypes.c_ulong()
    _require_success(
        api.export_key(
            key,
            0,
            _BCRYPT_ECCPUBLIC_BLOB,
            None,
            None,
            0,
            ctypes.byref(size),
            0,
        ),
        "measuring the installation public key",
    )
    output = ctypes.create_string_buffer(size.value)
    _require_success(
        api.export_key(
            key,
            0,
            _BCRYPT_ECCPUBLIC_BLOB,
            None,
            output,
            len(output),
            ctypes.byref(size),
            0,
        ),
        "reading the installation public key",
    )
    return bytes(output.raw[: size.value])


def _status_code(status: int) -> int:
    return int(status) & 0xFFFFFFFF


def _require_success(status: int, operation: str) -> None:
    code = _status_code(status)
    if code != 0:
        raise InstallationKeyUnavailable(f"CNG failed while {operation} (0x{code:08X})")
