# app/core/config.py  
  
import os  
from functools import lru_cache  
from pathlib import Path  
from dotenv import load_dotenv  
from pydantic_settings import BaseSettings  
from pydantic import BaseModel, Field  
  
# Load .env explicitly so values are in os.environ before BaseSettings reads them  
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")  
  
  
class RedisSettings(BaseModel):  
    url: str = Field(..., description="Redis connection URL")  
  
  
class Settings(BaseSettings):  
    app_name: str = "OnlyFans Conversational Analytics"  
    environment: str = "development"  
    version: str = "0.7.5"  
  
    onlyfans_base_url: str = "https://onlyfans.com/api2/v2"  
    onlyfans_auth_cookie: str = ""  
    onlyfans_creator_id: str = ""  
  
    extension_id: str = ""  
  
    cosmos_gremlin_uri: str = ""  
    cosmos_gremlin_user: str = ""  
    cosmos_gremlin_password: str = ""  
  
    nlp_model_path: str = ""  
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"  
  
    redis: RedisSettings = RedisSettings(  
        url=os.getenv("REDIS_URL", "redis://localhost:6379")  
    )  
  
    class Config:  
        env_file = ".env"  
        case_sensitive = False  
  
  
@lru_cache()  
def get_settings() -> Settings:  
    return Settings()  
  
  
settings = get_settings()  