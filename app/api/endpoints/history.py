"""Authenticated history settings and stable projection message pages."""

from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from app.api.security import (
    AuthContext,
    get_auth_context,
    require_creator,
    verify_same_origin,
    verify_csrf_token,
)
from app.core.config import settings
from app.models.history import (
    AgentPairingResponse,
    HistorySettingsResponse,
    MessagePageResponse,
    UpdateHistorySettingsRequest,
)
from app.persistence.history import ProjectionCursorStale
from app.services.paging_cursor import (
    InvalidMessageCursor,
    MessageCursor,
    MessageCursorCodec,
)
from app.transport.manager import transport_manager


router = APIRouter(prefix="/api/v1", tags=["History"])
cursor_codec = MessageCursorCodec(settings.security_signing_secret.get_secret_value())


def _public_settings(document: dict[str, Any]) -> HistorySettingsResponse:
    return HistorySettingsResponse.model_validate_json(
        json.dumps({
            name: document[name]
            for name in HistorySettingsResponse.model_fields
        })
    )


def _settings_response(
    document: dict[str, Any], response: Response
) -> HistorySettingsResponse:
    public = _public_settings(document)
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = f'"{public.settings_revision}"'
    return public


def _expected_revision(if_match: str | None) -> int:
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match is required")
    value = if_match.removeprefix("W/").strip().strip('"')
    try:
        revision = int(value)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="If-Match must be a settings revision") from error
    if revision < 0:
        raise HTTPException(status_code=400, detail="If-Match must be non-negative")
    return revision


@router.post("/agent/pairing", response_model=AgentPairingResponse)
def create_agent_pairing(
    request: Request,
    response: Response,
    context: AuthContext = Depends(get_auth_context),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
) -> AgentPairingResponse:
    """Issue one short-lived, exact-account ticket consumed by one Agent handshake."""
    require_creator(context)
    verify_same_origin(request)
    verify_csrf_token(context, csrf)
    ticket, expires_at = transport_manager.issue_agent_pairing_ticket(
        principal_id=context.principal_id,
        creator_account_id=context.creator_account_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return AgentPairingResponse(pairing_ticket=ticket, expires_at=expires_at)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=MessagePageResponse,
)
def get_message_page(
    conversation_id: str,
    response: Response,
    before: str | None = Query(None),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    context: AuthContext = Depends(get_auth_context),
) -> MessagePageResponse:
    response.headers["Cache-Control"] = "no-store"
    if not transport_manager.projection.conversation_exists(
        context.creator_account_id, conversation_id
    ):
        raise HTTPException(status_code=404, detail="Conversation was not found")

    cursor = None
    if before is not None:
        try:
            cursor = cursor_codec.decode(before)
        except InvalidMessageCursor as error:
            raise HTTPException(status_code=400, detail="cursor_invalid") from error
        if (
            cursor.account_id != context.creator_account_id
            or cursor.conversation_id != conversation_id
        ):
            raise HTTPException(status_code=400, detail="cursor_invalid")

    try:
        items, has_more, generation = transport_manager.projection.message_rows(
            context.creator_account_id,
            conversation_id,
            before=None if cursor is None else (cursor.sent_at, cursor.message_id),
            limit=limit,
            expected_generation=(
                None if cursor is None else cursor.projection_generation
            ),
            expected_revision=None if cursor is None else cursor.projection_revision,
        )
    except ProjectionCursorStale as error:
        raise HTTPException(status_code=409, detail="cursor_stale") from error
    except LookupError as error:
        raise HTTPException(status_code=503, detail="projection_unavailable") from error

    older_cursor = None
    if has_more and items:
        oldest = items[0]
        older_cursor = cursor_codec.encode(
            MessageCursor(
                account_id=context.creator_account_id,
                conversation_id=conversation_id,
                projection_generation=generation["generation_id"],
                projection_revision=generation["projected_revision"],
                sent_at=oldest["sent_at"],
                message_id=oldest["message_id"],
            )
        )

    projection = transport_manager.projection.state(context.creator_account_id)
    if projection["projected_revision"] != generation["projected_revision"]:
        raise HTTPException(status_code=409, detail="cursor_stale")
    return MessagePageResponse.model_validate_json(
        json.dumps({
            "creator_account_id": context.creator_account_id,
            "conversation_id": conversation_id,
            "projection_generation": generation["generation_id"],
            "read_revision": generation["read_revision"],
            "generated_at": generation["generated_at"],
            "items": items,
            "older_cursor": older_cursor,
            "has_older_stored_items": has_more,
            "conversation_coverage": transport_manager.history.conversation_coverage(
                context.creator_account_id, conversation_id
            ),
            "projection": projection,
        })
    )


@router.get("/settings/history", response_model=HistorySettingsResponse)
def get_history_settings(
    response: Response,
    context: AuthContext = Depends(get_auth_context),
) -> HistorySettingsResponse:
    return _settings_response(
        transport_manager.history.history_settings(context.creator_account_id),
        response,
    )


@router.put("/settings/history", response_model=HistorySettingsResponse)
async def update_history_settings(
    request: Request,
    settings_request: UpdateHistorySettingsRequest,
    response: Response,
    context: AuthContext = Depends(get_auth_context),
    if_match: str | None = Header(None, alias="If-Match"),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
) -> HistorySettingsResponse:
    require_creator(context)
    verify_same_origin(request)
    verify_csrf_token(context, csrf)
    expected_revision = _expected_revision(if_match)
    current = transport_manager.history.history_settings(context.creator_account_id)
    consent_revision = current["consent_revision"]
    authorized_platform_creator_id = current["authorized_platform_creator_id"]
    if settings_request.accept_consent:
        if settings_request.consent_policy_version != current["consent_policy_version"]:
            raise HTTPException(status_code=422, detail="Current consent policy must be accepted")
        consent_revision = f"consent-{uuid4()}"
        if context.platform_creator_id is None:
            raise HTTPException(
                status_code=403,
                detail="A verified platform creator binding is required",
            )
        authorized_platform_creator_id = context.platform_creator_id
    elif settings_request.consent_policy_version not in {None, current["consent_policy_version"]}:
        raise HTTPException(status_code=422, detail="Consent policy version is invalid")
    if settings_request.desired_state == "running" and (
        consent_revision is None or authorized_platform_creator_id is None
    ):
        raise HTTPException(status_code=422, detail="Consent is required to start historical sync")
    try:
        updated = transport_manager.history.update_history_settings(
            context.creator_account_id,
            expected_revision=expected_revision,
            values={
                "consent_policy_version": current["consent_policy_version"],
                "consent_revision": consent_revision,
                "authorized_platform_creator_id": authorized_platform_creator_id,
                "desired_state": settings_request.desired_state,
                "recent_window_days": settings_request.recent_window_days,
                "page_size": settings_request.page_size,
                "pages_per_wake": settings_request.pages_per_wake,
                "request_interval_ms": settings_request.request_interval_ms,
                "retry_limit": settings_request.retry_limit,
            },
        )
    except LookupError as error:
        raise HTTPException(status_code=412, detail="settings_revision_conflict") from error
    try:
        await transport_manager.publish_history_settings(
            context.creator_account_id, updated
        )
    except LookupError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _settings_response(
        transport_manager.history.history_settings(context.creator_account_id),
        response,
    )


@router.delete("/settings/history/consent", response_model=HistorySettingsResponse)
async def revoke_history_settings(
    request: Request,
    response: Response,
    context: AuthContext = Depends(get_auth_context),
    if_match: str | None = Header(None, alias="If-Match"),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
) -> HistorySettingsResponse:
    require_creator(context)
    verify_same_origin(request)
    verify_csrf_token(context, csrf)
    expected_revision = _expected_revision(if_match)
    current = transport_manager.history.history_settings(context.creator_account_id)
    try:
        updated = transport_manager.history.update_history_settings(
            context.creator_account_id,
            expected_revision=expected_revision,
            values={
                "consent_policy_version": current["consent_policy_version"],
                "consent_revision": None,
                "authorized_platform_creator_id": None,
                "desired_state": "revoked",
                "recent_window_days": current["recent_window_days"],
                "page_size": current["page_size"],
                "pages_per_wake": current["pages_per_wake"],
                "request_interval_ms": current["request_interval_ms"],
                "retry_limit": current["retry_limit"],
            },
        )
    except LookupError as error:
        raise HTTPException(status_code=412, detail="settings_revision_conflict") from error
    try:
        await transport_manager.publish_history_settings(
            context.creator_account_id, updated
        )
    except LookupError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _settings_response(
        transport_manager.history.history_settings(context.creator_account_id),
        response,
    )
