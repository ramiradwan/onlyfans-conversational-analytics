# app/core/config.py  
import os  
from functools import lru_cache  
from dotenv import load_dotenv  
from pydantic_settings import BaseSettings  
from pydantic import BaseModel, Field  
  
# Load .env file if present  
load_dotenv()  
  
  
class RedisSettings(BaseModel):  
    """Redis connection configuration for Pub/Sub broadcaster."""  
    url: str = Field(..., description="Redis connection URL (e.g., redis://localhost:6379)")  
  
  
class Settings(BaseSettings):  
    # ------------------------  
    # App Settings  
    # ------------------------  
    app_name: str = "OnlyFans Conversational Analytics"  
    environment: str = os.getenv("ENVIRONMENT", "development")  
  
    # Added per communication-spec — required for connection_ack payload  
    version: str = os.getenv("APP_VERSION", "0.7.0")  
  
    # ------------------------  
    # OnlyFans API  
    # ------------------------  
    onlyfans_base_url: str = "https://onlyfans.com/api2/v2"  
    onlyfans_auth_cookie: str = os.getenv("ONLYFANS_AUTH_COOKIE", "")  
    onlyfans_creator_id: str = os.getenv("ONLYFANS_CREATOR_ID", "")  
  
    # ------------------------  
    # Extension Bridge  
    # ------------------------  
    extension_id: str = os.getenv("EXTENSION_ID", "")  
  
    # ------------------------  
    # Database (Cosmos DB / Gremlin)  
    # ------------------------  
    cosmos_gremlin_uri: str = os.getenv("COSMOS_GREMLIN_URI", "")  
    cosmos_gremlin_user: str = os.getenv("COSMOS_GREMLIN_USER", "")  
    cosmos_gremlin_password: str = os.getenv("COSMOS_GREMLIN_PASSWORD", "")  
  
    # ------------------------  
    # NLP Models  
    # ------------------------  
    nlp_model_path: str = os.getenv("NLP_MODEL_PATH", "")  
    embedding_model_name: str = os.getenv(  
        "EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"  
    )  
  
    # ------------------------  
    # Redis Pub/Sub  
    # ------------------------  
    redis: RedisSettings = RedisSettings(  
        url=os.getenv("REDIS_URL", "redis://localhost:6379")  
    )  
  
    class Config:  
        env_file = ".env"  
        case_sensitive = False  
  
  
@lru_cache()  
def get_settings() -> Settings:  
    """Return cached settings instance."""  
    return Settings()  
  
  
# Global settings object  
settings = get_settings()  