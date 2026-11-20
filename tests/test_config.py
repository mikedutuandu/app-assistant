import json
from pathlib import Path

from assistant.config import Settings
from assistant.db import saver_url

ROOT = Path(__file__).resolve().parents[1]


def _settings(**extra) -> Settings:
    return Settings(
        openrouter_api_key="k",
        database_url="postgresql://pooled/db",
        s3_bucket="b",
        s3_access_key="a",
        s3_secret_key="s",
        public_base_url="http://testserver",
        _env_file=None,
        **extra,
    )  # type: ignore[call-arg]


def test_saver_uses_the_unpooled_url_when_set():
    s = _settings(database_url_unpooled="postgresql://direct/db")
    assert saver_url(s) == "postgresql://direct/db"


def test_saver_falls_back_to_database_url():
    assert saver_url(_settings()) == "postgresql://pooled/db"


def test_empty_unpooled_url_is_unset():
    s = _settings(database_url_unpooled="")
    assert s.database_url_unpooled is None
    assert saver_url(s) == "postgresql://pooled/db"


def test_vercel_runs_in_iad1():
    assert json.loads((ROOT / "vercel.json").read_text())["regions"] == ["iad1"]
