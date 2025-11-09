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
  
app = FastAPI(  
    title="OnlyFans Conversational Analytics",  
    description="Ingest, enrich, and analyze OnlyFans creator–fan conversations",  
    version="0.7.0",  
)  
  
# ------------------------  
# CORS for frontend + extension (loose in dev, tighten in prod)  
# ------------------------  
app.add_middleware(  
    CORSMiddleware,  
    allow_origins=[  
        "http://localhost:5173",      # Vite dev server  
        "http://127.0.0.1:5173",  
        "http://localhost:8000",      # FastAPI backend  
        f"chrome-extension://{settings.extension_id}",  # Extension origin  
    ],  
    allow_credentials=True,  
    allow_methods=["*"],  
    allow_headers=["*"],  
)  
  
# ------------------------  
# Mount static files (includes Vite build output in app/static/dist)  
# ------------------------  
app.mount("/static", StaticFiles(directory="app/static"), name="static")  
  
# ------------------------  
# Router Registration  
# ------------------------  
app.include_router(insights.router, prefix="/api/insights", tags=["Insights"])  
app.include_router(websocket.router, prefix="/api", tags=["WebSocket"])  # unified WS hub  
app.include_router(schema.router)  # schemas endpoint  
app.include_router(frontend.router, tags=["Frontend"])  # serves / via Jinja template  
  
# ------------------------  
# Startup & Shutdown events — manage Broadcast lifecycle  
# ------------------------  
@app.on_event("startup")  
async def startup_event():  
    await broadcast.connect()  
  
@app.on_event("shutdown")  
async def shutdown_event():  
    await broadcast.disconnect()  
  
# ------------------------  
# Health Check  
# ------------------------  
@app.get("/health", tags=["Health"])  
async def health_check():  
    """Simple health check endpoint."""  
    return {"status": "ok", "message": "API is running"}  