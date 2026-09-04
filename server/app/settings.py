from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONGARS_", env_file=".env", extra="ignore")

    env: str = "dev"
    host: str = "0.0.0.0"
    port: int = 8710
    db_path: Path = Path("./data/mongars.db")
    redis_url: str = "redis://127.0.0.1:6379/0"
    llm_base_url: str = "http://127.0.0.1:8711/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
