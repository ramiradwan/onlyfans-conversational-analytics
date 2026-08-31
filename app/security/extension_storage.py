"""Device-bound bootstrap envelopes for Full-mode extension persistence.

The browser stores only the opaque bootstrap.  Its account credential is
protected with Windows DPAPI for the current user, while the stable account
storage key is independently derived from the same installation master.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.security.local_data_key import (
    LocalDataKeyError,
    database_key,
    protect_local_secret,
    unprotect_local_secret,
)


BOOTSTRAP_SCHEMA = "ofca-extension-storage-bootstrap/v1"
_BOOTSTRAP_PURPOSE = "extension-storage-bootstrap-v1"
_EXTENSION_ID = re.compile(r"[a-p]{32}")
_MAX_BOOTSTRAP_BYTES = 16 * 1024


@dataclass(frozen=True)
class ExtensionStorageBootstrap:
    extension_id: str
    creator_account_id: str
    credential_kind: Literal["pairing", "reconnect"]
    auth_ticket: str


def seal_extension_storage_bootstrap(
    *,
    extension_id: str,
    creator_account_id: str,
    credential_kind: Literal["pairing", "reconnect"],
    auth_ticket: str,
) -> str:
    """Seal one account credential for browser-restart bootstrap."""

    document = _validated_document(
        {
            "schema": BOOTSTRAP_SCHEMA,
            "extension_id": extension_id,
            "creator_account_id": creator_account_id,
            "credential_kind": credential_kind,
            "auth_ticket": auth_ticket,
        }
    )
    plaintext = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    protected = protect_local_secret(plaintext, purpose=_BOOTSTRAP_PURPOSE)
    return base64.urlsafe_b64encode(protected).rstrip(b"=").decode("ascii")


def open_extension_storage_bootstrap(
    bootstrap: str,
    *,
    expected_extension_id: str,
) -> ExtensionStorageBootstrap:
    """Open and strictly validate one device-bound browser bootstrap."""

    _validate_extension_id(expected_extension_id)
    if not isinstance(bootstrap, str) or not bootstrap or len(bootstrap) > _MAX_BOOTSTRAP_BYTES:
        raise LocalDataKeyError("extension storage bootstrap is invalid")
    try:
        raw = base64.b64decode(
            bootstrap.encode("ascii") + b"=" * (-len(bootstrap) % 4),
            altchars=b"-_",
            validate=True,
        )
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        if canonical != bootstrap:
            raise LocalDataKeyError("extension storage bootstrap encoding is noncanonical")
        plaintext = unprotect_local_secret(raw, purpose=_BOOTSTRAP_PURPOSE)
        candidate = json.loads(plaintext)
    except (UnicodeError, ValueError, json.JSONDecodeError, LocalDataKeyError) as error:
        raise LocalDataKeyError("extension storage bootstrap could not be opened") from error
    document = _validated_document(candidate)
    if document["extension_id"] != expected_extension_id:
        raise LocalDataKeyError("extension storage bootstrap identity does not match")
    return ExtensionStorageBootstrap(
        extension_id=document["extension_id"],
        creator_account_id=document["creator_account_id"],
        credential_kind=document["credential_kind"],
        auth_ticket=document["auth_ticket"],
    )


def extension_storage_key(
    master_path: str | Path,
    *,
    extension_id: str,
    creator_account_id: str,
) -> bytes:
    """Derive the stable AES-256 root for one extension/account partition."""

    _validate_extension_id(extension_id)
    _validate_nonempty(creator_account_id, "creator account")
    binding = hashlib.sha256(
        f"{extension_id}\0{creator_account_id}".encode("utf-8")
    ).hexdigest()
    return database_key(master_path, f"extension-storage-{binding}")


def extension_storage_key_base64(
    master_path: str | Path,
    *,
    extension_id: str,
    creator_account_id: str,
) -> str:
    return base64.b64encode(
        extension_storage_key(
            master_path,
            extension_id=extension_id,
            creator_account_id=creator_account_id,
        )
    ).decode("ascii")


def _validated_document(value: object) -> dict[str, str]:
    expected = {
        "schema",
        "extension_id",
        "creator_account_id",
        "credential_kind",
        "auth_ticket",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise LocalDataKeyError("extension storage bootstrap document is invalid")
    if value.get("schema") != BOOTSTRAP_SCHEMA:
        raise LocalDataKeyError("extension storage bootstrap schema is unsupported")
    extension_id = value.get("extension_id")
    creator_account_id = value.get("creator_account_id")
    credential_kind = value.get("credential_kind")
    auth_ticket = value.get("auth_ticket")
    _validate_extension_id(extension_id)
    _validate_nonempty(creator_account_id, "creator account")
    _validate_nonempty(auth_ticket, "Agent credential")
    if credential_kind not in {"pairing", "reconnect"}:
        raise LocalDataKeyError("extension storage credential kind is invalid")
    return {
        "schema": BOOTSTRAP_SCHEMA,
        "extension_id": extension_id,
        "creator_account_id": creator_account_id,
        "credential_kind": credential_kind,
        "auth_ticket": auth_ticket,
    }


def _validate_extension_id(value: object) -> None:
    if not isinstance(value, str) or _EXTENSION_ID.fullmatch(value) is None:
        raise LocalDataKeyError("Chrome extension identity is invalid")


def _validate_nonempty(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise LocalDataKeyError(f"{label} is invalid")
