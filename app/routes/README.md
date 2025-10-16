# Routes  
  
Contains **FastAPI route definitions** for the API.  
  
## Components  
  
- **conversations.py** — Endpoints to fetch and process conversations  
- **insights.py** — Endpoints to return analytics metrics (volume, % total, trends, sentiment, response times)  
  
## Purpose  
  
Routes:  
- Accept HTTP requests.  
- Validate parameters.  
- Call the appropriate service functions.  
- Return typed responses using Pydantic models.  