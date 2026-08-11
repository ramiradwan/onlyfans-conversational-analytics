"""Local session verification and runtime-policy construction."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.runtime_policy import AuthContext, RuntimePolicy


class LocalSessionError(ValueError):
    """Raised when a local session or CSRF document is invalid."""


def _base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64(value: str) -> bytes:
    return base64.urlsafe_b64decode(
        (value + "=" * (-len(value) % 4)).encode("ascii")
    )


def _signing_secret() -> bytes:
    return settings.security_signing_secret.get_secret_value().encode("utf-8")


def issue_local_session_token(
    identity: AuthContext,
    *,
    issued_at: int | None = None,
) -> str:
    if not identity.platform_creator_id or not identity.session_id:
        raise ValueError("local sessions require platform_creator_id and session_id")
    issued = int(time.time()) if issued_at is None else issued_at
    document = {
        "account": identity.creator_account_id,
        "exp": issued + settings.bridge_session_ttl_seconds,
        "iat": issued,
        "platform_creator": identity.platform_creator_id,
        "principal": identity.principal_id,
        "purpose": "bridge-session",
        "role": identity.role,
        "session": identity.session_id,
        "v": 1,
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_signing_secret(), payload, hashlib.sha256).digest()
    return f"bridge-v1.{_base64(payload)}.{_base64(signature)}"


def verify_local_session_token(
    token: str, *, now: int | None = None
) -> tuple[AuthContext, str]:
    try:
        prefix, encoded_payload, encoded_signature = token.split(".", 2)
        if prefix != "bridge-v1":
            raise ValueError("prefix")
        payload = _decode_base64(encoded_payload)
        signature = _decode_base64(encoded_signature)
        expected = hmac.new(_signing_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ValueError("document")
        issued_at = int(document["iat"])
        expires_at = int(document["exp"])
        role = str(document["role"])
        if role not in {"creator", "operator"}:
            raise ValueError("role")
        identity = AuthContext(
            principal_id=str(document["principal"]),
            creator_account_id=str(document["account"]),
            role=role,  # type: ignore[arg-type]
            platform_creator_id=str(document["platform_creator"]),
            session_id=str(document["session"]),
            session_expires_at=expires_at,
        )
    except (ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise LocalSessionError("Authenticated session is invalid") from error
    if document != {
        "account": identity.creator_account_id,
        "exp": expires_at,
        "iat": issued_at,
        "platform_creator": identity.platform_creator_id,
        "principal": identity.principal_id,
        "purpose": "bridge-session",
        "role": identity.role,
        "session": identity.session_id,
        "v": 1,
    }:
        raise LocalSessionError("Authenticated session is invalid")
    instant = int(time.time()) if now is None else now
    if issued_at > instant or expires_at <= instant:
        raise LocalSessionError("Authenticated session has expired")
    return identity, hashlib.sha256(token.encode("ascii")).hexdigest()


@lru_cache(maxsize=4)
def _store(path: str) -> SQLiteAuthenticationStore:
    return SQLiteAuthenticationStore(Path(path))


def build_runtime_policy(
    identity: AuthContext,
    *,
    signed_object_digests: tuple[str, ...] = (),
) -> RuntimePolicy:
    return _store(str(settings.auth_database_path.resolve())).build_runtime_policy(
        identity,
        signed_object_digests=signed_object_digests,
    )


def sign_local_document(document: Mapping[str, Any]) -> str:
    """Return one signed token carrying a local document."""

    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_signing_secret(), payload, hashlib.sha256).digest()
    return f"{_base64(payload)}.{_base64(signature)}"


def open_local_document(token: str | None) -> dict[str, Any]:
    """Return the document carried by one signed local token."""

    if not isinstance(token, str):
        raise LocalSessionError("Signed document is required")
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _decode_base64(encoded_payload)
        signature = _decode_base64(encoded_signature)
        expected = hmac.new(_signing_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ValueError("document")
    except (ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise LocalSessionError("Signed document is invalid") from error
    return document


def issue_csrf_token(policy: RuntimePolicy, *, issued_at: int | None = None) -> str:
    identity = policy.identity
    document = {
        "account": identity.creator_account_id,
        "iat": int(time.time()) if issued_at is None else issued_at,
        "session": identity.session_id,
        "principal": identity.principal_id,
        "role": identity.role,
        "v": 1,
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_signing_secret(), payload, hashlib.sha256).digest()
    return f"{_base64(payload)}.{_base64(signature)}"


def verify_csrf_document(
    policy: RuntimePolicy,
    token: str | None,
    *,
    now: int | None = None,
) -> None:
    if token is None:
        raise LocalSessionError("CSRF token is required")
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _decode_base64(encoded_payload)
        signature = _decode_base64(encoded_signature)
        expected = hmac.new(_signing_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        document = json.loads(payload)
        issued_at = int(document["iat"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as error:
        raise LocalSessionError("CSRF token is invalid") from error
    identity = policy.identity
    if document != {
        "account": identity.creator_account_id,
        "iat": issued_at,
        "session": identity.session_id,
        "principal": identity.principal_id,
        "role": identity.role,
        "v": 1,
    }:
        raise LocalSessionError("CSRF token does not match the session")
    instant = int(time.time()) if now is None else now
    age = instant - issued_at
    if age < 0 or age > settings.csrf_token_ttl_seconds:
        raise LocalSessionError("CSRF token has expired")
