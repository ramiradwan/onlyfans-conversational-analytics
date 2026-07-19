"""Server-derived local authentication context and same-origin CSRF tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.core.config import settings
from app.transport.manager import DEV_ACCOUNT_ID, DEV_PRINCIPAL_ID


@dataclass(frozen=True, slots=True)
class AuthContext:
    principal_id: str
    creator_account_id: str
    role: Literal["creator", "operator"]
    platform_creator_id: str | None = None
    session_id: str | None = None
    session_expires_at: int | None = None


def _development_context_allowed() -> bool:
    return (
        settings.websocket_auth_mode == "development_stub"
        and settings.environment.lower() in {"development", "dev", "local", "test"}
        and settings.websocket_bind_host in {"127.0.0.1", "localhost", "::1"}
    )


def _decode_signed_document(token: str, *, prefix: str) -> dict[str, object]:
    try:
        actual_prefix, encoded_payload, encoded_signature = token.split(".", 2)
        if actual_prefix != prefix:
            raise ValueError("prefix")
        payload = base64.urlsafe_b64decode(
            (encoded_payload + "=" * (-len(encoded_payload) % 4)).encode("ascii")
        )
        signature = base64.urlsafe_b64decode(
            (encoded_signature + "=" * (-len(encoded_signature) % 4)).encode("ascii")
        )
        secret = settings.security_signing_secret.get_secret_value().encode("utf-8")
        expected = hmac.new(secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ValueError("document")
        return document
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=401, detail="Authenticated session is invalid") from error


def local_session_token(
    context: AuthContext,
    *,
    issued_at: int | None = None,
) -> str:
    """Seal the exact locally provisioned account/role/platform binding.

    Provisioning/WebAuthn owns when this helper is called. The runtime never
    infers a platform identity from the Brain account identifier.
    """
    if not context.platform_creator_id or not context.session_id:
        raise ValueError("local sessions require platform_creator_id and session_id")
    issued = int(time.time()) if issued_at is None else issued_at
    document = {
        "account": context.creator_account_id,
        "exp": issued + settings.bridge_session_ttl_seconds,
        "iat": issued,
        "platform_creator": context.platform_creator_id,
        "principal": context.principal_id,
        "purpose": "bridge-session",
        "role": context.role,
        "session": context.session_id,
        "v": 1,
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    secret = settings.security_signing_secret.get_secret_value().encode("utf-8")
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"bridge-v1.{_base64(payload)}.{_base64(signature)}"


def get_auth_context(request: Request) -> AuthContext:
    """Resolve identity from trusted server state, never request account parameters."""
    token = request.cookies.get(settings.bridge_session_cookie_name)
    if token is None:
        if _development_context_allowed():
            return AuthContext(
                DEV_PRINCIPAL_ID,
                DEV_ACCOUNT_ID,
                "creator",
                settings.development_platform_creator_id,
                "development-session",
            )
        raise HTTPException(status_code=401, detail="Authenticated session is required")
    document = _decode_signed_document(token, prefix="bridge-v1")
    now = int(time.time())
    try:
        issued_at = int(document["iat"])
        expires_at = int(document["exp"])
        role = str(document["role"])
        if role not in {"creator", "operator"}:
            raise ValueError("role")
        context = AuthContext(
            principal_id=str(document["principal"]),
            creator_account_id=str(document["account"]),
            role=role,  # type: ignore[arg-type]
            platform_creator_id=str(document["platform_creator"]),
            session_id=str(document["session"]),
            session_expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="Authenticated session is invalid") from error
    if document != {
        "account": context.creator_account_id,
        "exp": expires_at,
        "iat": issued_at,
        "platform_creator": context.platform_creator_id,
        "principal": context.principal_id,
        "purpose": "bridge-session",
        "role": context.role,
        "session": context.session_id,
        "v": 1,
    }:
        raise HTTPException(status_code=401, detail="Authenticated session is invalid")
    if issued_at > now or expires_at <= now:
        raise HTTPException(status_code=401, detail="Authenticated session has expired")
    return context


def require_creator(context: AuthContext) -> None:
    if context.role != "creator":
        raise HTTPException(status_code=403, detail="Creator authority is required")


def verify_same_origin(request: Request) -> None:
    if _development_context_allowed():
        return
    expected = urlsplit(settings.bridge_origin)
    expected_origin = f"{expected.scheme}://{expected.netloc}"
    if (
        request.headers.get("host", "").lower() != expected.netloc.lower()
        or request.headers.get("origin") != expected_origin
    ):
        raise HTTPException(status_code=403, detail="Request origin is not authorized")


def _base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def csrf_token(context: AuthContext, *, issued_at: int | None = None) -> str:
    document = {
        "account": context.creator_account_id,
        "iat": int(time.time()) if issued_at is None else issued_at,
        "session": context.session_id,
        "principal": context.principal_id,
        "role": context.role,
        "v": 1,
    }
    payload = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    secret = settings.security_signing_secret.get_secret_value().encode("utf-8")
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_base64(payload)}.{_base64(signature)}"


def verify_csrf_token(context: AuthContext, token: str | None) -> None:
    if token is None:
        raise HTTPException(status_code=403, detail="CSRF token is required")
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = base64.urlsafe_b64decode(
            (encoded_payload + "=" * (-len(encoded_payload) % 4)).encode("ascii")
        )
        signature = base64.urlsafe_b64decode(
            (encoded_signature + "=" * (-len(encoded_signature) % 4)).encode("ascii")
        )
        secret = settings.security_signing_secret.get_secret_value().encode("utf-8")
        expected = hmac.new(secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        document = json.loads(payload)
        issued_at = int(document["iat"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as error:
        raise HTTPException(status_code=403, detail="CSRF token is invalid") from error
    if document != {
        "account": context.creator_account_id,
        "iat": issued_at,
        "session": context.session_id,
        "principal": context.principal_id,
        "role": context.role,
        "v": 1,
    }:
        raise HTTPException(status_code=403, detail="CSRF token does not match the session")
    age = int(time.time()) - issued_at
    if age < 0 or age > settings.csrf_token_ttl_seconds:
        raise HTTPException(status_code=403, detail="CSRF token has expired")
