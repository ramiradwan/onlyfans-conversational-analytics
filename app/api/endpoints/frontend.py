"""
Frontend routes for OnlyFans Conversational Analytics.
Serves the compiled Vite + React frontend via Jinja2 templates,
injecting runtime configuration for the browser extension and WebSocket bridge.
"""

import json
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.utils.logger import logger
from app.core.config import settings
from app.core.customer_release import (
    CustomerReleaseConfigurationError,
    load_customer_release_config,
)
from app.core.resource_paths import ResourcePathError, resource_path
from app.api.security import (
    csrf_token,
    get_runtime_policy,
)
from app.bootstrap import transport_manager
from app.security.runtime_policy import RuntimePolicy

router = APIRouter(tags=["Frontend"])

# Directories
TEMPLATES_DIR = resource_path("app/templates")
DIST_DIR = resource_path("app/static/dist")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@lru_cache(maxsize=1)
def _setup_transfer_receiver_url() -> str:
    """Explicit setup-transfer receiver; never a generic sign-in destination."""
    try:
        return load_customer_release_config().setup_transfer_receiver_url
    except CustomerReleaseConfigurationError:
        return ""


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


_FIRST_PAINT_FONT = re.compile(r"-latin-[a-z]+-normal-[\w-]+\.woff2$")


def _manifest_entry(manifest: Dict[str, Any]) -> tuple[str | None, Dict[str, Any]]:
    entry = manifest.get("index.html")
    if not isinstance(entry, dict) or entry.get("isEntry") is not True:
        return None, {}
    return "index.html", entry


def _first_paint_fonts(entry: Dict[str, Any]) -> list[str]:
    """Return the latin subsets of the bundled faces, which the first screen renders with."""
    assets = entry.get("assets", [])
    if not isinstance(assets, list):
        return []
    return [
        asset
        for asset in assets
        if isinstance(asset, str) and _FIRST_PAINT_FONT.search(asset)
    ]


def _validate_manifest_assets(entry: Dict[str, Any]) -> str | None:
    """Ensure manifest-referenced frontend assets stay inside the dist package."""

    css_assets = entry.get("css", [])
    if not isinstance(css_assets, list):
        return "Vite manifest has an invalid frontend asset reference"
    assets = [entry.get("file"), *css_assets, *_first_paint_fonts(entry)]
    if not all(isinstance(asset, str) and asset for asset in assets):
        return "Vite manifest has an invalid frontend asset reference"
    for asset in assets:
        try:
            resource_path(asset, root=DIST_DIR)
        except (FileNotFoundError, ResourcePathError) as error:
            return f"Vite manifest frontend asset is invalid: {error}"
    return None


_manifest_load = _load_manifest()
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
    font_files = _first_paint_fonts(entry) if entry_key else []
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
        "SETUP_TRANSFER_RECEIVER_URL": _setup_transfer_receiver_url(),
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
            "font_preloads": [f"/static/dist/{f}" for f in font_files],
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
    if first_segment in {"api", "ws", "static", "provisioning"}:
        raise HTTPException(status_code=404, detail="Not found")
    return await serve_frontend(request, policy)
