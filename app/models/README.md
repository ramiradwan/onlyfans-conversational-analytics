# Models  
  
Contains all **Pydantic data schemas** used in the project.  
  
## Structure  
  
- **core.py** — Raw data models representing messages and conversations before enrichment  
- **graph.py** — Labeled Property Graph (LPG) node and edge schemas aligned with the therapy research schema  
- **insights.py** — Response models for analytics endpoints  
  
## Purpose  
  
- Ensure data integrity and validation.  
- Provide typed interfaces between routes and services.  
- Mirror the therapy research graph schema for compatibility with advanced traversal queries.  