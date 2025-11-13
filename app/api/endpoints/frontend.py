"""  
Frontend routes for OnlyFans Conversational Analytics.  
Serves the compiled Vite + React frontend via Jinja2 templates,  
injecting runtime configuration for the browser extension and WebSocket bridge.  
Also exposes a bootstrap endpoint for initial state hydration.  
"""  
  
import json  
from pathlib import Path  
from typing import Dict, Any  
  
from fastapi import APIRouter, Request, Query  
from fastapi.responses import HTMLResponse  
from fastapi.templating import Jinja2Templates  
import redis  
  
from app.utils.logger import logger  
from app.core.config import settings  
from app.services.insights_service import get_full_snapshot  # NEW: service to query DB  
from app.models.insights import FullSyncResponse  # Pydantic model  
  
router = APIRouter(tags=["Frontend"])  
  
# Directories  
TEMPLATES_DIR = Path("app/templates")  
DIST_DIR = Path("app/static/dist")  
  
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))  
  
# Redis connection  
r = redis.Redis.from_url(settings.redis.url)  
  
  
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
  
  
@router.get("/", response_class=HTMLResponse)  
async def serve_frontend(  
    request: Request,  
    user_id: str = Query(None, description="User ID (must match extension connection)")  
):  
    manifest = _manifest  
  
    # Find entry file by isEntry flag  
    entry_key = next((k for k, v in manifest.items() if v.get("isEntry")), None)  
    if not entry_key and manifest:  
        entry_key = next(iter(manifest.keys()), None)  
  
    app_script = manifest.get(entry_key, {}).get("file") if entry_key else None  
    css_files = manifest.get(entry_key, {}).get("css", []) if entry_key else []  
  
    base_ws_url = str(request.base_url).rstrip("/").replace("http", "ws") 
     
    # Auto‑fetch from Redis if no query param
    if not user_id:  
        latest_id = r.get("latest_user_id")  
        if latest_id:  
            user_id = latest_id.decode("utf-8")  
            logger.info(f"[FRONTEND] Auto‑selected latest user_id from Redis: {user_id}")  
        else:  
            logger.warning("[FRONTEND] No user_id provided, defaulting to demo_user")  
            user_id = "demo_user"  
    
    ws_url = f"{base_ws_url}/api/ws/frontend/{user_id}"  
  
    config = {  
        "EXTENSION_ID": settings.extension_id,  
        "FASTAPI_WS_URL": ws_url,  
        "API_BASE_URL": str(request.base_url).rstrip("/"),  
        "VERSION": settings.version,  
    }  
  
    logger.info(  
        f"[FRONTEND] Serving frontend for user_id={user_id} "  
        f"with script={app_script}, CSS={css_files}, WS_URL={ws_url}"  
    )  
  
    return templates.TemplateResponse(  
        "index.html",  
        {  
            "request": request,  
            "app_script": f"/static/dist/{app_script}" if app_script else None,  
            "css_files": [f"/static/dist/{c}" for c in css_files],  
            "config": config,  
        },  
    )  
  
  
# NEW: Bootstrap endpoint for frontend hydration  
@router.get("/api/v1/frontend/bootstrap/{user_id}", response_model=FullSyncResponse)  
async def bootstrap_state(user_id: str):  
    """  
    Returns the latest snapshot for a given user_id from DB.  
    Used by the frontend to hydrate state immediately after page load.  
    """  
    snapshot = await get_full_snapshot(user_id)  
    if not snapshot:  
        logger.warning(f"[FRONTEND] No snapshot found for user_id={user_id}")  
        return FullSyncResponse(conversations=[], analytics={})  
    return snapshot  