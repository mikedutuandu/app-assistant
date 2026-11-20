import json
from pathlib import Path

from starlette.requests import Request

from assistant.api.deps import client_ip
from assistant.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "client": ("10.0.0.1", 1234)})


def test_asgi_module_exposes_app(monkeypatch):
    import importlib
    import sys

    from fastapi import FastAPI

    from assistant.config import get_settings

    for name, value in {
        "OPENROUTER_API_KEY": "k",
        "DATABASE_URL": "postgresql://x",
        "S3_BUCKET": "b",
        "S3_ACCESS_KEY": "a",
        "S3_SECRET_KEY": "s",
        "PUBLIC_BASE_URL": "http://t",
        "RUN_LIFESPAN": "false",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    sys.modules.pop("assistant.asgi", None)
    asgi = importlib.import_module("assistant.asgi")
    get_settings.cache_clear()

    assert isinstance(asgi.app, FastAPI)


def test_client_ip_prefers_x_vercel_forwarded_for():
    request = _request(
        {"x-vercel-forwarded-for": "198.51.100.7", "x-real-ip": "192.0.2.1", "x-forwarded-for": "1.1.1.1"}
    )
    assert client_ip(request) == "198.51.100.7"


def test_client_ip_then_x_real_ip():
    assert client_ip(_request({"x-real-ip": "192.0.2.1", "x-forwarded-for": "1.1.1.1, 2.2.2.2"})) == "192.0.2.1"


def test_client_ip_then_rightmost_x_forwarded_for():
    assert client_ip(_request({"x-forwarded-for": "1.1.1.1, 203.0.113.9"})) == "203.0.113.9"
    assert (
        client_ip(_request({"x-vercel-forwarded-for": " ", "x-forwarded-for": "1.1.1.1, 203.0.113.9"})) == "203.0.113.9"
    )
    assert client_ip(_request({})) == "10.0.0.1"


def test_pool_and_drain_defaults_fit_vercel():
    fields = Settings.model_fields
    assert (fields["db_pool_max"].default, fields["saver_pool_max"].default) == (2, 1)
    assert fields["shutdown_drain_seconds"].default == 0.4


def test_vercel_json_points_at_the_entrypoint():
    config = json.loads((ROOT / "vercel.json").read_text())
    (function,) = config["functions"]
    assert (ROOT / function).is_file()
    assert function == "src/assistant/asgi.py"
    assert config["functions"][function]["maxDuration"] == 300
    assert config["crons"] == [{"path": "/internal/cleanup", "schedule": "0 3 * * *"}]


def test_pyproject_entrypoint_matches_asgi_module():
    text = (ROOT / "pyproject.toml").read_text()
    assert '[tool.vercel]\nentrypoint = "assistant.asgi:app"' in text
