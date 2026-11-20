import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from assistant.packs import RegistryEntry

_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,40}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    openrouter_api_key: str
    # Neon (Vercel Marketplace) injects DATABASE_URL (pooled, PgBouncer) and DATABASE_URL_UNPOOLED (direct), or the same
    # with a NEON_ prefix when Vercel makes the connection use one. LangGraph's checkpoints and migrations' locks want a
    # session of their own, so every pool and the startup migration use the unpooled one when set (DATABASE_URL is then
    # unused). The unprefixed name wins when both are set.
    database_url: str = Field(validation_alias=AliasChoices("database_url", "neon_database_url"))
    database_url_unpooled: str | None = Field(
        default=None, validation_alias=AliasChoices("database_url_unpooled", "neon_database_url_unpooled")
    )
    # Every table (LangGraph's checkpoints too) lives in this schema. On Supabase it must not be an exposed schema.
    db_schema: str = "assistant"
    db_pool_max: int = 2  # connections per instance for the app's queries
    saver_pool_max: int = 1  # connections per instance for LangGraph's checkpoints
    s3_endpoint: str | None = None
    s3_bucket: str
    s3_access_key: str
    s3_secret_key: str
    s3_region: str = "auto"
    # Supabase: the project URL and its secret (service role) key. When both are set, the browser uploads through
    # Supabase Storage's own signed upload links; otherwise through an S3 presigned PUT.
    supabase_url: str | None = None
    supabase_service_key: str | None = None
    packs_dir: Path = Path("apps")  # the YAML packs, and registry.yaml: the apps trusted to serve their own pack
    # APPS: the registry as JSON (`[{"id", "site", "secret_env"}]`); when set, apps/registry.yaml is not read.
    apps: Annotated[list[RegistryEntry] | None, NoDecode] = None
    # The owner's ceilings, over every pack (an app's own pack can never raise them):
    max_message_usd: float = 0.25  # caps a pack's message_usd_cap
    allowed_models: Annotated[list[str] | None, NoDecode] = None  # ALLOWED_MODELS="a/b,c/d"; unset: any model
    max_app_daily_usd: float | None = None  # one app's model spend per UTC day; over it, "resting". Unset: off
    public_base_url: str
    demo_daily_usd_cap: float = 3.0
    run_lifespan: bool = True
    # Vercel Cron sends `Authorization: Bearer ${CRON_SECRET}` to /internal/cleanup; unset, that endpoint is off.
    cron_secret: str | None = None
    shutdown_drain_seconds: float = 0.4  # Vercel gives a function about 500 ms at shutdown

    @field_validator("db_schema")
    @classmethod
    def _plain_schema_name(cls, value: str) -> str:
        if not _SCHEMA.fullmatch(value):
            raise ValueError("DB_SCHEMA must be lowercase letters, digits and _, starting with a letter or _")
        return value

    @field_validator("apps", mode="before")
    @classmethod
    def _apps_json(cls, value: object) -> object:
        if isinstance(value, str):
            return json.loads(value) if value.strip() else None
        return value

    @field_validator("allowed_models", mode="before")
    @classmethod
    def _model_list(cls, value: object) -> object:
        if isinstance(value, str):
            if value.strip().startswith("["):
                return json.loads(value)
            return [m.strip() for m in value.split(",") if m.strip()] or None
        return value

    @field_validator(
        "s3_endpoint",
        "cron_secret",
        "database_url_unpooled",
        "supabase_url",
        "supabase_service_key",
        "max_app_daily_usd",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        # `S3_ENDPOINT=` in an env file loads as "": treat it as unset (boto3 then uses AWS; uploads use S3 presigns).
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
