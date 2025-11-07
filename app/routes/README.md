# Routes  
  
Contains **FastAPI route definitions** for the API layer of OnlyFans Conversational Analytics.  
  
---  
  
## Components  
  
- **conversations.py**    
  Endpoints to fetch and process conversations.  
  - `/chats` — Fetch chat threads from OnlyFans API or browser extension.  
  - `/chats/{chat_id}/messages` — Fetch messages for a specific chat.  
  - `/chats/{chat_id}/full` — Fetch a chat thread with all messages.  
  - `/from-extension` — Accept IndexedDB dump from browser extension, run enrichment + graph build, update cache, return typed data.  
  - `/extension-cache` — Update backend cache directly from raw payloads.  
  - `/sync` — Return current cached chats/messages as typed Pydantic models.  
  
- **frontend.py**    
  Serves the compiled Vite + React frontend via Jinja2 templates, injecting:  
  - `EXTENSION_ID` from `.env`  
  - `FASTAPI_WS_URL` for WebSocket bridge  
  - Entry JS/CSS from Vite `manifest.json`  
  
- **insights.py**    
  Endpoints to return analytical metrics:  
  - `/topics` — Volume, % total, and trend for each topic.  
  - `/sentiment-trend` — Average sentiment score trend over time.  
  - `/response-time` — Average handling time, silence percentage, and turns.  
  
- **websocket.py**    
  WebSocket hub for real-time connections:  
  - `/ws/extension` — Browser extension → Backend ingestion.  
  - `/ws/frontend` — React dashboard updates.  
  - `/ws/chatwoot` — External integration.  
  - Broadcasts typed `SyncResponse` payloads identical to `/sync`.  
  
---  
  
## Purpose  
  
Routes:  
- Accept HTTP or WS requests.  
- Validate parameters and payloads with Pydantic models.  
- Call appropriate service functions (`OnlyFansClient`, `DataIngestService`, enrichment, graph builder, analytics).  
- Return typed responses, ensuring consistent schema across REST and WS.  
  
---  
  
## Schema Consistency  
  
- All conversation data returned via REST (`/sync`, `/from-extension`) and WS (`type=cache_update`) uses the same `SyncResponse` model.  
- Frontend TypeScript types generated from OpenAPI will match WS payloads.  
- Analytics routes return isolated insight models, unaffected by ingestion pipeline changes.  