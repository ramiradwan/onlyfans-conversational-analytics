# Models  
  
Contains all **Pydantic data schemas** for OnlyFans Conversational Analytics.  
  
Models are the **single source of truth** for:  
- REST API request/response bodies  
- WebSocket message payloads  
- Internal service data structures (graph, enrichment, analytics)  
  
All models are pure data definitions — no business logic — and include explicit type hints and optional validators.  
  
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
- `CacheUpdatePayload` — Full snapshot (`cache_update`).  
- `NewRawMessagePayload` — Single delta message (`new_raw_message`).  
  
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
  
### **commands.py**  
AI‑generated commands from **Brain ➔ Agent**:  
- `SendMessageCommand` — instructs the Agent to send a message in a chat.  
  
### **auth.py**  
Authentication data for OnlyFans API:  
- `AuthData` — optional `auth_cookie` for direct API calls.  
  
### **wss.py**  
WebSocket message envelopes with **Pydantic discriminated unions**:  
- **IncomingWssMessage** — `cache_update`, `new_raw_message`, `keepalive`  
- **OutgoingWssMessage** — `connection_ack`, `system_status`, `system_error`, `full_sync_response`, `append_message`, `analytics_update`, `command_to_execute`, `enrichment_result`  
  
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
  
---  
  
## Example WS Contract  
  
**Agent ➔ Brain:**  
```json  
{ "type": "cache_update", "payload": { "chats": [...], "messages": [...] } }  
{ "type": "new_raw_message", "payload": { "message": {...} } }  
{ "type": "keepalive", "payload": {} }  
```
  
**Brain ➔ Bridge/Agent:**
```json  
{ "type": "full_sync_response", "payload": { "chats": [...], "messages": [...] } }  
{ "type": "append_message", "payload": { "conversationId": "...", ... } }  
{ "type": "analytics_update", "payload": { "topics": [...], ... } }  
{ "type": "enrichment_result", "payload": { "conversation_id": "...", "topics": [...], ... } }  
{ "type": "command_to_execute", "payload": { "chat_id": "...", "text": "..." } }  
```