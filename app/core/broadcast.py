# app/core/broadcast.py  
from broadcaster import Broadcast  
from app.core.config import settings  
  
# Global stateless Pub/Sub backend  
broadcast = Broadcast(settings.redis.url)  