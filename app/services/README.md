# Services  
  
Contains the **business logic** and **data processing pipelines**.  
  
## Components  
  
- **onlyfans_client.py** — Handles API calls to fetch raw messages/conversations  
- **enrichment.py** — NLP processing (sentiment analysis, topic extraction, embeddings)  
- **graph_builder.py** — Converts enriched data into LPG nodes and edges ready for Cosmos DB Gremlin  
- **insights_service.py** — Runs Gremlin queries to compute metrics for dashboard endpoints  
  
## Purpose  
  
Services separate **data processing logic** from API endpoint definitions, keeping the architecture clean and testable.  