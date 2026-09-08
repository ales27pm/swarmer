from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PERMISSIONS_PATH = Path(__file__).resolve().parents[2] / "configs" / "permissions.yaml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONGARS_", env_file=".env", extra="ignore")

    env: str = "dev"
    host: str = "0.0.0.0"  # nosec B104 - paired phones need the authenticated LAN listener
    port: int = 8710
    db_path: Path = Path("./data/mongars.db")
    workspace_root: Path = Path("./workspace")
    redis_url: str = "redis://127.0.0.1:6379/0"
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
    agent_job_max_attempts: int = Field(default=3, ge=1, le=20)
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
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
