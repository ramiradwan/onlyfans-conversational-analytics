"""HTTP adapters for runtime-policy authentication and same-origin requests."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request

from app.core.config import settings
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.admission_confirmation import (
    AdmissionConfirmation,
    AdmissionConfirmationError,
    ConfirmationSubject,
    issue_admission_confirmation,
)
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
    require_identity,
    require_role,
)
from app.transport.manager import DEV_ACCOUNT_ID, DEV_PRINCIPAL_ID


_DURABLE_WEBAUTHN_SESSION_PREFIX = "webauthn-session:"


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


def webauthn_session_token(identity: AuthContext, session_value: str) -> str:
    """Seal a durable WebAuthn session without granting a platform identity."""

    if not session_value:
        raise ValueError("durable WebAuthn sessions require a session value")
    envelope = AuthContext(
        principal_id=identity.principal_id,
        creator_account_id=identity.creator_account_id,
        role=identity.role,
        # The legacy sealed envelope requires this claim. It is never returned
        # as WebAuthn authority; the durable policy intentionally leaves it unbound.
        platform_creator_id=identity.creator_account_id,
        session_id=f"{_DURABLE_WEBAUTHN_SESSION_PREFIX}{session_value}",
    )
    return issue_local_session_token(envelope)


def _allows_anonymous_reauthentication(request: Request) -> bool:
    path = request.url.path
    return path == "/" or path.startswith("/api/v1/webauthn/")


def _invalid_session_policy(request: Request, detail: str) -> RuntimePolicy:
    if _allows_anonymous_reauthentication(request):
        return build_runtime_policy()
    raise HTTPException(status_code=401, detail=detail)


def get_runtime_policy(request: Request) -> RuntimePolicy:
    """Build one policy from current durable state and an optional session identity."""

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
        return build_runtime_policy()
    try:
        identity, digest = verify_local_session_token(token)
    except LocalSessionError as error:
        return _invalid_session_policy(request, str(error))
    session_reference = identity.session_id
    if session_reference is not None and session_reference.startswith(
        _DURABLE_WEBAUTHN_SESSION_PREFIX
    ):
        session_value = session_reference.removeprefix(
            _DURABLE_WEBAUTHN_SESSION_PREFIX
        )
        active = SQLiteAuthenticationStore(
            settings.auth_database_path
        ).read_bridge_session(session_value)
        if active is None or active.policy.identity is None:
            return _invalid_session_policy(request, "Authenticated session is invalid")
        durable_identity = active.policy.identity
        if (
            identity.principal_id,
            identity.creator_account_id,
            identity.role,
        ) != (
            durable_identity.principal_id,
            durable_identity.creator_account_id,
            durable_identity.role,
        ):
            return _invalid_session_policy(request, "Authenticated session is invalid")
        return active.policy
    return build_runtime_policy(identity, signed_object_digests=(digest,))


def get_authenticated_runtime_policy(
    policy: RuntimePolicy = Depends(get_runtime_policy),
) -> RuntimePolicy:
    """Require the identity subset before account-scoped authorization."""

    try:
        require_identity(policy)
    except RuntimeAuthorizationDenied as error:
        raise HTTPException(status_code=401, detail=str(error)) from error
    return policy


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
    if policy.identity is None:
        raise HTTPException(status_code=403, detail="CSRF token does not match the session")
    try:
        verify_csrf_document(policy, token)
    except LocalSessionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


def confirm_admission(
    request: Request,
    csrf: str | None,
    subject: ConfirmationSubject,
) -> tuple[AdmissionConfirmation, str]:
    """Mint one confirmation for a same-origin, CSRF-checked session request."""

    policy = get_authenticated_runtime_policy(get_runtime_policy(request))
    require_creator(policy)
    verify_same_origin(request)
    verify_csrf_token(policy, csrf)
    try:
        return issue_admission_confirmation(policy, subject)
    except AdmissionConfirmationError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
