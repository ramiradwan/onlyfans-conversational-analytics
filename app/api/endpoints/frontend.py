"""
Frontend routes for OnlyFans Conversational Analytics.
Serves the compiled Vite + React frontend via Jinja2 templates,
injecting runtime configuration for the browser extension and WebSocket bridge.
"""

import hmac
import json
import time
from dataclasses import dataclass
from typing import Dict, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.utils.logger import logger
from app.core.config import settings
from app.core.resource_paths import ResourcePathError, resource_path
from app.api.security import (
    csrf_token,
    get_runtime_policy,
)
from app.security.runtime_policy import RuntimePolicy
from app.transport import transport_manager

router = APIRouter(tags=["Frontend"])

# Directories
TEMPLATES_DIR = resource_path("app/templates")
DIST_DIR = resource_path("app/static/dist")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@dataclass(frozen=True)
class ManifestLoad:
    manifest: Dict[str, Any]
    error: str | None = None


def _is_production_environment() -> bool:
    return settings.environment.lower() not in {"development", "dev", "local", "test"}


def _load_manifest() -> ManifestLoad:
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
                    manifest = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.exception(f"[FRONTEND] Failed to read manifest: {e}")
                return ManifestLoad(
                    {}, f"could not read Vite manifest at {path}: {e}"
                )
            if not isinstance(manifest, dict):
                return ManifestLoad(
                    {}, f"Vite manifest at {path} must contain a JSON object"
                )
            return ManifestLoad(manifest)
    logger.warning("[FRONTEND] No manifest.json found in dist directory")
    return ManifestLoad({}, "Vite manifest is absent from the compiled frontend package")


def _manifest_entry(manifest: Dict[str, Any]) -> tuple[str | None, Dict[str, Any]]:
    entry_key = next(
        (
            key
            for key, value in manifest.items()
            if isinstance(value, dict) and value.get("isEntry")
        ),
        None,
    )
    if entry_key is None:
        return None, {}
    entry = manifest[entry_key]
    return entry_key, entry


def _validate_manifest_assets(entry: Dict[str, Any]) -> str | None:
    """Ensure manifest-referenced frontend assets stay inside the dist package."""

    css_assets = entry.get("css", [])
    if not isinstance(css_assets, list):
        return "Vite manifest has an invalid frontend asset reference"
    assets = [entry.get("file"), *css_assets]
    if not all(isinstance(asset, str) and asset for asset in assets):
        return "Vite manifest has an invalid frontend asset reference"
    for asset in assets:
        try:
            resource_path(asset, root=DIST_DIR)
        except (FileNotFoundError, ResourcePathError) as error:
            return f"Vite manifest frontend asset is invalid: {error}"
    return None


_manifest_load = _load_manifest()
@router.post(
    "/api/v1/session/bootstrap",
    include_in_schema=False,
    response_class=RedirectResponse,
)
async def bootstrap_local_session(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
) -> RedirectResponse:
    """Consume the launcher secret once and confirm a local browser launch."""
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
    if not transport_manager.consume_launcher_bootstrap(ticket):
        raise HTTPException(status_code=401, detail="Bootstrap ticket is invalid or used")
    response = RedirectResponse(url="/", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post(
    "/api/v1/session/handoff",
    include_in_schema=False,
    response_class=JSONResponse,
)
async def issue_local_session_handoff(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
) -> JSONResponse:
    """Exchange the launcher credential for one bounded browser handoff code."""
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
        raise HTTPException(status_code=401, detail="Launcher authorization is invalid")
    code = transport_manager.issue_launcher_handoff(
        ticket, ttl_seconds=settings.launcher_handoff_ttl_seconds
    )
    if code is None:
        raise HTTPException(status_code=409, detail="launcher_bootstrap_already_consumed")
    response = JSONResponse({"handoff_code": code})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "/api/v1/session/handoff",
    include_in_schema=False,
    response_class=RedirectResponse,
)
async def redeem_local_session_handoff(
    request: Request,
) -> Response:
    """Redeem one browser handoff code as proof of a local browser launch."""
    if settings.websocket_auth_mode != "local_session":
        raise HTTPException(status_code=404, detail="Not found")
    expected_host = urlsplit(settings.bridge_origin).netloc.lower()
    if request.headers.get("host", "").lower() != expected_host:
        raise HTTPException(status_code=400, detail="Unexpected local Bridge origin")
    code = request.query_params.get("code", "")
    if len(code) < 32 or not transport_manager.redeem_launcher_handoff(code):
        return JSONResponse(
            status_code=401,
            content={"detail": "Handoff code is invalid or unavailable"},
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )
    response = RedirectResponse(url="/", status_code=303)
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
    policy: RuntimePolicy = Depends(get_runtime_policy),
):
    manifest = _manifest_load.manifest

    entry_key, entry = _manifest_entry(manifest)
    if _is_production_environment() and _manifest_load.error:
        raise HTTPException(
            status_code=500,
            detail=f"Production frontend packaging error: {_manifest_load.error}",
        )
    if _is_production_environment() and (
        not entry_key or not isinstance(entry.get("file"), str) or not entry["file"]
    ):
        raise HTTPException(
            status_code=500,
            detail="Production frontend packaging error: Vite manifest has no entry",
        )
    if _is_production_environment():
        asset_error = _validate_manifest_assets(entry)
        if asset_error:
            raise HTTPException(
                status_code=500,
                detail=f"Production frontend packaging error: {asset_error}",
            )

    app_script = entry.get("file") if entry_key else None
    css_files = entry.get("css", []) if entry_key else []
    api_base_url = (
        str(request.base_url).rstrip("/")
        if settings.websocket_auth_mode == "development_stub"
        else settings.bridge_origin.rstrip("/")
    )
    base_ws_url = api_base_url.replace("http://", "ws://", 1).replace(
        "https://", "wss://", 1
    )
    ws_url = f"{base_ws_url}/ws/bridge"

    identity = policy.identity
    config = {
        "EXTENSION_ID": settings.extension_id,
        "FASTAPI_WS_URL": ws_url,
        "API_BASE_URL": api_base_url,
        "VERSION": settings.version,
        "CREATOR_ID": None if identity is None else identity.creator_account_id,
        "BRIDGE_ROLE": None if identity is None else identity.role,
        "BRIDGE_AUTH_TICKET": (
            None
            if identity is None
            else transport_manager.issue_bridge_ticket(
                principal_id=identity.principal_id,
                creator_account_id=identity.creator_account_id,
                role=identity.role,
                ttl_seconds=(
                    settings.bridge_ticket_ttl_seconds
                    if identity.session_expires_at is None
                    else max(
                        1,
                        min(
                            settings.bridge_ticket_ttl_seconds,
                            identity.session_expires_at - int(time.time()),
                        ),
                    )
                ),
            )
        ),
    }

    logger.info(
        f"[FRONTEND] Serving frontend with script={app_script}, "
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
            "csrf_token": None if identity is None else csrf_token(policy),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/{frontend_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def serve_frontend_route(
    request: Request,
    frontend_path: str,
    policy: RuntimePolicy = Depends(get_runtime_policy),
):
    """Serve BrowserRouter refreshes without swallowing API or transport namespaces."""
    first_segment = frontend_path.split("/", 1)[0]
    if first_segment in {"api", "ws", "static"}:
        raise HTTPException(status_code=404, detail="Not found")
    return await serve_frontend(request, policy)
