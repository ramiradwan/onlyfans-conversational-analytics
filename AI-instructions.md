# PROJECT GUIDE — OnlyFans Conversational Analytics  
  
This document defines the **source-of-truth overview** for how this project is structured, coded, and extended.    
It is **intended for both humans and AI code generators** to ensure consistent architecture, style, and purpose across the codebase.  
  
---  
  
## PROJECT NAME  
**OnlyFans Conversational Analytics** — A FastAPI + Pydantic application that ingests, enriches, stores, and analyzes creator–fan conversations using a therapy-research-style Labeled Property Graph (LPG) in Azure Cosmos DB (Gremlin API).  
  
---  
  
## PROJECT GOALS  
1. Fetch creator–fan conversations from the OnlyFans API.  
2. Enrich messages with NLP (topic extraction, sentiment, embeddings).  
3. Store enriched data in a **Labeled Property Graph** modeled after psychotherapy research schemas.  
4. Provide analytical endpoints for dashboards (volume, trends, sentiment, engagement metrics).  
5. Maintain a clean, modular architecture that’s easy to extend.  
  
---  
  
## FOLDER STRUCTURE & PURPOSES  
  
### `app/`  
- **Purpose:** Main application code.  
- **Files:**  
  - `main.py`: FastAPI entry point. Registers all routes and configures the app.  
  - `config.py`: Centralized configuration (env vars, DB connection settings).  
- **Subfolders:**  
  - `models/`: Pydantic data schemas.  
  - `services/`: Business logic and data processing.  
  - `routes/`: FastAPI endpoint definitions.  
  - `utils/`: Helper utilities (logging, time parsing, etc.).  
  
### `app/models/`  
- **Purpose:** Define all data models for type safety and validation.  
- **Files:**  
  - `core.py`: Raw message/conversation models before enrichment.  
  - `graph.py`: LPG node and edge models (Fan, Creator, ConversationNode, Topic, EngagementAction, InteractionOutcome, GraphEdge).  
  - `insights.py`: Response models for analytics endpoints.  
- **Style:** Use `BaseModel` from Pydantic. Include type hints and optional fields. Keep models pure (no methods except validators).  
  
### `app/services/`  
- **Purpose:** Implement business logic and data workflows.  
- **Files:**  
  - `onlyfans_client.py`: Handles authenticated calls to the OnlyFans API.  
  - `enrichment.py`: NLP enrichment (sentiment analysis, topic extraction, embeddings).  
  - `graph_builder.py`: Converts enriched conversations into LPG vertices and edges for Cosmos DB.  
  - `insights_service.py`: Executes Gremlin queries to compute analytics metrics.  
- **Style:** Keep functions small and focused. No direct HTTP response handling here — only return Python objects/models.  
  
### `app/routes/`  
- **Purpose:** Define HTTP endpoints and map them to services.  
- **Files:**  
  - `conversations.py`: Endpoints for fetching and processing conversations.  
  - `insights.py`: Endpoints for returning dashboard analytics.  
- **Style:** Validate inputs with Pydantic models. Return typed responses. Handle exceptions with `HTTPException`.  
  
### `app/utils/`  
- **Purpose:** Reusable helper functions.  
- **Files:**  
  - `logger.py`: Configures logging for the app.  
  - `time.py`: Time conversion and formatting helpers.  
- **Style:** Keep utilities stateless and pure.  
  
---  
  
## GRAPH SCHEMA (LPG)  
  
**Vertices:**  
- `Fan(fanId, joinDate, demographics, sentimentProfile)`  
- `Creator(creatorId, niche, styleProfile)`  
- `ConversationNode(conversationId, startDate, endDate, messageCount, averageResponseTime, turns, silencePercentage)`  
- `Topic(topicId, description, embedding, category)`  
- `EngagementAction(actionId, name, embedding, type)`  
- `InteractionOutcome(outcomeId, name, score, date)`  
  
**Edges:**  
- `HAS_CONVERSATION(fan -> conversation)`  
- `DISCUSS_TOPIC(conversation -> topic)`  
- `USES_ENGAGEMENT(conversation -> engagementAction)`  
- `TARGETS_TOPIC(engagementAction -> topic)`  
- `RESULTS_IN_OUTCOME(conversation -> interactionOutcome)`  
- `FOLLOWED_BY(conversation -> conversation)` — chronological linkage.  
  
---  
  
## CODING STYLE  
- **Language:** Python 3.10+  
- **Framework:** FastAPI  
- **Models:** Pydantic BaseModel  
- **Type Safety:** Always use type hints (`List[str]`, `Optional[float]`, etc.)  
- **Error Handling:** Use `HTTPException` in routes. Log errors in services.  
- **Imports:** Use absolute imports (`from app.models.core import Message`) not relative.  
- **Docstrings:** Short docstring for each function explaining purpose.  
- **Function Size:** Keep functions small and focused on one task.  
- **Separation of Concerns:** Routes handle HTTP, Services handle logic, Models handle data structure.  
  
---  
  
## NLP ENRICHMENT PIPELINE  
- **Input:** Raw conversation messages.  
- **Steps:**  
  1. Sentiment analysis (transformer model or spaCy extension).  
  2. Topic extraction (NER or keyword clustering).  
  3. Semantic embeddings (`sentence-transformers`).  
  4. Engagement action classification.  
- **Output:** Enriched data ready for LPG insertion.  
  
---  
  
## GREMLIN QUERY PATTERNS  
- **Precision Engagement Analysis:** Find engagement actions that correlate with positive sentiment for similar topics.  
- **Mechanism of Engagement Change:** Trace sequences of engagement actions over time.  
- **Community Detection:** Louvain clustering on Topics.  
- **Centrality Analysis:** Identify “keystone topics” with high connectivity.  
  
---  
  
## BEST PRACTICES  
- Keep README.md in each folder updated to reflect changes.  
- Use `.env` for secrets (API keys, DB connections).  
- Log all service-level errors for debugging.  
- Write unit tests for services before deploying.  
- Ensure AI-generated code adheres strictly to this architecture.  
  
---  
  
## AI PROMPT USAGE  
When asking AI to generate new code:  
1. **Paste this Project Guide first** in the session.  
2. Then clearly describe the feature/file you want.  
3. AI should:  
   - Place new code in the correct folder.  
   - Follow Pydantic + FastAPI conventions.  
   - Integrate with the LPG graph schema where relevant.  
   - Document code with comments/docstrings.  