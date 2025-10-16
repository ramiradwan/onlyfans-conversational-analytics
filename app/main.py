"""  
Main entry point for the OnlyFans Conversational Analytics API.  
  
This FastAPI app exposes endpoints to:  
- Fetch conversation data from a creator's OnlyFans account.  
- Generate insights from processed messages.  
  
Routers:  
- /conversations → Fetch chats and messages  
- /insights → Analyze enriched conversation data  
"""  
  
from fastapi import FastAPI  
  
from app.routes import conversations, insights  
  
app = FastAPI(  
    title="OnlyFans Conversational Analytics",  
    description="Turn your chats into insights",  
    version="0.1.0"  
)  
  
# ------------------------  
# Router Registration  
# ------------------------  
app.include_router(  
    conversations.router,  
    prefix="/conversations",  
    tags=["Conversations"]  
)  
  
app.include_router(  
    insights.router,  
    prefix="/insights",  
    tags=["Insights"]  
)  
  
# ------------------------  
# Health Check  
# ------------------------  
@app.get("/", tags=["Health"])  
async def root():  
    """Simple health check endpoint."""  
    return {"status": "ok", "message": "OnlyFans Conversational Analytics API is running"}  