import os  
from functools import lru_cache  
from pydantic_settings import BaseSettings  
from dotenv import load_dotenv  
  
# Load .env file if present  
load_dotenv()  
  
class Settings(BaseSettings):  
    # ------------------------  
    # App Settings  
    # ------------------------  
    app_name: str = "OnlyFans Conversational Analytics"  
    environment: str = os.getenv("ENVIRONMENT", "development")  
  
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
  
    class Config:  
        env_file = ".env"  
        case_sensitive = False  
  
@lru_cache()  
def get_settings() -> Settings:  
    """Return cached settings instance."""  
    return Settings()  
  
# Global settings object  
settings = get_settings()  