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
    redis_stream_maxlen: int = Field(default=10_000, ge=1, le=10_000_000)
    redis_stream_retention_seconds: int = Field(default=604_800, ge=60, le=31_536_000)
    vector_backend: Literal["sqlite", "faiss"] = "sqlite"
    vector_index_path: Path = Path("./data/vector-index")
    vector_index_generations_to_keep: int = Field(default=2, ge=1, le=50)
    outbox_publication_lease_seconds: int = Field(default=30, ge=5, le=900)
    control_plane_heartbeat_seconds: int = Field(default=10, ge=2, le=300)
    maintenance_lease_seconds: int = Field(default=45, ge=10, le=900)
    llm_base_url: str = "http://127.0.0.1:8711/v1"
    orchestrator_model: str = "Hermes-3-Llama-3.2-3B-abliterated"
    planner_model: str | None = None
    evaluator_model: str | None = None
    summarizer_model: str | None = None
    synthesizer_model: str | None = None
    goal_max_steps: int = Field(default=20, ge=1, le=20)
    goal_max_replans: int = Field(default=3, ge=0, le=10)
    goal_max_parallelism: int = Field(default=3, ge=1, le=3)
    goal_max_runtime_seconds: int = Field(default=1_800, ge=30, le=86_400)
    goal_max_model_calls: int = Field(default=30, ge=1, le=100)
    goal_model_call_lease_seconds: int = Field(default=120, ge=30, le=900)
    goal_context_max_tokens: int = Field(default=2_048, ge=64, le=32_768)
    goal_context_max_memory_items: int = Field(default=6, ge=0, le=100)
    goal_context_max_episode_items: int = Field(default=4, ge=0, le=100)
    goal_context_max_agent_cards: int = Field(default=6, ge=0, le=64)
    goal_context_max_upstream_results: int = Field(default=8, ge=0, le=20)
    goal_context_max_result_chars_per_node: int = Field(default=2_000, ge=0, le=100_000)
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    project_embedding_base_url: str | None = None
    project_embedding_model: str | None = None
    project_embedding_model_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    project_memory_query_prefix: str = Field(default="", max_length=200)
    project_memory_document_prefix: str = Field(default="", max_length=200)
    permissions_path: Path = DEFAULT_PERMISSIONS_PATH
    pairing_bootstrap_token: SecretStr | None = None
    pairing_code_ttl_seconds: int = 600
    pairing_candidate_ttl_seconds: int = 120
    pairing_max_attempts: int = 10
    websocket_io_timeout_seconds: float = Field(default=5.0, ge=0.1, le=30)
    websocket_notification_poll_seconds: float = Field(default=0.1, ge=0.01, le=5)
    websocket_notification_instance_stale_seconds: int = Field(default=60, ge=10, le=3600)
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
        project_embedding_fields = (
            self.project_embedding_base_url,
            self.project_embedding_model,
            self.project_embedding_model_revision,
        )
        if any(project_embedding_fields) and not all(project_embedding_fields):
            raise ValueError(
                "project embeddings require a provider URL, model, and pinned model SHA-256"
            )
        if self.agent_heartbeat_seconds >= self.agent_lease_seconds:
            raise ValueError("agent heartbeat interval must be shorter than the lease")
        if self.agent_heartbeat_seconds >= self.agent_offline_timeout_seconds:
            raise ValueError("agent heartbeat interval must be shorter than the offline timeout")
        if self.control_plane_heartbeat_seconds >= self.maintenance_lease_seconds:
            raise ValueError("control-plane heartbeat must be shorter than the maintenance lease")
        if (
            self.control_plane_heartbeat_seconds
            >= self.websocket_notification_instance_stale_seconds
        ):
            raise ValueError(
                "control-plane heartbeat must be shorter than WebSocket instance staleness"
            )
        if self.message_board_backend == "redis":
            _validate_redis_transport_url(self.redis_url.get_secret_value())
            if self.redis_operation_timeout_seconds >= self.outbox_publication_lease_seconds:
                raise ValueError(
                    "Redis operation timeout must be shorter than the outbox publication lease"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
