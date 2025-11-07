# Services  
  
Contains the **business logic** and **data processing pipelines** for OnlyFans Conversational Analytics.  
  
---  
  
## Components  
  
### **data_ingest.py**  
Manages ingestion of chat/message data from the browser extension or OnlyFans API.    
**Responsibilities:**  
- Store latest chats/messages in an in-memory cache.  
- Convert raw dict payloads into validated Pydantic models.  
- Provide raw dicts (for API clients) and typed models (for routes & WebSocket hub).  
- Used by:  
  - `/from-extension` route  
  - `/sync` route  
  - WebSocket hub (`/ws/extension`)  
  
**Key Methods:**  
- `update_cache(chats, messages)` — Saves raw payloads.  
- `parse_chats(raw_chats)` — Returns validated `ChatThread` models.  
- `parse_messages(raw_messages)` — Returns validated `Message` models.  
- `get_all_chats_from_db(limit, offset)` — Raw chat dicts (IndexedDB).  
- `get_all_messages_from_db(chat_id, limit, offset)` — Raw message dicts (IndexedDB).  
- `get_cached_chats()` / `get_cached_messages()` — Typed models from cache.  
  
---  
  
### **onlyfans_client.py**  
Facade for retrieving OnlyFans chat data.    
**Preferred source:** Browser extension cache via `DataIngestService`.    
**Legacy:** Placeholder for future direct API calls.  
  
**Key Methods:**  
- `get_chats(limit, offset)` — Returns validated `ChatThread` models from cache.  
- `get_messages(chat_id, limit, offset)` — Returns validated `Message` models from cache.  
- `get_chat_with_messages(chat_id, message_limit)` — Returns a single chat thread with its messages attached.  
  
**Fix Applied:**    
Now calls `DataIngestService.parse_chats()` and `.parse_messages()` — removing old non-existent method names.  
  
---  
  
### **enrichment.py**  
NLP enrichment pipeline.    
Processes validated conversation models to extract:  
- Topics (NER, keyword clustering, embeddings)  
- Engagement actions (message type classification)  
- Sentiment analysis  
- Interaction outcomes (tips, renewals, drop-offs)  
  
**Key Method:**  
- `enrich_conversation(chat_thread)` — Returns a dict of topics, actions, sentiment, and outcomes.  
  
---  
  
### **graph_builder.py**  
Converts enriched conversation data into LPG vertices and edges for Cosmos DB (Gremlin API).    
Matches the **therapy-research-style schema** defined in `AI-instructions.md`.  
  
**Produces:**  
- Vertices: `Fan`, `Creator`, `ConversationNode`, `Topic`, `EngagementAction`, `InteractionOutcome`  
- Edges: `HAS_CONVERSATION`, `DISCUSS_TOPIC`, `USES_ENGAGEMENT`, `TARGETS_TOPIC`, `RESULTS_IN_OUTCOME`  
  
**Key Method:**  
- `build_graph(enriched_conv, fan_id)` — Returns `{ "vertices": [...], "edges": [...] }`  
  
---  
  
### **insights_service.py**  
Analytics layer — executes Gremlin traversals against Azure Cosmos DB to compute metrics for dashboard endpoints.  
  
**Computes:**  
- Topic metrics (volume, % of total, trend)  
- Sentiment trend (average sentiment over time)  
- Response time metrics (AHT, silence %, turns)  
  
**Key Methods:**  
- `fetch_topic_metrics(start_date, end_date)`  
- `fetch_sentiment_trend(start_date, end_date)`  
- `fetch_response_time_metrics(start_date, end_date)`  
  
---  
  
## Purpose  
  
Services separate **data processing logic** from API endpoint definitions, keeping the architecture clean and testable.    
They:  
- Accept raw inputs (from extension, API, cache)  
- Perform validation, normalization, enrichment, and transformation  
- Return typed models or structured results to routes and WebSocket hub  
  
---  
  
## Schema Consistency  
  
- All conversation data flows through:  
  **`DataIngestService` → `EnrichmentService` → `GraphBuilder`**  
- WebSocket hub and REST routes (`/sync`, `/from-extension`) share identical typed outputs (`SyncResponse`).  
- Analytics services (`insights_service.py`) are isolated from ingestion pipeline changes.  
  
---  
  
## Example Data Flow  
  
```mermaid  
flowchart LR  
    EXT[Browser Extension IndexedDB] -->|Raw chats/messages| INGEST[DataIngestService]  
    INGEST -->|Typed models| ENRICH[EnrichmentService]  
    ENRICH --> GRAPH[GraphBuilder]  
    GRAPH --> COSMOS[Azure Cosmos DB (Gremlin)]  
    COSMOS --> INSIGHTS[Insights Service]  
    INSIGHTS --> ROUTES[API Routes: /topics, /sentiment-trend, /response-time]  
    INGEST --> WS[WebSocket Hub: cache_update]  
    INGEST --> ROUTES_SYNC[API Routes: /sync]  
```