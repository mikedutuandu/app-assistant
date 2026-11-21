import json
import os
import subprocess
import sys
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
    monkeypatch.delitem(sys.modules, "assistant.asgi", raising=False)
    asgi = importlib.import_module("assistant.asgi")
    get_settings.cache_clear()

    assert isinstance(asgi.app, FastAPI)


def test_client_ip_prefers_x_vercel_forwarded_for():
    request = _request(
        {"x-vercel-forwarded-for": "198.51.100.7", "x-real-ip": "192.0.2.1", "x-forwarded-for": "1.1.1.1"}
    )
    assert client_ip(request) == "198.51.100.7"


def test_client_ip_takes_the_first_entry_of_a_list():
    assert client_ip(_request({"x-vercel-forwarded-for": "198.51.100.7, 10.1.1.1"})) == "198.51.100.7"
    assert client_ip(_request({"x-real-ip": " 192.0.2.1 , 10.1.1.1"})) == "192.0.2.1"


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
    assert function == "app.py"
    assert "src" not in config["functions"][function]["excludeFiles"]
    assert config["functions"][function]["maxDuration"] == 300
    assert config["crons"] == [{"path": "/internal/cleanup", "schedule": "0 3 * * *"}]


def test_pyproject_entrypoint_is_the_root_shim():
    text = (ROOT / "pyproject.toml").read_text()
    assert '[tool.vercel]\nentrypoint = "app:app"' in text
    assert (ROOT / "app.py").is_file()
    assert (ROOT / "src" / "assistant" / "asgi.py").is_file()


def test_app_py_shim_imports_with_a_clean_path():
    # A fresh interpreter whose path has no `assistant` package: the shim must add src/ itself.
    code = (
        "import os, runpy, sys\n"
        "sys.path[:] = [p for p in sys.path if not os.path.isdir(os.path.join(p or '.', 'assistant'))]\n"
        "import importlib.util as u\n"
        "assert u.find_spec('assistant') is None\n"
        "ns = runpy.run_path('app.py')\n"
        "print(type(ns['app']).__name__)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"} | {
        "OPENROUTER_API_KEY": "k",
        "DATABASE_URL": "postgresql://x",
        "S3_BUCKET": "b",
        "S3_ACCESS_KEY": "a",
        "S3_SECRET_KEY": "s",
        "PUBLIC_BASE_URL": "http://t",
        "RUN_LIFESPAN": "false",
    }
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "FastAPI"


def test_env_example_leaves_supabase_upload_settings_empty():
    """After `cp .env.example .env`, local uploads must not sign at a placeholder Supabase project."""
    lines = dict(
        line.split("=", 1) for line in (ROOT / ".env.example").read_text().splitlines() if line and line[0] != "#"
    )
    assert lines["SUPABASE_URL"] == "" and lines["SUPABASE_SERVICE_KEY"] == ""


def test_storage_names_its_upload_backend(test_settings):
    from assistant.files.storage import Storage

    assert Storage(test_settings).upload_backend == "s3"
    signed = test_settings.model_copy(update={"supabase_url": "https://ref.supabase.co", "supabase_service_key": "k"})
    assert Storage(signed).upload_backend == "supabase"
