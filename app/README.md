# App  
  
This folder contains the main application code for the OnlyFans Conversational Analytics project.  
  
## Structure  
  
- **main.py** — FastAPI entry point that initializes the API and registers routes  
- **config.py** — Environment variables and configuration (e.g., Cosmos DB connection settings)  
- **models/** — Pydantic models for data validation and type safety  
- **services/** — Business logic and data processing (OnlyFans API client, NLP enrichment, graph building)  
- **routes/** — FastAPI route definitions for API endpoints  
- **utils/** — Helper functions and utilities (logging, time parsing, etc.)  
  
## Notes  
  
The application is built to support:  
- **Creator analytics dashboards** for OnlyFans  
- **Therapy-research-style graph models** stored in Azure Cosmos DB via the Gremlin API  