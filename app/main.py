"""FastAPI application for ingestion, analytics, Agent, and Bridge traffic."""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.endpoints import companion_pairing, creator_vault, frontend, history, insights, transport_ws, webauthn
from app.analytics import runtime as analytics_runtime
from app.bootstrap import history_source, transport_manager
from app.core.config import settings
from app.core.broadcast import broadcast
from app.core.resource_paths import resource_path
from app.persistence.auth import InstallationKeyReference, SQLiteAuthenticationStore
from app.security.activation_gate import (
    evaluate_runtime_activation,
    record_activation,
)
from app.security.installation_key import (
    InstallationKeyAuthority,
    InstallationKeyUnavailable,
    WindowsCNGInstallationKeyProvider,
)
from app.transport.companion_origin import CompanionOriginBoundary

logger = logging.getLogger(__name__)

_installation_key_authority: InstallationKeyAuthority | None = None
_installation_key_reference: InstallationKeyReference | None = None


def configure_analytics_runtime():
    """Build the analytics runtime from application-owned resources."""

    return analytics_runtime.configure_default_analytics_runtime(
        history_source,
        backend=settings.canonical_persistence_backend,
        projections_path=settings.analytics_projection_database_path,
        canonical_path=settings.canonical_database_path,
        activation=transport_manager.projection_activation,
        post_commit_rebuild_enabled=(
            settings.canonical_persistence_backend == "sqlite"
        ),
    )


# Register dependencies before handlers request the default runtime.
configure_analytics_runtime()


def initialize_installation_key() -> InstallationKeyReference:
    """Load or create the TPM-backed installation key."""
    global _installation_key_authority, _installation_key_reference
    store = SQLiteAuthenticationStore(settings.auth_database_path)
    if settings.environment.lower() == "test":
        active = store.installation_key_reference()
        if active is not None:
            _installation_key_authority = None
            _installation_key_reference = active
            return active
    authority = InstallationKeyAuthority(
        store, WindowsCNGInstallationKeyProvider()
    )
    reference = authority.ensure_ready()
    _installation_key_authority = authority
    _installation_key_reference = reference
    return reference


def activate_runtime() -> None:
    """Ready the installation key, then evaluate the activation conditions.

    An installation whose key provider is unavailable still starts and serves
    the bounded loopback provisioning surface without activating. An
    installation-key policy refusal stops startup.
    """
    if settings.websocket_auth_mode == "local_session":
        try:
            initialize_installation_key()
        except InstallationKeyUnavailable:
            logger.warning(
                "activation_event reason_code=installation_key_unavailable"
            )
    decision = record_activation(evaluate_runtime_activation())
    if decision.production and not decision.activated:
        logger.warning(
            "activation_event reason_code=runtime_activation_refused conditions=%s",
            ",".join(decision.refused_conditions),
        )

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
app.add_middleware(CompanionOriginBoundary)

# -------------------------------------------------
# Static file mount (Vite build output in app/static/dist)
# -------------------------------------------------
app.mount("/static", StaticFiles(directory=resource_path("app/static")), name="static")

# -------------------------------------------------
# Router Registration
# -------------------------------------------------
app.include_router(transport_ws.router, tags=["Transport"])
app.include_router(history.router)
app.include_router(insights.router)
app.include_router(webauthn.router)
app.include_router(creator_vault.router)
app.include_router(companion_pairing.router)

# -------------------------------------------------
# Startup & Shutdown events — manage Broadcast lifecycle
# -------------------------------------------------
@app.on_event("startup")
async def startup_event():
    activate_runtime()
    await broadcast.connect()
    await transport_manager.start()
    # Use resources created for this application lifecycle.
    configure_analytics_runtime()
    # Recover every canonical account's analytics projection in the
    # background; readiness must not wait on this potentially slow replay.
    analytics_runtime.launch_default_analytics_runtime()

@app.on_event("shutdown")
async def shutdown_event():
    await transport_manager.stop()
    drained = await analytics_runtime.shutdown_default_analytics_runtime(
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
