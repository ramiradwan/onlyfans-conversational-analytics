"""  
Main entry point for the OnlyFans Conversational Analytics API.  
  
This FastAPI app exposes:  
- API routes for conversation ingestion and insights  
- WebSocket hub for extension, frontend, and other real-time services  
- Frontend React app (built with Vite) served via Jinja2 templates  
- Static assets (JS/CSS) from the Vite build  
"""  
  
from fastapi import FastAPI  
from fastapi.middleware.cors import CORSMiddleware  
from fastapi.staticfiles import StaticFiles  
  
from app.api.endpoints import insights, frontend, websocket, schema  
from app.core.config import settings  
from app.core.broadcast import broadcast  
  
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
allowed_origins = [  
    "http://localhost:5173",     # Vite dev server  
    "http://127.0.0.1:5173",  
    "http://localhost:8000",     # FastAPI backend  
]  
  
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
app.include_router(insights.router, prefix="/api/insights", tags=["Insights"])  
app.include_router(websocket.router, prefix="/api", tags=["WebSocket"])  # unified WS hub  
app.include_router(schema.router)  # schemas endpoint  
app.include_router(frontend.router, tags=["Frontend"])  # serves / via Jinja template  
  
# -------------------------------------------------  
# Startup & Shutdown events — manage Broadcast lifecycle  
# -------------------------------------------------  
@app.on_event("startup")  
async def startup_event():  
    await broadcast.connect()  
  
@app.on_event("shutdown")  
async def shutdown_event():  
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