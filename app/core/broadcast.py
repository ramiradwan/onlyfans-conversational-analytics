"""  
Global Redis-backed Pub/Sub broadcaster.  
  
This instance is:  
- Stateless at the application level (safe for multi-worker deployments)  
- Configured from core.config.Settings.redis.url  
- Connected/disconnected via FastAPI startup/shutdown events in main.py  
"""  
  
from broadcaster import Broadcast  
from app.core.config import settings  
  
broadcast: Broadcast = Broadcast(settings.redis.url)  