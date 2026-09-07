from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
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
    permissions_path: Path = DEFAULT_PERMISSIONS_PATH
    pairing_bootstrap_token: SecretStr | None = None
    pairing_code_ttl_seconds: int = 600
    pairing_candidate_ttl_seconds: int = 120
    pairing_max_attempts: int = 10
    allow_insecure_remote_http: bool = False

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
