"""  
API endpoint to expose WebSocket message schemas for frontend type generation.  
  
Spec compliance:  
    - GET /api/v1/schemas/wss returns the JSON schema for OutgoingWssMessage.  
    - Used by frontend build tooling to auto-generate TypeScript WS types  
      via json-schema-to-typescript.  
"""  
  
from fastapi import APIRouter  
from fastapi.responses import JSONResponse  
from pydantic import TypeAdapter  
from app.models.wss import OutgoingWssMessage  
  
router = APIRouter()  
  
# Pre-generate schema once at import time for performance  
_wss_schema = TypeAdapter(OutgoingWssMessage).json_schema(mode="serialization")  
  
@router.get("/api/v1/schemas/wss", tags=["Schemas"], response_class=JSONResponse)  
async def get_wss_schema() -> dict:  
    """  
    Returns the JSON schema for OutgoingWssMessage.  
  
    Frontend tooling uses this to auto-generate TypeScript WS types.  
    This ensures strict type safety between backend WS payloads  
    and frontend consumers.  
  
    Example usage in frontend:  
        $ curl http://localhost:8000/api/v1/schemas/wss > wss-schema.json  
        $ json-schema-to-typescript wss-schema.json > ws-types.ts  
    """  
    return _wss_schema  