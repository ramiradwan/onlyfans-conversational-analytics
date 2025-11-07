"""  
Frontend routes for OnlyFans Conversational Analytics.  
  
Serves the compiled Vite + React frontend via Jinja2 templates,  
injecting runtime configuration for the browser extension and WebSocket bridge.  
"""  
  
import json  
from pathlib import Path  
from typing import Dict, Any  
  
from fastapi import APIRouter, Request  
from fastapi.responses import HTMLResponse  
from fastapi.templating import Jinja2Templates  
  
from app.utils.logger import logger  
from app.config import settings  # <-- import settings so we can use extension_id  
  
router = APIRouter()  
  
# Directories  
TEMPLATES_DIR = Path("app/templates")  
DIST_DIR = Path("app/static/dist")  
  
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))  
  
  
def read_manifest() -> Dict[str, Any]:  
    """  
    Reads Vite's manifest.json from either the root dist location or .vite subfolder.  
    Returns an empty dict if not found.  
    """  
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
  
  
@router.get("/", response_class=HTMLResponse)  
async def serve_frontend(request: Request):  
    """  
    Serve the frontend entry HTML, injecting runtime config for extension and WS.  
    """  
    manifest = read_manifest()  
  
    # Find entry file by isEntry flag  
    entry_key = next((k for k, v in manifest.items() if v.get("isEntry")), None)  
    if not entry_key and manifest:  
        entry_key = next(iter(manifest.keys()), None)  # fallback  
  
    app_script = manifest.get(entry_key, {}).get("file") if entry_key else None  
    css_files = manifest.get(entry_key, {}).get("css", []) if entry_key else []  
  
    base_ws_url = str(request.base_url).rstrip("/").replace("http", "ws")  
    ws_url = f"{base_ws_url}/api/ws/frontend"  
  
    config = {  
        "EXTENSION_ID": settings.extension_id,  # <-- now comes from .env  
        "FASTAPI_WS_URL": ws_url,  
    }  
  
    logger.info(f"[FRONTEND] Serving frontend with script={app_script}, CSS={css_files}")  
    return templates.TemplateResponse(  
        "index.html",  
        {  
            "request": request,  
            "app_script": f"/static/dist/{app_script}" if app_script else None,  
            "css_files": [f"/static/dist/{c}" for c in css_files],  
            "config": config,  
        },  
    )  