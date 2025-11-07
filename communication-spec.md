Here is that specification converted to Markdown.

---

# Full-Stack Communication Specification

**TO:** Project Team & AI Code Generators
**FROM:** Principal Software Architect
**DATE:** 2025-11-08
**VERSION:** 1.0.0
**SUBJECT:** Full-Stack Communication Specification for the OnlyFans Conversational Analytics Platform

---

## Introduction

This document is the single source of truth for all data communication and API contracts between the "Brain," "Bridge," and "Agent" actors. It is designed to be 100% consistent with the `AI-instructions.md` project guide and serves as the final blueprint for system-wide communication. All new development or refactoring must adhere to this specification.

## Part 1: The "Brain" (FastAPI Backend) Specification

This section defines the architecture and API for the FastAPI server ("Brain"), which functions as the system's "mission control" for data ingestion, enrichment, and command generation.

### 1.1 Core Communication Protocol: WSS Envelope

A robust communication protocol is foundational to the system's stability. After analysis, a formal JSON-RPC 2.0 protocol has been rejected.1 JSON-RPC is oriented towards client-initiated, action-oriented remote procedure calls.2 This is antithetical to our system's primary requirement, which is event-driven and server-initiated data pushing.3

Instead, all WebSocket communication in both directions must utilize a standardized JSON envelope with a `type` and `payload` structure. This approach is a documented best practice for custom WebSocket protocols, as it provides clarity, maintainability, and easy dispatching.5

To enforce this at the network boundary, a root Pydantic model, `BaseWssMessage`, will be defined in `app/models/core.py`. The main WebSocket endpoint (e.g., in `app/routes/conversations.py`) will parse all incoming messages against this model. This implementation transforms the endpoint into a simple, maintainable dispatcher (e.g., using `match message.type:`), which delegates the actual business logic to the appropriate service.5 This leverages Pydantic's core strength: providing runtime type safety and validation against external data.6

### 1.2 WSS Ingestion Strategy: The Dual-Mode Protocol

The existing architecture's "full-sync-on-every-message" behavior is an unsustainable anti-pattern that creates exponential load. To correct this, the system will implement a **"snapshot-then-delta"** pattern, which is a high-performance standard for real-time systems.8 This strategy, analogous to *"Incremental Hydration"* in modern web frameworks 9, establishes two distinct data ingestion pipelines.

#### 1.2.1 `cache_update` (Full-Sync / Snapshot)

* **Flow:** This message must be sent by the Agent once upon initial WebSocket connection.
* **Payload:** A complete JSON array of all chats and messages from the Agent's local IndexedDB.
* **Brain's Action:** The `app/services/data_ingest.py` service will receive this message and trigger a full-scale ingestion pipeline. Both `app/services/enrichment.py` and `app/services/graph_builder.py` will be invoked to process the entire dataset, potentially rebuilding the graph in Azure Cosmos DB to ensure 100% consistency. Following this, the Brain will broadcast a `full_sync_response` (see Table 1) to the Bridge.

#### 1.2.2 `new_raw_message` (Event-Driven / Delta)

* **Flow:** This message must be sent by the Agent every time a single new message or event is captured.
* **Payload:** A single JSON object representing only the new message.
* **Brain's Action:** The `data_ingest.py` service will route this message to a lightweight ingestion path.

This dual-mode strategy necessitates a critical refactoring of the system's services. The `app/services/graph_builder.py` service must be modified to expose two distinct public methods: `rebuild_graph_from_snapshot(data)` and `append_graph_from_delta(item)`. The `data_ingest.py` service is then responsible for inspecting the WSS message type and invoking the correct service method. This separation is the core mechanism that solves the platform's primary performance bottleneck, ensuring a "delta" message can never accidentally trigger a "snapshot" process.

### 1.3 Command Generation Strategy: Decoupled Event Emitter

The `AI-instructions.md` guide mandates that the Brain's AI/enrichment services (e.g., `enrichment.py`) can generate commands (e.g., "send this message") that are ultimately executed by the Agent. These services must not be directly coupled to the WebSocket network logic.

To solve this, the Brain will implement an internal publish/subscribe (Pub/Sub) system, fully decoupling the business logic (the "publisher") from the network broadcast logic (the "subscriber").

* **Connection Management:** A singleton `ConnectionManager` class must be implemented, following standard FastAPI patterns for tracking active WebSocket connections.11
* **Pub/Sub Emitter:** A robust, `asyncio`-compatible Pub/Sub library, such as `fastapi-websocket-pubsub` 12 or a system built on `broadcaster` 12 or `Redis Pub/Sub` 13, will be integrated.
* **Command Flow:**
    1.  An AI service in `app/services/enrichment.py` generates a command.
    2.  It publishes this command to an internal topic, (e.g., `await pubsub.publish("command_topic", payload)`).12 The service has zero awareness of the WebSocket layer.
    3.  The WebSocket endpoint (`app/routes/conversations.py`) subscribes to `"command_topic"`.
    4.  Upon receiving an event, the `ConnectionManager` formats it as a `command_to_execute` message (see Table 1) and broadcasts it to the appropriate Bridge client.

This decoupled architecture is essential for scalability and testability. Business logic in `app/services/` can be unit-tested in isolation without mocking network connections. Furthermore, choosing a `Redis-backed Pub/Sub` implementation 14 ensures the system can scale horizontally across multiple Uvicorn workers or containers. An in-memory-only `ConnectionManager` 11 cannot support horizontal scaling, as a broadcast message would only be delivered to clients connected to the same worker process that received the publish event, failing to reach all other clients.15

### 1.4 API Contract 1: Brain-Bridge WSS Contract (Server ➔ Client)

This table formally defines the API of all messages sent from the Brain to the Bridge. This contract is the "source of truth" for the Pydantic models in `app/models/` and the manually-defined TypeScript types in the frontend.

| Message Type (`type`) | Payload Schema (Pydantic Model) | Direction | Description |
| :--- | :--- | :--- | :--- |
| `connection_ack` | `models.core.ConnectionInfo` | Brain ➔ Bridge | Sent immediately on successful WSS connection. Confirms connection and provides system version. |
| `system_status` | `models.core.SystemStatus` | Brain ➔ Bridge | Broadcasts the current status of the Brain (e.g., "PROCESSING\_SNAPSHOT", "REALTIME"). |
| `full_sync_response` | `models.insights.FullSyncResponse` | Brain ➔ Bridge | (Snapshot) A complete snapshot of all conversations, analytics, and graph data. Sent once after processing a `cache_update`. |
| `append_message` | `models.graph.ConversationNode` | Brain ➔ Bridge | (Delta) A single new or updated conversation node. Sent after processing a `new_raw_message`. |
| `analytics_update` | `models.insights.AnalyticsUpdate` | Brain ➔ Bridge | (Delta) A granular update to an analytics metric (e.g., sentiment). |
| `command_to_execute` | `models.commands.SendMessageCommand` | Brain ➔ Bridge | An AI-generated command to be proxied to the Agent (e.g., "send this specific message"). |
| `system_error` | `models.core.WssError` | Brain ➔ Bridge | Reports a server-side processing error to the frontend. |

## Part 2: The "Bridge" (React Frontend) Specification

This section defines the architecture for the React frontend ("Bridge"), which acts as the resilient translator, state manager, and user interface for the system.

### 2.1 Type-Safety and Synchronization Strategy

The Bridge must maintain 100% type-safety with the Brain's API. A dual-strategy is required to achieve this.

#### 2.1.1 REST API (OpenAPI)

For all standard HTTP endpoints, the project must auto-generate TypeScript types.

* **Tooling:** A generator such as `openapi-typescript` or the more modern `hey-api/openapi-ts` 16 will be used.
* **Workflow:** A `package.json` script (e.g., `npm run sync:types`) must be implemented. This script will fetch the schema from the running backend (e.g., `http://localhost:8000/openapi.json`) and generate the corresponding TypeScript interfaces.18 This automated process is a "best practice" for eliminating schema drift and avoiding the "maintenance nightmare" of manually syncing API types.19

#### 2.1.2 WebSocket API (Manual)

A critical limitation of the OpenAPI specification is that it does not and cannot include schemas for WebSocket messages.5 This is a "critical gap" that automation cannot solve.

* **Specification:** A manual TypeScript definition file, `frontend/src/types/websocket.ts`, must be created and maintained.
* **Content:** This file will define the `WssMessage` envelope and a specific TypeScript interface for each payload specified in Table 1 (e.g., `FullSyncResponsePayload`, `AppendMessagePayload`).
* **Risk:** This manual file is the primary point of potential schema drift between the backend and frontend. All development processes must enforce that any change to a WSS-related Pydantic model in the Brain is immediately and manually reflected in this file.

### 2.2 State Management Architecture: Zustand

The system requires a state manager capable of handling a large, streaming dataset.

* **Architectural Decision:** `Zustand` is the optimal choice for this use case.22
* **Justification:** The application's state is not atomic; it is a single, large, relational dataset (the list of chats) managed from a "top-down" source (the WebSocket). `Zustand`'s "top-down" single-store model is a perfect architectural fit for this data flow, whereas a "bottom-up" atomic model like `Jotai` would be inappropriate.24 Furthermore, `Zustand` is highly performant for frequent WebSocket updates, as components only subscribe to the slices of state they select, preventing a flood of `append_message` events from causing application-wide re-renders.25
* **Specification:**
    * A single global store, `frontend/src/store/useChatStore.ts`, will be created.
    * This store will hold the connection status (`readyState`), the list of conversations (`conversations: ConversationNode`), and all computed analytics (`insights: AnalyticsUpdate`).
    * The store itself must contain the message-handling logic (e.g., `handleWssMessage(message)`), which will implement the state updates for `full_sync_response` (replace state) and `append_message` (append to state).

### 2.3 Resilient Communication Hooks

The Bridge must be resilient to network failures and extension errors. Building this logic from scratch is unnecessary and error-prone.26

#### 2.3.1 `useSocket` Hook

A custom hook, `frontend/src/hooks/useSocket.ts`, will be implemented using the `react-use-websocket` library.27

* **Configuration:** It must be configured for automatic reconnection with an exponential backoff strategy.27
* **Responsibility:** This hook's sole responsibility is managing the WSS connection. It must not contain any application state. It will route all incoming messages directly to the Zustand store for processing, e.g., `onMessage: (event) => useChatStore.getState().handleWssMessage(event.data)`.

#### 2.3.2 `useExtension` Service

* **Implementation:** A TypeScript service module, `frontend/src/services/extension.ts`, must be created. This module will be the only part of the application authorized to call `chrome.runtime.sendMessage`. Modern `chrome.runtime` APIs are Promise-based.30 This service will wrap all calls in Promises, providing a clean `async/await` interface to the rest of the application (e.g., `await extension.getAllChats()`).
* **Error Handling:** A critical function of this wrapper is robust error handling. The Promise resolver must check for the existence of `chrome.runtime.lastError`.31 If this object is present, the Promise must be rejected with the error message. This practice is essential to prevent silent failures, which are common when `lastError` is not checked.33

### 2.4 API Contract 2: Bridge-Agent Contract (Client ➔ Extension)

This table defines the API for communication from the React app (web page) to the extension's `background.js` script. This communication relies on the `externally_connectable` key in `manifest.json` 35 and the `chrome.runtime.onMessageExternal` listener in the Agent's background script.38

| Message Type (`type`) | Payload Schema (TypeScript) | Direction | Description |
| :--- | :--- | :--- | :--- |
| `get_all_chats_from_db` | `null` | Bridge ➔ Agent | Sent on app load to request the initial data hydration. Triggers the Agent to start the "Snapshot" sync. |
| `get_all_messages_from_db` | `{ chatId: string }` | Bridge ➔ Agent | (Per `AI-instructions.md`) Requests all messages for a specific chat. Note: May be deprecated by `get_all_chats_from_db`. |
| `execute_agent_command` | `SendMessageCommand` | Bridge ➔ Agent | (Command Proxy) Proxies a command received from the Brain (from Table 1) to the Agent for execution on the page. |

## Part 3: The "Agent" (Chrome Extension) Specification

This section defines the architecture for the browser extension ("Agent"), which acts as the data source and command executor.

### 3.1 Data Capture Architecture: Page-Hook Injection

A common approach for network interception is the `chrome.webRequest` API. However, this API is insufficient for this project's needs. The `webRequest` API cannot intercept the content of individual WebSocket messages (only the initial handshake) 39 and cannot read the response bodies of `fetch` or `XHR` requests.40

Therefore, the `page-hook.js` (monkey-patching) approach described in `AI-instructions.md` is mandatory.41 This architecture is implemented as a 3-file-chain due to Chrome's security boundaries.42

* **`extension/page-hook.js`:** This script is injected into the main "page world." It must monkey-patch `window.fetch`, `window.WebSocket`, and `XMLHttpRequest.prototype.open/send` to capture traffic. On capture, it sends the data to `content.js` using `window.postMessage` with the `__OF_FORWARDER__` type (see Table 3).
* **`extension/content.js`:** This script runs in an "isolated world" 42 and cannot access the page's JavaScript. It must have a `window.addEventListener('message',...)` to listen only for `__OF_FORWARDER__` messages (see Table 3). Upon receipt, it immediately forwards the data to `background.js` using `chrome.runtime.sendMessage`.30
* **`extension/background.js`:** This is the extension's service worker. It must have a `chrome.runtime.onMessage` listener 30 to receive data from `content.js`. All logic for saving to `IndexedDB` and forwarding to the Brain resides here.

This chain is a non-negotiable consequence of the extension security model. The `page-hook` has data access but no API access. The `background.js` has API access (e.g., `chrome.storage`, WSS) but no page data access. The `content.js` is the mandatory, low-logic "message bridge" between them.

### 3.2 Data Forwarding & Sync Strategy (Dual-Mode)

The Agent's `background.js` is the originator of the Dual-Mode Ingestion protocol defined in section 1.2.

#### Snapshot Flow (Pull-to-Push)

1.  The `background.js` script receives the `get_all_chats_from_db` message from the Bridge via `onMessageExternal` (see Table 2).
2.  It must query its entire `IndexedDB`.44
3.  It must format this data into a single message with the type `cache_update`: `{"type": "cache_update", "payload": [...]}`.
4.  It must send this single large message over the WebSocket connection to the Brain.

#### Delta Flow (Event-Driven)

1.  The `background.js` script receives a single captured event from `content.js` via `onMessage`.
2.  It must save this single event to its `IndexedDB`.44
3.  It must format this event with the type `new_raw_message`: `{"type": "new_raw_message", "payload": {...}}`.
4.  It must send this message over the WSS connection to the Brain.

This logic solves the project's core performance problem. The Agent's `IndexedDB` 44 serves as the local "source of truth" and a "store-and-forward" outbox. The Brain's `Cosmos DB` becomes an eventually consistent replica. This design also rigidly enforces the 3-Actor security boundary, as only the Agent's `background.js` ever initiates communication with the Brain.

### 3.3 Command Execution Architecture

This flow is the exact reverse of the data capture architecture (3.1) and is how the Brain's AI-generated commands are executed on the page.

1.  **`extension/background.js`:** Receives the `execute_agent_command` from the Bridge (see Table 2). It must then use `chrome.tabs.sendMessage` 43 to send this command to the `content.js` script running in the active OnlyFans tab.
2.  **`extension/content.js`:** Receives the command from `background.js` via `chrome.runtime.onMessage`. It must then use `window.postMessage` to inject this command into the page's main world, using the `__OF_BACKEND__` type (see Table 3).
3.  **`extension/page-hook.js`:** This script must also have a `window.addEventListener('message',...)` listening only for `__OF_BACKEND__` messages (see Table 3). When it receives a command, it must translate that JSON command into an actual `fetch` or `WebSocket.send` call within the page's context, thereby emulating the user and executing the Brain's command.

The `page-hook.js` thus serves as a bi-directional "adapter." It adapts page-native events into JSON for the extension, and it adapts JSON commands from the extension back into page-native actions. This completes the full, 3-actor communication loop.

### 3.4 API Contract 3: Page-Agent Contract (Internal)

This table defines the internal `window.postMessage` API between `page-hook.js` (Page World) and `content.js` (Isolated World). This contract is critical for stability and security. The target webpage (`onlyfans.com`) may use `postMessage` for its own features. We must use unique, namespaced type keys (e.g., `__OF_FORWARDER__`) to prevent message collisions and ensure our listeners only process messages originating from our own scripts.

| Direction | Event Type (`data.type`) | Payload Schema (`data.payload`) | Purpose |
| :--- | :--- | :--- | :--- |
| Page ➔ Agent | `__OF_FORWARDER__` | `{ "event": "fetch" \| "websocket" \| "xhr", "data":... }` | Sent by `page-hook.js` to `content.js`, forwarding a captured network event. |
| Agent ➔ Page | `__OF_BACKEND__` | `{ "command": "send_message", "payload":... }` | Sent by `content.js` to `page-hook.js`, instructing the page to execute a command. |
