"""  
API endpoint to expose WebSocket message schemas for frontend type generation.  
"""  
  
from fastapi import APIRouter  
from fastapi.responses import JSONResponse  
from pydantic import TypeAdapter  
from app.models.wss import OutgoingWssMessage  
  
router = APIRouter()  
  
# Pre-generate schema once at import time  
_wss_schema = TypeAdapter(OutgoingWssMessage).json_schema(mode="serialization")  
  
@router.get("/api/v1/schemas/wss", tags=["Schemas"], response_class=JSONResponse)  
async def get_wss_schema() -> dict:  
    """  
    Returns the JSON schema for OutgoingWssMessage.  
    Frontend uses this to auto-generate TypeScript WS types.  
    """  
    return _wss_schema  