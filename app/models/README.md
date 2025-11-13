# Models  
  
Contains all **Pydantic data schemas** for OnlyFans Conversational Analytics.  
  
Models are the **single source of truth** for:  
- REST API request/response bodies  
- WebSocket message payloads  
- Internal service data structures (graph, enrichment, analytics)  
  
All models are **pure data definitions** — no business logic — and include explicit type hints, docstrings, and optional validators/examples for schema clarity.  
  
---  
  
## Structure  
  
### **core.py**  
Base OnlyFans data structures:  
- `UserRef` — Fan/creator profile reference.  
- `Message` — Chat message object with full OnlyFans API field parity.  
- `ChatThread` — Conversation thread with optional embedded `Message` list.  
- `SyncResponse` — REST snapshot response for chats/messages.  
- System payloads: `ConnectionInfo`, `SystemStatus`, `WssError`, `KeepalivePayload`.  
  
### **ingest.py**  
Incoming WebSocket payloads from **Agent ➔ Brain**:  
- `CacheUpdatePayload` — Full snapshot (`cache_update`) with `chats` and `messages` lists. Includes rich `example` metadata for schema generation.  
- `NewRawMessagePayload` — Single delta message (`new_raw_message`) with `message` object.  
  
### **graph.py**  
Labeled Property Graph (LPG) vertices & edges:  
- Vertices: `Fan`, `Creator`, `ConversationNode`, `Topic`, `EngagementAction`, `InteractionOutcome`  
- Edge: `GraphEdge`  
- **New:** `EnrichmentResultPayload` — payload for WS `enrichment_result` messages.  
  
### **insights.py**  
Analytics response models:  
- `TopicMetricsResponse`  
- `SentimentTrendPoint`  
- `SentimentTrendResponse`  
- `ResponseTimeMetricsResponse`  
- `AnalyticsUpdate` — granular metric updates, includes optional `priorityScores` and `unreadCounts`.  
- `FullSyncResponse` — complete snapshot of conversations + analytics.  
  
### **commands.py**  
AI‑generated commands from **Brain ➔ Agent**:  
- `SendMessageCommand` — instructs the Agent to send a message in a chat. Includes `chat_id`, `text`, optional `media_url` with example values.  
  
### **auth.py**  
Authentication data for OnlyFans API:  
- `AuthData` — optional `auth_cookie` for direct API calls.  
  
### **wss.py**  
WebSocket message envelopes with **Pydantic discriminated unions**:  
- **IncomingWssMessage** — `cache_update`, `new_raw_message`, `keepalive`, optional `online_users_update` (presence heartbeat).  
- **OutgoingWssMessage** — `connection_ack`, `system_status`, `system_error`, `full_sync_response`, `append_message`, `analytics_update`, `command_to_execute`, `enrichment_result`, optional `online_users_update`.  
  
Presence payloads now include `description` and `example` metadata for frontend schema generation.  
  
---  
  
## Purpose  
- Enforce **type safety** across REST and WS flows.  
- Provide a **single source of truth** for payload schemas.  
- Enable **auto‑generation** of frontend TypeScript types:  
  - REST: from `/openapi.json` via `@hey-api/openapi-ts`  
  - WS: from `/api/v1/schemas/wss` via `json-schema-to-typescript`  
  
---  
  
## Schema Consistency  
- All WS messages use `IncomingWssMessage` / `OutgoingWssMessage` unions with `Field(discriminator="type")`.  
- Payload models are reused across REST and WS to avoid divergence.  
- Graph, enrichment, and analytics models are isolated but composable.  
- Example values are included where possible to improve generated frontend docs.  
  
---  
  
## Example WS Contract  
  
**Agent ➔ Brain:**  
```json  
{ "type": "cache_update", "payload": { "chats": [...], "messages": [...] } }  
{ "type": "new_raw_message", "payload": { "message": {...} } }  
{ "type": "keepalive", "payload": {} }  
{ "type": "online_users_update", "payload": { "user_ids": [123, 456], "timestamp": "2025-11-08T12:34:56Z" } }  
```  
  
**Brain ➔ Bridge/Agent:**  
```json  
{ "type": "full_sync_response", "payload": { "conversations": [...], "analytics": {...} } }  
{ "type": "append_message", "payload": { "conversationId": "...", ... } }  
{ "type": "analytics_update", "payload": { "topics": [...], ... } }  
{ "type": "enrichment_result", "payload": { "conversation_id": "...", "topics": [...], ... } }  
{ "type": "command_to_execute", "payload": { "chat_id": "...", "text": "..." } }  
{ "type": "online_users_update", "payload": { "user_ids": [123, 456], "timestamp": "2025-11-08T12:34:56Z" } }  
```  