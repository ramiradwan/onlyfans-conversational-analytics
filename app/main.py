"""
Main entry point for the OnlyFans Conversational Analytics API.

This FastAPI app exposes:
- authenticated protocol-v2 ingestion, settings, and message-page routes
- Agent and Bridge WebSocket transports
- Frontend React app (built with Vite) served via Jinja2 templates
- Static assets (JS/CSS) from the Vite build
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.endpoints import frontend, history, insights, transport_ws
from app.core.config import settings
from app.core.broadcast import broadcast
from app.services import insights_service
from app.transport import transport_manager

logger = logging.getLogger(__name__)

# -------------------------------------------------
# FastAPI application metadata from settings
# -------------------------------------------------
app = FastAPI(
    title=settings.app_name,
    description="Ingest, enrich, and analyze OnlyFans creator–fan conversations",
    version=settings.version,
)

# -------------------------------------------------
# CORS for frontend + extension
# Loose in dev, configurable via settings/environment
# -------------------------------------------------
allowed_origins = [settings.bridge_origin]
if settings.websocket_auth_mode == "development_stub":
    allowed_origins.extend(
        [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8000",
        ]
    )

# Add extension origin if extension_id is set
if settings.extension_id:
    allowed_origins.append(f"chrome-extension://{settings.extension_id}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------
# Static file mount (Vite build output in app/static/dist)
# -------------------------------------------------
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# -------------------------------------------------
# Router Registration
# -------------------------------------------------
app.include_router(transport_ws.router, tags=["Transport"])
app.include_router(history.router)
app.include_router(insights.router)

# -------------------------------------------------
# Startup & Shutdown events — manage Broadcast lifecycle
# -------------------------------------------------
@app.on_event("startup")
async def startup_event():
    await broadcast.connect()
    await transport_manager.start()
    # Recover every canonical account's analytics projection in the
    # background; readiness must not wait on this potentially slow replay.
    insights_service.launch_default_projection_scheduler()

@app.on_event("shutdown")
async def shutdown_event():
    await transport_manager.stop()
    drained = await insights_service.shutdown_default_projection_scheduler(
        timeout=5.0
    )
    if not drained:
        logger.warning(
            "analytics_scheduler_event "
            "reason_code=analytics_projection_shutdown_timeout "
            "event_type=shutdown count=1"
        )
    await broadcast.disconnect()

# -------------------------------------------------
# Health Check
# -------------------------------------------------
@app.get("/health", tags=["Health"])
async def health_check():
    """Simple health check endpoint."""
    return {
        "status": "ok",
        "message": "API is running",
        "version": settings.version,
        "environment": settings.environment,
    }


# Keep the SPA catch-all last so explicit health/API routes always win.
app.include_router(frontend.router, tags=["Frontend"])
