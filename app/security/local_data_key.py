"""Current-user protection and derivation for local database keys.

Production installations are Windows-only.  One random master key is wrapped
with DPAPI for the current user; independent SQLCipher keys are then derived
for each database scope.  The only non-Windows path is an explicit test key,
which keeps CI deterministic without introducing a production plaintext
fallback.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import secrets
import sys
from ctypes import wintypes
from pathlib import Path
from threading import RLock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.persistence.private_files import (
    PrivateFileSecurityError,
    apply_private_file_security,
    sync_directory,
    sync_file,
)


MASTER_KEY_BYTES = 32
TEST_MASTER_KEY_ENVIRONMENT_VARIABLE = "OFCA_TEST_DATABASE_MASTER_KEY_HEX"
MASTER_KEY_FILENAME = ".ofca-master-key.dpapi"
_MASTER_KEY_MAGIC = b"OFCA-DPAPI-MASTER-1\n"
_PROTECTED_SECRET_MAGIC = b"OFCA-DPAPI-SECRET-1\n"
_TEST_PROTECTED_SECRET_MAGIC = b"OFCA-TEST-AESGCM-SECRET-1\n"
_DPAPI_UI_FORBIDDEN = 0x1
_LOCK = RLock()


class LocalDataKeyError(RuntimeError):
    """Raised when local key material cannot be protected or recovered."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def database_key(path: str | Path, scope: str) -> bytes:
    """Derive a stable 256-bit SQLCipher key for one closed database scope."""

    normalized_scope = scope.strip().lower()
    if not normalized_scope or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in normalized_scope
    ):
        raise LocalDataKeyError("database key scope is invalid")
    database_path = Path(path).expanduser().resolve()
    master = _test_master_key()
    if master is None:
        if sys.platform != "win32":
            raise LocalDataKeyError(
                "production local database encryption requires Windows DPAPI"
            )
        master = _load_or_create_master_key(database_path.parent)
    return _derive(master, f"database:{normalized_scope}".encode("ascii"))


def protect_local_secret(secret: bytes, *, purpose: str) -> bytes:
    """Protect a short local secret for the current OS user.

    This is used for encrypted-backup key sidecars.  Test mode uses AES-GCM
    under the explicit test master; production never takes that branch.
    """

    if not secret:
        raise LocalDataKeyError("local secret must not be empty")
    purpose_bytes = _purpose_bytes(purpose)
    test_master = _test_master_key()
    if test_master is not None:
        nonce = secrets.token_bytes(12)
        wrapping_key = _derive(test_master, b"test-secret:" + purpose_bytes)
        ciphertext = AESGCM(wrapping_key).encrypt(nonce, secret, purpose_bytes)
        return _TEST_PROTECTED_SECRET_MAGIC + base64.b64encode(nonce + ciphertext)
    if sys.platform != "win32":
        raise LocalDataKeyError("local secret protection requires Windows DPAPI")
    protected = _dpapi_protect(secret, entropy=_entropy(purpose_bytes))
    return _PROTECTED_SECRET_MAGIC + base64.b64encode(protected)


def unprotect_local_secret(payload: bytes, *, purpose: str) -> bytes:
    """Recover a secret created by :func:`protect_local_secret`."""

    purpose_bytes = _purpose_bytes(purpose)
    if payload.startswith(_TEST_PROTECTED_SECRET_MAGIC):
        test_master = _test_master_key()
        if test_master is None:
            raise LocalDataKeyError("test-protected secret refused outside test mode")
        raw = _decode_payload(payload, _TEST_PROTECTED_SECRET_MAGIC)
        if len(raw) <= 12:
            raise LocalDataKeyError("test-protected secret is malformed")
        wrapping_key = _derive(test_master, b"test-secret:" + purpose_bytes)
        try:
            return AESGCM(wrapping_key).decrypt(raw[:12], raw[12:], purpose_bytes)
        except Exception as error:
            raise LocalDataKeyError("test-protected secret could not be opened") from error
    if not payload.startswith(_PROTECTED_SECRET_MAGIC):
        raise LocalDataKeyError("protected secret format is unsupported")
    if sys.platform != "win32":
        raise LocalDataKeyError("DPAPI-protected secret requires Windows")
    return _dpapi_unprotect(
        _decode_payload(payload, _PROTECTED_SECRET_MAGIC),
        entropy=_entropy(purpose_bytes),
    )


def _load_or_create_master_key(directory: Path) -> bytes:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MASTER_KEY_FILENAME
    with _LOCK:
        if path.exists():
            return _read_master_key(path)
        master = secrets.token_bytes(MASTER_KEY_BYTES)
        protected = _dpapi_protect(master, entropy=_entropy(b"database-master"))
        payload = _MASTER_KEY_MAGIC + base64.b64encode(protected)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _read_master_key(path)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            apply_private_file_security(path)
            sync_file(path)
            sync_directory(directory)
        except (OSError, PrivateFileSecurityError) as error:
            path.unlink(missing_ok=True)
            raise LocalDataKeyError("database master key could not be persisted") from error
        return master


def _read_master_key(path: Path) -> bytes:
    try:
        apply_private_file_security(path)
        payload = path.read_bytes()
        protected = _decode_payload(payload, _MASTER_KEY_MAGIC)
        master = _dpapi_unprotect(protected, entropy=_entropy(b"database-master"))
    except (OSError, PrivateFileSecurityError) as error:
        raise LocalDataKeyError("database master key could not be read") from error
    if len(master) != MASTER_KEY_BYTES:
        raise LocalDataKeyError("database master key has an invalid length")
    return master


def _test_master_key() -> bytes | None:
    value = os.environ.get(TEST_MASTER_KEY_ENVIRONMENT_VARIABLE)
    if value is None:
        return None
    if os.environ.get("ENVIRONMENT", "").strip().lower() != "test":
        raise LocalDataKeyError("test database key is refused outside test mode")
    try:
        key = bytes.fromhex(value)
    except ValueError as error:
        raise LocalDataKeyError("test database key must be hexadecimal") from error
    if len(key) != MASTER_KEY_BYTES:
        raise LocalDataKeyError("test database key must contain 32 bytes")
    return key


def _derive(master: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=MASTER_KEY_BYTES,
        salt=b"ofca-local-data-key-v1",
        info=info,
    ).derive(master)


def _purpose_bytes(purpose: str) -> bytes:
    normalized = purpose.strip().lower().encode("ascii", errors="strict")
    if not normalized or len(normalized) > 128:
        raise LocalDataKeyError("local secret purpose is invalid")
    return normalized


def _entropy(purpose: bytes) -> bytes:
    return hashlib.sha256(b"ofca-dpapi-v1:" + purpose).digest()


def _decode_payload(payload: bytes, magic: bytes) -> bytes:
    try:
        decoded = base64.b64decode(payload.removeprefix(magic), validate=True)
    except ValueError as error:
        raise LocalDataKeyError("protected local key payload is malformed") from error
    if not decoded:
        raise LocalDataKeyError("protected local key payload is empty")
    return decoded


def _input_blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(
        len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    return blob, buffer


def _dpapi_protect(value: bytes, *, entropy: bytes) -> bytes:
    input_blob, input_buffer = _input_blob(value)
    entropy_blob, entropy_buffer = _input_blob(entropy)
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "Conversation analytics local data key",
        ctypes.byref(entropy_blob),
        None,
        None,
        _DPAPI_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        error = ctypes.get_last_error()
        raise LocalDataKeyError(f"DPAPI protection failed with Windows error {error}")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)
        del input_buffer, entropy_buffer


def _dpapi_unprotect(value: bytes, *, entropy: bytes) -> bytes:
    input_blob, input_buffer = _input_blob(value)
    entropy_blob, entropy_buffer = _input_blob(entropy)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        _DPAPI_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        error = ctypes.get_last_error()
        raise LocalDataKeyError(f"DPAPI recovery failed with Windows error {error}")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(output_blob.pbData)
        del input_buffer, entropy_buffer
