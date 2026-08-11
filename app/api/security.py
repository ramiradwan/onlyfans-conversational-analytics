"""HTTP adapters for runtime-policy authentication and same-origin requests."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.core.config import settings
from app.security.local_sessions import (
    LocalSessionError,
    build_runtime_policy,
    issue_csrf_token,
    issue_local_session_token,
    verify_csrf_document,
    verify_local_session_token,
)
from app.security.runtime_policy import (
    AuthContext,
    RuntimeAuthorizationDenied,
    RuntimePolicy,
    require_role,
)
from app.transport.manager import DEV_ACCOUNT_ID, DEV_PRINCIPAL_ID


def _development_context_allowed() -> bool:
    return (
        settings.websocket_auth_mode == "development_stub"
        and settings.environment.lower() in {"development", "dev", "local", "test"}
        and settings.websocket_bind_host in {"127.0.0.1", "localhost", "::1"}
    )


def local_session_token(
    identity: AuthContext,
    *,
    issued_at: int | None = None,
) -> str:
    return issue_local_session_token(identity, issued_at=issued_at)


def get_runtime_policy(request: Request) -> RuntimePolicy:
    """Build one policy from verified session identity and current local state."""

    token = request.cookies.get(settings.bridge_session_cookie_name)
    if token is None:
        if _development_context_allowed():
            return build_runtime_policy(
                AuthContext(
                    DEV_PRINCIPAL_ID,
                    DEV_ACCOUNT_ID,
                    "creator",
                    settings.development_platform_creator_id,
                    "development-session",
                )
            )
        raise HTTPException(status_code=401, detail="Authenticated session is required")
    try:
        identity, digest = verify_local_session_token(token)
    except LocalSessionError as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    return build_runtime_policy(identity, signed_object_digests=(digest,))


def require_creator(policy: RuntimePolicy) -> None:
    try:
        require_role(policy, "creator")
    except RuntimeAuthorizationDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


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


def csrf_token(policy: RuntimePolicy, *, issued_at: int | None = None) -> str:
    return issue_csrf_token(policy, issued_at=issued_at)


def verify_csrf_token(policy: RuntimePolicy, token: str | None) -> None:
    try:
        verify_csrf_document(policy, token)
    except LocalSessionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
