from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: str
    database_url: str
    s3_endpoint: str | None = None
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_region: str = "auto"
    packs_dir: Path = Path("apps")
    public_base_url: str
    demo_daily_usd_cap: float = 3.0


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
