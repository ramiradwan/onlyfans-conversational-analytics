"""Activation-gated HTTP adapter for local WebAuthn ceremonies."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Annotated, Literal, NoReturn, TypeVar
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.activation import require_activated_runtime
from app.api.security import (
    csrf_token,
    get_runtime_policy,
    verify_csrf_token,
    verify_same_origin,
    webauthn_session_token,
)
from app.core.config import settings
from app.onboarding.admission import AdmissionRefused
from app.onboarding.admission_routes import GENERATION, authorize_desktop_passkey
from app.persistence.auth import AuthenticationStateError, SQLiteAuthenticationStore
from app.security.account_passkey_windows import WindowsHelloRefused
from app.security.signed_objects import SignedObjectRejected
from app.security.grant_refresh import renewal_runs
from app.security.runtime_policy import AuthContext, RuntimePolicy
from app.security.webauthn import (
    AuthenticationCredential,
    RegistrationAuthority,
    RegistrationCredential,
    SessionAuthority,
    WebAuthnAccessState,
    WebAuthnAuthorityDecision,
    WebAuthnAuthorityPort,
    WebAuthnAuthorityResult,
    WebAuthnService,
    WebAuthnVerificationError,
    configured_webauthn_authority_port,
    configured_webauthn_service,
)


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/webauthn",
    tags=["WebAuthn"],
    dependencies=[Depends(require_activated_runtime)],
)

BoundedWebAuthnValue = Annotated[str, Field(min_length=1, max_length=100_000)]
CredentialIdentifier = Annotated[str, Field(min_length=1, max_length=2_048)]
_MAX_FINISH_BODY_BYTES = 410_000


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RegistrationResponse(_StrictModel):
    client_data_json: BoundedWebAuthnValue = Field(alias="clientDataJSON")
    attestation_object: BoundedWebAuthnValue = Field(alias="attestationObject")


class RegistrationFinishRequest(_StrictModel):
    credential_id: CredentialIdentifier = Field(alias="id")
    raw_id: CredentialIdentifier = Field(alias="rawId")
    credential_type: Literal["public-key"] = Field(alias="type")
    response: RegistrationResponse


class AuthenticationResponse(_StrictModel):
    client_data_json: BoundedWebAuthnValue = Field(alias="clientDataJSON")
    authenticator_data: BoundedWebAuthnValue = Field(alias="authenticatorData")
    signature: BoundedWebAuthnValue
    user_handle: BoundedWebAuthnValue | None = Field(None, alias="userHandle")


class LoginFinishRequest(_StrictModel):
    credential_id: CredentialIdentifier = Field(alias="id")
    raw_id: CredentialIdentifier = Field(alias="rawId")
    credential_type: Literal["public-key"] = Field(alias="type")
    response: AuthenticationResponse

    def assertion(self) -> AuthenticationCredential:
        return AuthenticationCredential(
            credential_id=self.credential_id, raw_id=self.raw_id, credential_type=self.credential_type,
            client_data_json=self.response.client_data_json, authenticator_data=self.response.authenticator_data,
            signature=self.response.signature, user_handle=self.response.user_handle,
        )


FinishRequest = TypeVar("FinishRequest", bound=_StrictModel)


async def _validated_finish_body(
    request: Request,
    model: type[FinishRequest],
) -> FinishRequest:
    raw = await request.body()
    if len(raw) > _MAX_FINISH_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail="WebAuthn request is too large",
            headers={"Cache-Control": "no-store"},
        )
    try:
        document = json.loads(raw)
        return model.model_validate(document)
    except (json.JSONDecodeError, UnicodeError, ValidationError) as error:
        raise HTTPException(
            status_code=422,
            detail="WebAuthn request is invalid",
            headers={"Cache-Control": "no-store"},
        ) from error


def authentication_store() -> SQLiteAuthenticationStore:
    """Open the configured durable authentication adapter."""

    return SQLiteAuthenticationStore(settings.auth_database_path)


def webauthn_service(
    store: SQLiteAuthenticationStore = Depends(authentication_store),
) -> WebAuthnService:
    from app.onboarding.browser_tasks import saved_creator_label

    def label(creator_id):
        value = saved_creator_label(store, creator_id)
        if value is None:
            return None
        return value["display_name"] or "@" + value["username"]

    return configured_webauthn_service(store, account_label=label)


def webauthn_authority_port(
    store: SQLiteAuthenticationStore = Depends(authentication_store),
) -> WebAuthnAuthorityPort:
    return configured_webauthn_authority_port(store)


def _prepare_request(
    request: Request,
) -> None:
    verify_same_origin(request)


def _authority_or_refuse(
    decision: WebAuthnAuthorityDecision,
    *,
    operation: str,
) -> RegistrationAuthority | SessionAuthority:
    if (
        decision.result is not WebAuthnAuthorityResult.AUTHORIZED
        or decision.authority is None
    ):
        logger.warning(
            "webauthn_http_event operation=%s result=authority_refused reason=%s",
            operation,
            decision.result.value,
        )
        raise HTTPException(
            status_code=403,
            detail="WebAuthn is not available for this session",
            headers={"Cache-Control": "no-store"},
        )
    return decision.authority


def _verification_refusal(
    error: WebAuthnVerificationError, *, operation: str
) -> NoReturn:
    logger.warning(
        "webauthn_http_event operation=%s result=verification_refused reason=%s",
        operation,
        str(error),
    )
    raise HTTPException(
        status_code=400,
        detail="WebAuthn ceremony could not be completed",
        headers={"Cache-Control": "no-store"},
    ) from error


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _require_same_origin_read(request: Request) -> None:
    # Same-origin fetch metadata and exact host; GET does not require an Origin
    # header, which browsers commonly omit for these same-origin reads.
    if (request.headers.get("host") != urlsplit(settings.bridge_origin).netloc
            or request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}):
        raise HTTPException(403, "Request origin is not authorized")


def _session_access(
    policy: RuntimePolicy, authority_port: WebAuthnAuthorityPort
) -> tuple[WebAuthnAuthorityDecision, WebAuthnAccessState | None]:
    decision, access = authority_port.session_access(policy)
    if access is WebAuthnAccessState.RENEWING and not renewal_runs(settings):
        access = WebAuthnAccessState.UNAVAILABLE
    return decision, access


def runtime_policy_reader(request: Request) -> Callable[[], RuntimePolicy]:
    """Derive the request's policy again for each read of a long response."""

    return lambda: get_runtime_policy(request)


_ACCESS_STREAM_LIMIT = 8
_KEEPALIVE_SECONDS = 15.0
# Wake just after a validity boundary, so the read sees the grant past it.
_BOUNDARY_MARGIN_SECONDS = 0.05


class AccessStreams:
    """The open access streams of one application. Closing ends every one."""

    def __init__(self) -> None:
        self.active: set[AccessEventResponse] = set()
        self.closed = False

    def close(self) -> None:
        from app.provisioning.events import events

        self.closed = True
        events.publish()


def access_streams(application) -> AccessStreams:
    streams = getattr(application.state, "webauthn_access_streams", None)
    if streams is None:
        streams = application.state.webauthn_access_streams = AccessStreams()
    return streams


class AccessEventResponse(StreamingResponse):
    """Sends the access value again after each authority commit or validity boundary.

    The response owns its subscription, even if no body is ever consumed.
    """

    def __init__(self, streams: AccessStreams, read: Callable[[], str],
                 next_boundary: Callable[[], float | None]) -> None:
        self.streams, self.read, self.next_boundary = streams, read, next_boundary
        super().__init__(self.frames(), media_type="text/event-stream",
                         headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.listener = None

    async def __call__(self, scope, receive, send) -> None:
        from app.provisioning.events import events

        if self.streams.closed or len(self.streams.active) >= _ACCESS_STREAM_LIMIT:
            await JSONResponse({"detail": "Access updates are unavailable"},
                               status_code=503 if self.streams.closed else 429,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        self.streams.active.add(self)
        self.listener = events.subscribe()
        try:
            await super().__call__(scope, receive, send)
        finally:
            events.unsubscribe(self.listener)
            self.streams.active.discard(self)

    async def frames(self):
        signal = self.listener[1]
        previous = None
        while not self.streams.closed:
            # Clear before the read. A commit during the read or the send stays
            # set for the next iteration.
            signal.clear()
            try:
                value = await asyncio.to_thread(self.read)
                boundary = await asyncio.to_thread(self.next_boundary)
            except HTTPException:
                return
            if value != previous:
                yield "event: access\ndata: " + json.dumps({"access": value}, separators=(",", ":")) + "\n\n"
                previous = value
            wait = _KEEPALIVE_SECONDS if boundary is None else min(
                _KEEPALIVE_SECONDS, boundary + _BOUNDARY_MARGIN_SECONDS)
            try:
                await asyncio.wait_for(signal.wait(), wait)
            except TimeoutError:
                yield ": keepalive\n\n"


@router.get("/session-state/events", response_class=StreamingResponse)
async def session_state_events(
    request: Request,
    policy: Callable[[], RuntimePolicy] = Depends(runtime_policy_reader),
    store: SQLiteAuthenticationStore = Depends(authentication_store),
    authority_port: WebAuthnAuthorityPort = Depends(webauthn_authority_port),
) -> StreamingResponse:
    """Push the access value that session-state reports, so pages never poll it."""

    _require_same_origin_read(request)
    from app.provisioning.state import next_authority_expiry

    def read() -> str:
        access = _session_access(policy(), authority_port)[1]
        return "available" if access is None else access.value

    return AccessEventResponse(access_streams(request.app), read, lambda: next_authority_expiry(store))


@router.get("/session-state", response_model=dict[str, bool | str | None])
def session_state(request: Request, response: Response,
                  policy: RuntimePolicy = Depends(get_runtime_policy),
                  store: SQLiteAuthenticationStore = Depends(authentication_store),
                  authority_port: WebAuthnAuthorityPort = Depends(webauthn_authority_port),
                  ) -> dict[str, bool | str | None] | JSONResponse:
    _require_same_origin_read(request)
    decision, access = _session_access(policy, authority_port)
    if access is not None:
        # One closed value says why, without naming grants or accounts.
        return JSONResponse(
            status_code=409,
            content={"detail": "Enrollment state is unavailable", "access": access.value},
            headers={"Cache-Control": "no-store"},
        )
    enrolled = decision.result is WebAuthnAuthorityResult.AUTHORIZED
    native = getattr(request.app.state, "account_passkey_authority", None)
    if native is not None:
        enrolled = (enrolled and isinstance(decision.authority, SessionAuthority)
                    and native.requires_native(decision.authority.credential_id))
    _no_store(response)
    authenticated = bool(enrolled and store.bridge_session_is_current(policy))
    return {"authenticated": authenticated, "enrolled": enrolled,
            "csrf_token": csrf_token(policy) if authenticated else None,
            **({"native_required": True} if native is not None else {})}


def _require_legacy_passkeys(request: Request) -> None:
    if getattr(request.app.state, "account_passkey_authority", None) is not None:
        raise HTTPException(403, "Open Dipsy to use your passkey", headers={"Cache-Control": "no-store"})


@router.post("/lock")
async def lock_desktop(
    request: Request, response: Response,
    policy: RuntimePolicy = Depends(get_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
) -> dict[str, bool]:
    from app.api.endpoints.saved_data import COOKIE, PREFIX, lock_saved_views
    from app.api.endpoints.transport_ws import close_locked_desktop_views
    from app.analytics.publication_events import publication_events
    _prepare_request(request)
    verify_csrf_token(policy, csrf)
    native = getattr(request.app.state, "account_passkey_authority", None)
    if native is None or policy.identity is None:
        raise HTTPException(403, "An unlocked account passkey session is required")

    def retire_access() -> None:
        native.retire_initial()
        lock_saved_views()

    try:
        await asyncio.to_thread(native.store.lock_desktop, policy, retire_access)
    except AuthenticationStateError:
        raise HTTPException(403, "The desktop session has ended") from None
    publication_events.recheck_sessions()
    await close_locked_desktop_views(native.store)
    response.delete_cookie(settings.bridge_session_cookie_name, path="/", secure=True, httponly=True, samesite="strict")
    response.delete_cookie(COOKIE, path=PREFIX, secure=True, httponly=True, samesite="strict")
    _no_store(response)
    return {"locked": True}


@router.post("/registration/begin")
def begin_registration(
    request: Request,
    response: Response,
    policy: RuntimePolicy = Depends(get_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
    authority_port: WebAuthnAuthorityPort = Depends(webauthn_authority_port),
    service: WebAuthnService = Depends(webauthn_service),
) -> dict[str, object]:
    _prepare_request(request)
    _require_legacy_passkeys(request)
    if policy.identity is not None:
        verify_csrf_token(policy, csrf)
    authority = _authority_or_refuse(
        authority_port.registration_authority(policy),
        operation="registration_begin",
    )
    if not isinstance(authority, RegistrationAuthority):
        raise RuntimeError("Registration authority port returned the wrong authority type")
    _no_store(response)
    options, context = service.begin_first_registration(authority)
    response.set_cookie("__Host-first_enrollment", context, secure=True, httponly=True,
                        samesite="strict", path="/", max_age=settings.webauthn_challenge_ttl_seconds)
    return options


@router.post("/registration/finish")
async def finish_registration(
    request: Request,
    response: Response,
    policy: RuntimePolicy = Depends(get_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
    authority_port: WebAuthnAuthorityPort = Depends(webauthn_authority_port),
    service: WebAuthnService = Depends(webauthn_service),
) -> dict[str, str]:
    _prepare_request(request)
    _require_legacy_passkeys(request)
    if policy.identity is not None:
        verify_csrf_token(policy, csrf)
    body = await _validated_finish_body(request, RegistrationFinishRequest)
    authority = _authority_or_refuse(
        authority_port.registration_authority(policy),
        operation="registration_finish",
    )
    if not isinstance(authority, RegistrationAuthority):
        raise RuntimeError("Registration authority port returned the wrong authority type")
    try:
        issued = service.complete_first_registration(
            authority,
            RegistrationCredential(
                credential_id=body.credential_id,
                raw_id=body.raw_id,
                credential_type=body.credential_type,
                client_data_json=body.response.client_data_json,
                attestation_object=body.response.attestation_object,
            ),
            context=request.cookies.get("__Host-first_enrollment", ""),
            policy=policy,
            authority_port=authority_port,
        )
    except WebAuthnVerificationError as error:
        _verification_refusal(error, operation="registration_finish")
    _no_store(response)
    identity = issued.policy.identity
    if identity is None:
        raise RuntimeError("First enrollment did not produce an authenticated session")
    response.set_cookie(
        key=settings.bridge_session_cookie_name,
        value=webauthn_session_token(identity, issued.session_value),
        secure=True, httponly=True, samesite="strict", path="/",
        max_age=settings.bridge_session_ttl_seconds,
    )
    response.delete_cookie("__Host-first_enrollment", path="/", secure=True, httponly=True, samesite="strict")
    return {"profile": "local-first-enrollment-result.v1", "status": "registered", "csrf_token": issued.csrf_value}


@router.post("/login/begin")
def begin_login(
    request: Request,
    response: Response,
    policy: RuntimePolicy = Depends(get_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
    authority_port: WebAuthnAuthorityPort = Depends(webauthn_authority_port),
    service: WebAuthnService = Depends(webauthn_service),
) -> dict[str, object]:
    _prepare_request(request)
    if policy.identity is not None:
        verify_csrf_token(policy, csrf)
    authority = _authority_or_refuse(
        authority_port.session_authority(policy),
        operation="login_begin",
    )
    if not isinstance(authority, SessionAuthority):
        raise RuntimeError("Session authority port returned the wrong authority type")
    _no_store(response)
    native = getattr(request.app.state, "account_passkey_authority", None)
    if native is not None:
        if not native.requires_native(authority.credential_id):
            raise HTTPException(403, "Open Dipsy to set up your passkey", headers={"Cache-Control": "no-store"})
        return {"windows_hello": True}
    return service.begin_authentication(authority)


@router.post("/login/finish")
async def finish_login(
    request: Request,
    response: Response,
    policy: RuntimePolicy = Depends(get_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
    authority_port: WebAuthnAuthorityPort = Depends(webauthn_authority_port),
    service: WebAuthnService = Depends(webauthn_service),
) -> dict[str, str]:
    _prepare_request(request)
    _require_legacy_passkeys(request)
    if policy.identity is not None:
        verify_csrf_token(policy, csrf)
    body = await _validated_finish_body(request, LoginFinishRequest)
    authority = _authority_or_refuse(
        authority_port.session_authority(policy),
        operation="login_finish",
    )
    if not isinstance(authority, SessionAuthority):
        raise RuntimeError("Session authority port returned the wrong authority type")
    try:
        issued = service.complete_authentication(authority, body.assertion())
    except WebAuthnVerificationError as error:
        _verification_refusal(error, operation="login_finish")
    return _login_cookie(response, authority, issued)


@router.post("/login/windows-hello")
def login_windows_hello(
    request: Request,
    response: Response,
    policy: RuntimePolicy = Depends(get_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
    authority_port: WebAuthnAuthorityPort = Depends(webauthn_authority_port),
) -> dict[str, str]:
    _prepare_request(request)
    _no_store(response)
    if policy.identity is not None:
        verify_csrf_token(policy, csrf)
    return _native_login(request, response, policy, authority_port, initial=False)


@router.post("/login/initial")
def login_initial(
    request: Request,
    response: Response,
    policy: RuntimePolicy = Depends(get_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
    authority_port: WebAuthnAuthorityPort = Depends(webauthn_authority_port),
) -> dict[str, str]:
    """Use the uninterrupted enrollment proof without opening a new dialog."""
    _prepare_request(request)
    _no_store(response)
    if policy.identity is not None:
        verify_csrf_token(policy, csrf)
    return _native_login(request, response, policy, authority_port, initial=True)


def _native_login(request, response, policy, authority_port, *, initial: bool):
    native = getattr(request.app.state, "account_passkey_authority", None)
    authority = _authority_or_refuse(authority_port.session_authority(policy), operation="login_begin")
    if (native is None or not isinstance(authority, SessionAuthority)
            or not native.requires_native(authority.credential_id)):
        raise HTTPException(403, "Windows Hello is unavailable for this account")
    try:
        issued = native.sign_in(
            authority, authorize=authorize_desktop_passkey(request),
            current_authority=lambda connection: authority_port.session_authority(policy, connection=connection).authority,
            lifetime=settings.bridge_session_ttl_seconds,
            initial_generation=request.headers[GENERATION] if initial else None,
        )
    except WindowsHelloRefused as error:
        raise HTTPException(409, error.reason, headers={"Cache-Control": "no-store"}) from None
    except (AdmissionRefused, AuthenticationStateError, SignedObjectRejected, WebAuthnVerificationError):
        raise HTTPException(403, "Windows Hello could not verify this account") from None
    if issued is None:
        raise HTTPException(409, "initial_access_unavailable", headers={"Cache-Control": "no-store"})
    return _login_cookie(response, authority, issued)


def _login_cookie(response: Response, authority: SessionAuthority, issued) -> dict[str, str]:
    session_identity = AuthContext(
        principal_id=authority.principal_id,
        creator_account_id=authority.creator_account_id,
        role=authority.role,
        session_id=issued.session_id,
    )
    sealed_session = webauthn_session_token(session_identity, issued.session_value)
    response.set_cookie(
        key=settings.bridge_session_cookie_name,
        value=sealed_session,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=settings.bridge_session_ttl_seconds,
    )
    _no_store(response)
    return {"csrf_token": csrf_token(issued.policy)}
