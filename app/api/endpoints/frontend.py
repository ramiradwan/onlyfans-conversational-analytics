"""
Frontend routes for OnlyFans Conversational Analytics.
Serves the compiled Vite + React frontend via Jinja2 templates,
injecting runtime configuration for the browser extension and WebSocket bridge.
"""

import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Dict, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.utils.logger import logger
from app.core.config import settings
from app.api.security import (
    AuthContext,
    csrf_token,
    get_auth_context,
    local_session_token,
)
from app.transport import transport_manager

router = APIRouter(tags=["Frontend"])

# Directories
TEMPLATES_DIR = Path("app/templates")
DIST_DIR = Path("app/static/dist")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _load_manifest() -> Dict[str, Any]:
    """Read Vite's manifest.json from dist."""
    manifest_paths = [
        DIST_DIR / "manifest.json",
        DIST_DIR / ".vite" / "manifest.json"
    ]
    for path in manifest_paths:
        if path.exists():
            logger.info(f"[FRONTEND] Reading manifest from {path}")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.exception(f"[FRONTEND] Failed to read manifest: {e}")
                return {}
    logger.warning("[FRONTEND] No manifest.json found in dist directory")
    return {}


_manifest = _load_manifest()
@router.post(
    "/api/v1/session/bootstrap",
    include_in_schema=False,
    response_class=RedirectResponse,
)
async def bootstrap_local_session(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
) -> RedirectResponse:
    """Consume the launcher secret once and establish the exact local session."""
    if settings.websocket_auth_mode != "local_session":
        raise HTTPException(status_code=404, detail="Not found")
    expected_host = urlsplit(settings.bridge_origin).netloc.lower()
    if request.headers.get("host", "").lower() != expected_host:
        raise HTTPException(status_code=400, detail="Unexpected local Bridge origin")
    if authorization is None:
        raise HTTPException(status_code=401, detail="Launcher authorization is required")
    scheme, separator, ticket = authorization.partition(" ")
    if not separator or scheme.lower() != "bootstrap" or len(ticket) < 32:
        raise HTTPException(status_code=401, detail="Launcher authorization is invalid")
    expected = settings.local_session_bootstrap_token.get_secret_value()
    if not hmac.compare_digest(ticket, expected):
        raise HTTPException(status_code=401, detail="Bootstrap ticket is invalid or used")
    context = AuthContext(
        principal_id=settings.local_principal_id,
        creator_account_id=settings.local_creator_account_id,
        role=settings.local_bridge_role,
        platform_creator_id=settings.local_platform_creator_id,
        session_id=secrets.token_urlsafe(24),
    )
    sealed_session = local_session_token(context)
    if not transport_manager.consume_launcher_bootstrap(
        ticket,
        principal_id=context.principal_id,
        creator_account_id=context.creator_account_id,
    ):
        raise HTTPException(status_code=401, detail="Bootstrap ticket is invalid or used")
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=settings.bridge_session_cookie_name,
        value=sealed_session,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=settings.bridge_session_ttl_seconds,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get(
    "/",
    response_class=HTMLResponse,
    operation_id="serveFrontend"
)
async def serve_frontend(
    request: Request,
    context: AuthContext = Depends(get_auth_context),
):
    manifest = _manifest

    # Find entry file by isEntry flag
    entry_key = next((k for k, v in manifest.items() if v.get("isEntry")), None)
    if not entry_key and manifest:
        entry_key = next(iter(manifest.keys()), None)

    app_script = manifest.get(entry_key, {}).get("file") if entry_key else None
    css_files = manifest.get(entry_key, {}).get("css", []) if entry_key else []

    api_base_url = (
        str(request.base_url).rstrip("/")
        if settings.websocket_auth_mode == "development_stub"
        else settings.bridge_origin.rstrip("/")
    )
    base_ws_url = api_base_url.replace("http://", "ws://", 1).replace(
        "https://", "wss://", 1
    )
    ws_url = f"{base_ws_url}/ws/bridge"

    config = {
        "EXTENSION_ID": settings.extension_id,
        "FASTAPI_WS_URL": ws_url,
        "API_BASE_URL": api_base_url,
        "VERSION": settings.version,
        "CREATOR_ID": context.creator_account_id,
        "BRIDGE_ROLE": context.role,
        "BRIDGE_AUTH_TICKET": transport_manager.issue_bridge_ticket(
            principal_id=context.principal_id,
            creator_account_id=context.creator_account_id,
            role=context.role,
            ttl_seconds=(
                settings.bridge_ticket_ttl_seconds
                if context.session_expires_at is None
                else max(
                    1,
                    min(
                        settings.bridge_ticket_ttl_seconds,
                        context.session_expires_at - int(time.time()),
                    ),
                )
            ),
        ),
    }

    logger.info(
        f"[FRONTEND] Serving development Bridge with script={app_script}, "
        f"CSS={css_files}, WS_URL={ws_url}"
    )

    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "app_script": f"/static/dist/{app_script}" if app_script else None,
            "css_files": [f"/static/dist/{c}" for c in css_files],
            "config": config,
            "csrf_token": csrf_token(context),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/{frontend_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def serve_frontend_route(
    request: Request,
    frontend_path: str,
    context: AuthContext = Depends(get_auth_context),
):
    """Serve BrowserRouter refreshes without swallowing API or transport namespaces."""
    first_segment = frontend_path.split("/", 1)[0]
    if first_segment in {"api", "ws", "static"}:
        raise HTTPException(status_code=404, detail="Not found")
    return await serve_frontend(request, context)
