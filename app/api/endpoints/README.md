# API Endpoints  
  
Contains **FastAPI route definitions** for the API layer of OnlyFans Conversational Analytics.  
  
## Components  
  
### **frontend.py**  
Serves the compiled **Vite + React** frontend via Jinja2 templates, injecting runtime configuration:  
  
- `EXTENSION_ID` from `.env`  
- `FASTAPI_WS_URL` for WebSocket bridge (computed from `request.base_url`)  
- Entry JS/CSS paths from Vite `manifest.json`  
  
**Path(s):**  
- `GET /` — Returns `index.html` with injected config & asset links.  
  
### **insights.py**  
REST analytics endpoints returning aggregated conversational metrics:  
  
- `/api/v1/insights/topics` — Volume, % total, and trend for each topic.  
- `/api/v1/insights/sentiment-trend` — Average sentiment score trend over time.  
- `/api/v1/insights/response-time` — Average handling time (AHT), silence percentage, and turns.  
  
### **websocket.py**  
Unified WebSocket hub for **real‑time ingestion and broadcasting** using Redis Pub/Sub and type‑safe Pydantic unions:  
  
- `/ws/extension/{user_id}` — **Agent (Chrome extension)** → Brain ingestion pipeline.  
  - Receives:  
    - `cache_update` (full snapshot from IndexedDB)  
    - `new_raw_message` (delta event)  
    - `keepalive` (MV3 service worker persistence ping)  
  - Publishes processed data via Redis Pub/Sub.  
- `/ws/frontend/{user_id}` — **Bridge (React dashboard)** live updates.  
- `/ws/chatwoot/{user_id}` — External integration channel.  
  
### **schema.py**  
Schema exposure for **auto‑generated frontend WebSocket types**:  
  
- `/api/v1/schemas/wss` — Returns JSON Schema for `OutgoingWssMessage`.  
  - Used by frontend build scripts to generate TypeScript WS types automatically (`json-schema-to-typescript`).  
  
## Purpose  
  
Endpoints:  
- Accept HTTP or WebSocket requests.  
- Validate all parameters and payloads with Pydantic models (`IncomingWssMessage`, `OutgoingWssMessage`).  
- Call appropriate service functions:  
  - `DataIngestService.handle_snapshot()` / `.handle_delta()` for ingestion.  
  - Analytics services for insight metrics.  
  - Enrichment and graph building for conversation data.  
- Return typed responses, ensuring consistent schema across REST and WS.  
- WS errors are sent via `system_error` payload.  
  
## Schema Consistency  
  
- All real‑time conversation data is sent via WS using `OutgoingWssMessage` types as per spec.  
- Snapshot (`cache_update`) and delta (`new_raw_message`) flows share the same core models for chats/messages.  
- Frontend TypeScript types for WS are auto‑generated from `/api/v1/schemas/wss` — ensuring exact parity between backend and frontend payloads.  
- Analytics routes return isolated insight models (`FullSyncResponse`, `AnalyticsUpdate`), unaffected by ingestion pipeline changes.  
  
## WebSocket Ingestion Pipeline  
  
```mermaid  
flowchart LR
    subgraph EXT [Agent: Chrome Extension]
        BGSW[background.js - Service Worker]
        IDXDB[IndexedDB Snapshot/Deltas]
    end

    subgraph BE [Brain: FastAPI Backend]
        WSE[ws/extension/:user_id]
        DISVC[DataIngestService]
        REDIS[(Redis Pub/Sub broadcaster)]
        WSF[ws/frontend/:user_id]
    end

    subgraph FE [Bridge: React Frontend Dashboard]
        STORE[Zustand Store]
        UI[UI Components]
    end

    BGSW -->|cache_update / new_raw_message / keepalive| WSE
    WSE --> DISVC
    DISVC -->|full_sync_response / append_message| REDIS
    REDIS --> WSF
    WSF --> STORE
    STORE --> UI

```
  
## REST Analytics Pipeline  
  
```mermaid  
flowchart LR
    subgraph FE [Bridge: React Frontend Dashboard]
        CHARTS[Charts & Metrics UI]
    end

    subgraph BE [Brain: FastAPI Backend]
        INSIGHTS[api/v1/insights/* endpoints]
        ANALYTICS[Insights Service Layer]
        DB[(Analytics DB / Cache)]
    end

    CHARTS -->|HTTP GET| INSIGHTS
    INSIGHTS --> ANALYTICS
    ANALYTICS --> DB
    DB --> ANALYTICS
    ANALYTICS --> INSIGHTS
    INSIGHTS --> CHARTS

```

## Removed Legacy Endpoints  
  
As part of the [refactor](https://github.com/ramiradwan/onlyfans-conversational-analytics/issues/1)  
- **`conversations.py`** (legacy REST ingestion/cache routes) was removed.  
- All ingestion now happens over WebSocket, ensuring race‑condition safe processing and stateless backend operation.  
  
---  