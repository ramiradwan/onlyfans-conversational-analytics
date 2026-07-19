# app/core/config.py

from functools import lru_cache
from pathlib import Path
from typing import Literal
from dotenv import load_dotenv
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

# Load the application-local environment file before constructing settings.
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")


class Settings(BaseSettings):
    app_name: str = "OnlyFans Conversational Analytics"
    environment: str = "development"
    version: str = "0.7.5"
    websocket_auth_mode: str = "development_stub"
    websocket_bind_host: str = "127.0.0.1"
    agent_heartbeat_interval_seconds: int = Field(default=20, gt=0, le=300)
    agent_lease_timeout_seconds: int = Field(default=60, gt=0, le=900)
    canonical_persistence_backend: Literal["memory", "sqlite"] = "memory"
    canonical_database_path: Path = Path("canonical.sqlite3")
    broadcast_url: str = "memory://"

    onlyfans_base_url: str = "https://onlyfans.com/api2/v2"
    onlyfans_auth_cookie: str = ""
    onlyfans_creator_id: str = ""

    extension_id: str = ""

    cosmos_gremlin_uri: str = ""
    cosmos_gremlin_user: str = ""
    cosmos_gremlin_password: str = ""

    nlp_model_path: str = ""
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"

    @model_validator(mode="after")
    def validate_agent_lease_timing(self) -> "Settings":
        if self.agent_heartbeat_interval_seconds >= self.agent_lease_timeout_seconds:
            raise ValueError("Agent heartbeat interval must be less than lease timeout")
        return self

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
