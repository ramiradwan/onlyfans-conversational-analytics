# app/core/config.py

from functools import lru_cache
import re
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.runtime_paths import runtime_configuration_file


RESERVED_CONFIGURATION_PREFIX = "replace-with-"
CHROME_EXTENSION_ID_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    app_name: str = "OnlyFans Conversational Analytics"
    environment: str = "development"
    version: str = "0.7.5"
    websocket_auth_mode: Literal["development_stub", "local_session"] = "local_session"
    websocket_bind_host: str = "127.0.0.1"
    agent_heartbeat_interval_seconds: int = Field(default=20, gt=0, le=300)
    agent_lease_timeout_seconds: int = Field(default=60, gt=0, le=900)
    # The shipped runtime is local-first and durable. Tests must opt in to the
    # disposable backend explicitly (see tests/conftest.py).
    canonical_persistence_backend: Literal["memory", "sqlite"] = "sqlite"
    auth_database_path: Path = Path("auth.sqlite3")
    canonical_database_path: Path = Path("canonical.sqlite3")
    projection_database_path: Path = Path("projections.sqlite3")
    # The protocol-v2 read model above and the rebuildable analytics
    # projection store below are different schemas and must never share a
    # file; a shared file causes a migration checksum error on open.
    analytics_projection_database_path: Path = Path("analytics-projections.sqlite3")
    security_signing_secret: SecretStr = SecretStr(
        "onlyfans-local-development-signing-secret"
    )
    webauthn_challenge_ttl_seconds: int = Field(default=300, gt=0, le=600)
    csrf_token_ttl_seconds: int = Field(default=8 * 60 * 60, gt=0, le=24 * 60 * 60)
    bridge_session_ttl_seconds: int = Field(default=8 * 60 * 60, gt=0, le=24 * 60 * 60)
    bridge_ticket_ttl_seconds: int = Field(default=8 * 60 * 60, gt=0, le=24 * 60 * 60)
    agent_pairing_ticket_ttl_seconds: int = Field(default=120, gt=0, le=600)
    agent_reconnect_ticket_ttl_seconds: int = Field(default=30 * 24 * 60 * 60, gt=0)
    agent_config_ticket_ttl_seconds: int = Field(default=60 * 60, gt=0, le=24 * 60 * 60)
    bridge_session_cookie_name: str = "__Host-bridge_session"
    bridge_origin: str = "http://bridge.localhost:17871"
    local_session_bootstrap_token: SecretStr = SecretStr("")
    local_principal_id: str = ""
    local_creator_account_id: str = ""
    local_platform_creator_id: str = ""
    local_bridge_role: Literal["creator", "operator"] = "creator"
    identity_binding_source: Literal["development", "verified_grants"] = "development"
    verified_grant_bundle_sha256: str = ""
    development_platform_creator_id: str = "dev-platform-creator"
    broadcast_url: Literal["memory://"] = "memory://"

    extension_id: str = ""

    nlp_model_path: str = ""
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"

    @model_validator(mode="after")
    def validate_agent_lease_timing(self) -> "Settings":
        if self.agent_heartbeat_interval_seconds >= self.agent_lease_timeout_seconds:
            raise ValueError("Agent heartbeat interval must be less than lease timeout")
        production_mode = self.environment.lower() not in {
            "development", "dev", "local", "test"
        }
        if production_mode and self.websocket_auth_mode != "local_session":
            raise ValueError(
                "Production requires TPM-gated local session authentication"
            )
        signing_secret = self.security_signing_secret.get_secret_value()
        if production_mode and (
            signing_secret == "onlyfans-local-development-signing-secret"
            or len(signing_secret) < 32
            or signing_secret.startswith(RESERVED_CONFIGURATION_PREFIX)
        ):
            raise ValueError(
                "A generated security signing secret of at least 32 characters "
                "is required outside local development"
            )
        if self.websocket_bind_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("The local Brain runtime must bind to a loopback host")
        if self.websocket_auth_mode == "local_session":
            bootstrap = self.local_session_bootstrap_token.get_secret_value()
            if (
                len(bootstrap) < 32
                or bootstrap.startswith(RESERVED_CONFIGURATION_PREFIX)
            ):
                raise ValueError(
                    "Local session mode requires a generated launcher bootstrap "
                    "token of at least 32 characters"
                )
            bindings = (
                self.local_principal_id,
                self.local_creator_account_id,
                self.local_platform_creator_id,
            )
            if not all(bindings) or any(
                value.startswith(RESERVED_CONFIGURATION_PREFIX)
                for value in bindings
            ):
                raise ValueError(
                    "Local session mode requires exact non-placeholder principal, "
                    "Brain account, and platform creator bindings"
                )
            if production_mode and (
                self.identity_binding_source != "verified_grants"
                or re.fullmatch(
                    r"[0-9a-f]{64}", self.verified_grant_bundle_sha256
                )
                is None
            ):
                raise ValueError(
                    "Production identity bindings must come from a verified grant bundle"
                )
            if production_mode and (
                len(self.extension_id) != CHROME_EXTENSION_ID_LENGTH
                or not self.extension_id.islower()
                or any(character < "a" or character > "p" for character in self.extension_id)
            ):
                raise ValueError(
                    "Production local session mode requires an exact Chrome extension ID"
                )
        return self

@lru_cache()
def get_settings() -> Settings:
    runtime_file = runtime_configuration_file()
    development_file = Path(__file__).parent.parent / ".env"
    if runtime_file.exists():
        values = dotenv_values(runtime_file, interpolate=False)
        return Settings(
            _env_file=None,
            **{
                key.lower(): value
                for key, value in values.items()
                if value is not None
            },
        )
    return Settings(_env_file=development_file)


settings = get_settings()
