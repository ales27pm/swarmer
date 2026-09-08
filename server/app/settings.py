from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.message_board import _validate_redis_transport_url

DEFAULT_PERMISSIONS_PATH = Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONGARS_", env_file=".env", extra="ignore")

    env: str = "dev"
    host: str = "0.0.0.0"  # nosec B104 - paired phones need the authenticated LAN listener
    port: int = 8710
    db_path: Path = Path("./data/mongars.db")
    workspace_root: Path = Path("./workspace")
    message_board_backend: Literal["sqlite", "redis"] = "sqlite"
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    redis_stream_prefix: str = Field(default="mongars", min_length=1, max_length=100)
    redis_operation_timeout_seconds: float = Field(default=2.0, ge=0.1, le=30)
    vector_backend: Literal["sqlite", "faiss"] = "sqlite"
    vector_index_path: Path = Path("./data/vector-index")
    outbox_publication_lease_seconds: int = Field(default=30, ge=5, le=900)
    control_plane_heartbeat_seconds: int = Field(default=10, ge=2, le=300)
    maintenance_lease_seconds: int = Field(default=45, ge=10, le=900)
    llm_base_url: str = "http://127.0.0.1:8711/v1"
    orchestrator_model: str = "Hermes-3-Llama-3.2-3B-abliterated"
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    permissions_path: Path = DEFAULT_PERMISSIONS_PATH
    pairing_bootstrap_token: SecretStr | None = None
    pairing_code_ttl_seconds: int = 600
    pairing_candidate_ttl_seconds: int = 120
    pairing_max_attempts: int = 10
    allow_insecure_remote_http: bool = False
    agent_lease_seconds: int = Field(default=60, ge=15, le=900)
    agent_heartbeat_seconds: int = Field(default=20, ge=5, le=300)
    agent_offline_timeout_seconds: int = Field(default=90, ge=15, le=3600)
    agent_job_max_attempts: int = Field(default=3, ge=1, le=20)
    agent_score_refresh_seconds: int = Field(default=60, ge=10, le=3600)
    iphone_capability_grant_ttl_seconds: int = Field(default=90, ge=30, le=300)

    @field_validator("pairing_bootstrap_token")
    @classmethod
    def validate_pairing_bootstrap_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value()
        rejected = {
            "replace-with-a-long-random-operator-token",
            "change-me",
            "changeme",
        }
        if len(secret) < 32 or len(set(secret)) < 12 or secret.lower() in rejected:
            raise ValueError(
                "pairing bootstrap token must be a high-entropy secret of 32+ characters"
            )
        return value

    @model_validator(mode="after")
    def validate_agent_timing(self) -> "Settings":
        if self.agent_heartbeat_seconds >= self.agent_lease_seconds:
            raise ValueError("agent heartbeat interval must be shorter than the lease")
        if self.agent_heartbeat_seconds >= self.agent_offline_timeout_seconds:
            raise ValueError("agent heartbeat interval must be shorter than the offline timeout")
        if self.control_plane_heartbeat_seconds >= self.maintenance_lease_seconds:
            raise ValueError("control-plane heartbeat must be shorter than the maintenance lease")
        if self.message_board_backend == "redis":
            _validate_redis_transport_url(self.redis_url.get_secret_value())
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
