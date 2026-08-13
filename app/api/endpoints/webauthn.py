"""Activation-gated HTTP adapter for local WebAuthn ceremonies."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Literal, NoReturn, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.activation import require_activated_runtime
from app.api.security import (
    get_runtime_policy,
    verify_csrf_token,
    verify_same_origin,
)
from app.core.config import settings
from app.persistence.auth import SQLiteAuthenticationStore
from app.security.runtime_policy import RuntimePolicy
from app.security.webauthn import (
    AuthenticationCredential,
    RegistrationAuthority,
    RegistrationCredential,
    SessionAuthority,
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
    return configured_webauthn_service(store)


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
    if policy.identity is not None:
        verify_csrf_token(policy, csrf)
    authority = _authority_or_refuse(
        authority_port.registration_authority(policy),
        operation="registration_begin",
    )
    if not isinstance(authority, RegistrationAuthority):
        raise RuntimeError("Registration authority port returned the wrong authority type")
    _no_store(response)
    return service.begin_registration(authority)


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
        service.complete_registration(
            authority,
            RegistrationCredential(
                credential_id=body.credential_id,
                raw_id=body.raw_id,
                credential_type=body.credential_type,
                client_data_json=body.response.client_data_json,
                attestation_object=body.response.attestation_object,
            ),
        )
    except WebAuthnVerificationError as error:
        _verification_refusal(error, operation="registration_finish")
    _no_store(response)
    return {"status": "registered"}


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
        issued = service.complete_authentication(
            authority,
            AuthenticationCredential(
                credential_id=body.credential_id,
                raw_id=body.raw_id,
                credential_type=body.credential_type,
                client_data_json=body.response.client_data_json,
                authenticator_data=body.response.authenticator_data,
                signature=body.response.signature,
                user_handle=body.response.user_handle,
            ),
        )
    except WebAuthnVerificationError as error:
        _verification_refusal(error, operation="login_finish")
    response.set_cookie(
        key=settings.bridge_session_cookie_name,
        value=issued.session_value,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=settings.bridge_session_ttl_seconds,
    )
    _no_store(response)
    return {"csrf_token": issued.csrf_value}
