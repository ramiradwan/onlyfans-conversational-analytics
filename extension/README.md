# Browser Extension — OnlyFans Conversational Analytics  
  
Captures OnlyFans chat and engagement events directly from the browser, stores them locally in IndexedDB, and streams them to the backend for real-time analytics.  
  
---  
  
## Components  
  
### **manifest.json**  
Defines extension metadata, permissions, and external connection rules.  
- `host_permissions`: `*://onlyfans.com/*` — required for chat API interception.  
- `externally_connectable`: Restricts backend connection to `http://localhost:8000/*`.  
- `background`: Runs `background.js` as service worker.  
- `content_scripts`: Injects `content.js` at `document_start`.  
- `web_accessible_resources`: Makes `page-hook.js` injectable into OnlyFans pages.  
  
---  
  
### **background.js**  
Passive data capture + backend bridge.  
- **IndexedDB Layer**: `OnlyFansAnalyticsDB` with `chats` and `messages` object stores.  
- **Normalization**: Ensures `id` fields for chats/messages.  
- **Parsers**: Extract structured events from WS and fetch responses.  
- **Backend WS Connection**: Connects to `ws://localhost:8000/api/ws/extension`.  
- **Cache Broadcast**: Sends `{type: "cache_update", chats, messages}` to backend.  
- **Command Handling**: Receives backend commands (`send_message`, `send_ws_message`, `send_fetch_command`) and executes them via fetch or page context.  
  
---  
  
### **content.js**  
Bridge between page context and background.  
- **Page → Background**: Listens for `__OF_FORWARDER__` messages and relays them to `background.js`.  
- **Background → Page**: Forwards backend commands (`__OF_BACKEND__`) to page context.  
- **Injection**: Prepends `page-hook.js` before site scripts for early interception.  
  
---  
  
### **page-hook.js**  
Passive interceptor injected into OnlyFans pages.  
- **WebSocket Hook**: Wraps `window.WebSocket` to capture inbound messages from OF chat servers.  
- **Fetch Hook**: Wraps `window.fetch` to capture requests/responses to targeted API endpoints.  
- **XHR Hook**: Wraps `XMLHttpRequest` for same targeted endpoints.  
- **Backend Commands**: Executes WS sends or fetch commands from backend when allowed.  
  
---  
  
## Data Flow  
  
```mermaid  
flowchart LR  
  subgraph PAGE["OnlyFans Web Page"]  
    PH["page-hook.js"]  
  end  
  
  subgraph EXT["Extension Layer"]  
    CT["content.js"]  
    BG["background.js\nIndexedDB + WS"]  
  end  
  
  subgraph API["FastAPI Backend"]  
    WS["WebSocket Hub (/ws/extension)"]  
    DI["DataIngestService"]  
    ENR["EnrichmentService"]  
    GB["GraphBuilder"]  
    COS["Azure Cosmos DB (Gremlin)"]  
  end  
  
  PH -->|Forward events| CT --> BG  
  BG -->|cache_update| WS --> DI --> ENR --> GB --> COS  
  WS -->|send_command| BG -->|Forward to page| PH  
```  
  
---  
  
## IndexedDB Schema  
  
- **DB Name**: `OnlyFansAnalyticsDB`  
- **Stores**:  
  - `messages` (key: `id`)  
  - `chats` (key: `id`)  
  
---  
  
## Runtime WS Protocol  
  
**Extension → Backend** (`/ws/extension`):  
```json  
{  
  "type": "cache_update",  
  "chats": [ {...}, {...} ],  
  "messages": [ {...}, {...} ]  
}  
```  
  
**Backend → Extension**:  
```json  
{  
  "type": "send_command",  
  "action": "send_message",  
  "chat_id": "12345",  
  "text": "Hello from backend"  
}  
```  
  
---  
  
## Security  
  
- Passive capture only — no DOM modifications except injecting hooks.  
- External connection restricted to local backend origin.  
- No cloud sync — data remains local unless pushed to backend over WS.  
  
---  
  
## Development Notes  
  
- Backend must be running at `http://localhost:8000` for WS connection.  
- The WS URL in `background.js` can be changed for deployed environments.  
- `broadcastCacheUpdate()` can be triggered manually for testing. 

 