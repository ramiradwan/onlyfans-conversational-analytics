"""Local saved-data access. No caller supplies an owner or receives a Bridge session."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.api.endpoints import creator_vault
from app.api.endpoints.webauthn import (
    LoginFinishRequest, _validated_finish_body, authentication_store, webauthn_service,
)
from app.api.security import verify_same_origin
from app.bootstrap import transport_manager
from app.core.config import settings
from app.onboarding.admission import AdmissionRefused
from app.onboarding.admission_routes import GENERATION, authorize_desktop_passkey
from app.persistence.account_passkey import AccountPasskeyBindings
from app.persistence.archive_export import CreatorVaultExporter
from app.persistence.auth import AuthenticationStateError, SavedDataOwner, SQLiteAuthenticationStore
from app.persistence.retention import RetentionPolicyError
from app.security.account_passkey_windows import WindowsHelloRefused
from app.security.activation_gate import REQUIRED_GRANTS, current_activation
from app.security.signed_objects import SignedObjectRejected
from app.security.installation_key import InstallationKeyAuthority, InstallationKeyError, WindowsCNGInstallationKeyProvider
from app.security.saved_data import SavedDataSessions
from app.security.webauthn import WebAuthnService, WebAuthnVerificationError
from app.services.saved_results import saved_result_document, saved_result_page


PREFIX = "/api/v1/saved-data"
COOKIE = "dipsy_saved_data"
_sessions = SavedDataSessions()


def lock_saved_views() -> None:
    _sessions.lock_all()


def require_local_installation(store: SQLiteAuthenticationStore = Depends(authentication_store)) -> None:
    decision = current_activation()
    if any(condition != REQUIRED_GRANTS for condition in decision.refused_conditions):
        raise HTTPException(503, "Local installation unavailable")
    if decision.production:
        try:
            InstallationKeyAuthority(store, WindowsCNGInstallationKeyProvider()).signing_reference()
        except (InstallationKeyError, OSError):
            raise HTTPException(503, "Local installation unavailable") from None


def _request(request: Request, response: Response) -> None:
    if request.method == "GET" and request.url.path == PREFIX + "/session/events":
        from app.api.endpoints.webauthn import _require_same_origin_read
        _require_same_origin_read(request)
    else:
        verify_same_origin(request)
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix=PREFIX, tags=["Saved data"], dependencies=[
    Depends(_request), Depends(require_local_installation),
])


def _locked() -> HTTPException:
    return HTTPException(403, "Saved data is locked", headers={"Cache-Control": "no-store"})


def _account_passkey(request: Request):
    return getattr(request.app.state, "account_passkey_authority", None)


@router.get("/session/events")
def saved_session_events(request: Request):
    """Notify the open archive when its own local session ends."""
    from app.api.endpoints.webauthn import AccessEventResponse, access_streams
    token = request.cookies.get(COOKIE, "")
    return AccessEventResponse(access_streams(request.app),
        lambda: "available" if _sessions.is_open(token) else "locked", lambda: None)


def start_account_passkey_withdrawals(application) -> None:
    authority = getattr(application.state, "account_passkey_authority", None)
    if authority is not None:
        authority.start(_sessions)


def _access_owner(request, store, connection) -> SavedDataOwner | None:
    authority = _account_passkey(request)
    if authority is None:
        if AccountPasskeyBindings(store).has_retained_authority(connection):
            return None
        return store.saved_data_owner(connection)
    try:
        active = authority.active(connection)
    except (AuthenticationStateError, SignedObjectRejected):
        return None
    return None if active is None else active[0].owner


def _owner(request: Request, store: SQLiteAuthenticationStore) -> SavedDataOwner:
    with store.database.transaction(immediate=False) as connection:
        owner = _access_owner(request, store, connection)
    if owner is None:
        raise _locked()
    return owner


@router.post("/available")
def available(request: Request, store: SQLiteAuthenticationStore = Depends(authentication_store)) -> dict[str, bool]:
    with store.database.transaction(immediate=False) as connection:
        owner = _access_owner(request, store, connection)
        if owner is None:
            return {"available": False}
        result = {"available": True}
        if store.lapsed_account_authority(connection) == "expired":
            result["authorization_expired"] = True
        return result


@router.post("/login/begin")
def begin(request: Request, store: SQLiteAuthenticationStore = Depends(authentication_store),
          service: WebAuthnService = Depends(webauthn_service)) -> dict[str, object]:
    owner = _owner(request, store)
    if owner.account_passkey_binding is not None:
        return {"windows_hello": True}
    try:
        return service.begin_saved_data(owner)
    except AuthenticationStateError:
        raise _locked() from None


@router.post("/login/finish")
async def finish(request: Request, response: Response, store: SQLiteAuthenticationStore = Depends(authentication_store),
                 service: WebAuthnService = Depends(webauthn_service)) -> dict:
    body = await _validated_finish_body(request, LoginFinishRequest)
    owner = _owner(request, store)
    if owner.account_passkey_binding is not None:
        raise _locked()
    try:
        service.verify_saved_data(owner, body.assertion())
    except (WebAuthnVerificationError, AuthenticationStateError):
        raise _locked() from None
    with store.database.transaction() as connection:
        if _access_owner(request, store, connection) != owner:
            raise _locked()
        lifetime = min(settings.bridge_session_ttl_seconds, 900)
        token, csrf = _sessions.issue(owner, lifetime)
    return _session_cookie(response, token, csrf, lifetime)


def _session_cookie(response: Response, token: str, csrf: str, lifetime: int) -> dict:
    response.set_cookie(COOKIE, token, secure=True, httponly=True, samesite="strict", path=PREFIX)
    return {"csrf_token": csrf, "expires_in": lifetime}


@router.post("/login/windows-hello")
def login_windows_hello(request: Request, response: Response) -> dict:
    authority = _account_passkey(request)
    if authority is None:
        raise _locked()
    lifetime = min(settings.bridge_session_ttl_seconds, 900)
    try:
        authorize = authorize_desktop_passkey(request)
        token, csrf = authority.unlock(_sessions, authorize=authorize, lifetime=lifetime,
                                      generation=request.headers[GENERATION])
    except WindowsHelloRefused as error:
        raise HTTPException(409, error.reason, headers={"Cache-Control": "no-store"}) from None
    except (AdmissionRefused, AuthenticationStateError, SignedObjectRejected, WebAuthnVerificationError):
        raise _locked() from None
    return _session_cookie(response, token, csrf, lifetime)


@contextmanager
def _opened(request: Request, store: SQLiteAuthenticationStore):
    # Hold the installation's authority fence until the canonical read or delete commits.
    with store.database.transaction() as connection:
        owner = _access_owner(request, store, connection)
        token = request.cookies.get(COOKIE, "")
        csrf = request.headers.get("x-csrf-token", "")
        if owner is None or not _sessions.accepts(owner, token, csrf):
            raise _locked()
        yield owner, connection


def _enforce_retention(account: str) -> None:
    try:
        creator_vault._retention().enforce(account)
    except RetentionPolicyError:
        raise HTTPException(409, "The stored retention policy is unavailable") from None


@router.post("/view")
def view(request: Request, after: str = Query("", max_length=2048),
         store: SQLiteAuthenticationStore = Depends(authentication_store)) -> dict:
    with _opened(request, store) as (owner, _):
        account = owner.creator_account_id
        _enforce_retention(account)
        with transport_manager.canonical_database.transaction(immediate=False) as db:
            rows = db.execute("""
                SELECT m.message_id AS id, m.text, m.sent_at FROM account_messages m
                JOIN archive_membership a USING(creator_account_id, message_id)
                JOIN archive_policies p USING(creator_account_id)
                WHERE m.creator_account_id=? AND m.is_deleted=0 AND a.vault_purpose=1
                  AND p.vault_enabled=1 AND m.message_id>? ORDER BY m.message_id LIMIT 51
            """, (account, after)).fetchall()
        pending = creator_vault._deletions().outstanding(account)
        return {"messages": [dict(row) for row in rows[:50]],
                "next": rows[49]["id"] if len(rows) > 50 else None, "deletion_pending": pending is not None}


@router.post("/export")
def export(request: Request, store: SQLiteAuthenticationStore = Depends(authentication_store)) -> JSONResponse:
    with _opened(request, store) as (owner, _):
        _enforce_retention(owner.creator_account_id)
        archive = CreatorVaultExporter(transport_manager.canonical_database).build(owner.creator_account_id)
        return JSONResponse(archive.document(), headers={"Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="creator-vault-export.json"'})


@router.post("/delete")
async def delete(request: Request, store: SQLiteAuthenticationStore = Depends(authentication_store)) -> dict[str, str]:
    with _opened(request, store) as (owner, _):
        account = owner.creator_account_id
        operations = creator_vault._deletions()
        pending = operations.outstanding(account)
        operation_id = pending.operation_id if pending else str(uuid4())
        if not pending:
            creator_vault._retention().delete_all(account, provenance=f"creator_delete:{operation_id}")
    # An accepted deletion can finish after the session ends. Reuse the durable retry journal.
    from app.analytics.runtime import invalidate_question_sources
    invalidate_question_sources(account)
    operation = await creator_vault._finish_deletion(account, operation_id, operations)
    return {"status": operation.status}


@router.post("/results")
def results(request: Request, after: UUID | None = None, store: SQLiteAuthenticationStore = Depends(authentication_store)) -> dict:
    with _opened(request, store) as (owner, _):
        _enforce_retention(owner.creator_account_id)
        return saved_result_page(transport_manager.canonical_database, owner, str(after) if after else "")


@router.post("/metrics")
def metrics(request: Request, store: SQLiteAuthenticationStore = Depends(authentication_store)) -> dict:
    from app.analytics.errors import AnalyticsError
    from app.analytics.graph_store import GraphStoreError
    from app.analytics.sqlite_projection_store import ProjectionValidationError
    from app.services.insights_service import saved_metrics

    with _opened(request, store) as (owner, _):
        _enforce_retention(owner.creator_account_id)
        try:
            document = saved_metrics(owner.creator_account_id)
        except (AnalyticsError, GraphStoreError, ProjectionValidationError, ValueError):
            raise HTTPException(409, "Saved metrics could not be verified") from None
        if document is None:
            raise HTTPException(404, "No saved metrics are available")
        return document


@router.post("/results/{kind}/{identifier}")
def result(request: Request, kind: Literal["Full", "Preview"], identifier: UUID,
           store: SQLiteAuthenticationStore = Depends(authentication_store)) -> dict:
    with _opened(request, store) as (owner, connection):
        _enforce_retention(owner.creator_account_id)
        try:
            return saved_result_document(transport_manager.canonical_database, store, connection, owner, str(identifier), kind)
        except ValueError:
            raise HTTPException(404, "This saved result is unavailable") from None


@router.post("/messages/delete")
def delete_message(request: Request, message_id: str = Query(min_length=1, max_length=2048),
                   store: SQLiteAuthenticationStore = Depends(authentication_store)) -> dict[str, bool]:
    with _opened(request, store) as (owner, _):
        account = owner.creator_account_id
        with transport_manager.canonical_database.read() as db:
            exists = db.execute("""SELECT 1 FROM archive_membership WHERE creator_account_id=?
                                   AND message_id=? AND vault_purpose=1""", (account, message_id)).fetchone()
        if not exists:
            raise HTTPException(404, "This saved message is unavailable")
        creator_vault._retention().delete_message(account, message_id)
        from app.analytics.runtime import invalidate_question_sources
        invalidate_question_sources(account)
        return {"deleted": True}


@router.post("/lock")
def lock(request: Request, response: Response) -> dict[str, bool]:
    _sessions.close(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path=PREFIX, secure=True, httponly=True, samesite="strict")
    return {"locked": True}
