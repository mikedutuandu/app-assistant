"""An app whose shared secret is not set is left out at startup, with a warning naming the variable, so a new
deployment runs with only the apps it has secrets for (the shipped HookPlaybook entry needs no secret elsewhere).
With no app left, the server does not start."""

import logging
from pathlib import Path

import pytest

from assistant.main import apps_with_secrets
from assistant.packs import PackError, RegistryEntry, load_packs

APPS = Path(__file__).parents[1] / "apps"
HOOKPLAYBOOK = RegistryEntry(
    id="hookplaybook", site="https://hookplaybook.com", secret_env="HOOKPLAYBOOK_SESSION_SECRET"
)


def test_an_app_without_its_secret_is_left_out_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("DEMO_SESSION_SECRET", "a" * 64)
    monkeypatch.delenv("HOOKPLAYBOOK_SESSION_SECRET", raising=False)
    with caplog.at_level(logging.WARNING):
        local, registry = apps_with_secrets(load_packs(APPS, "http://localhost:8000"), [HOOKPLAYBOOK])
    assert list(local) == ["demo"]
    assert registry == []
    assert "HOOKPLAYBOOK_SESSION_SECRET" in caplog.text


def test_every_app_with_its_secret_is_kept(monkeypatch):
    monkeypatch.setenv("DEMO_SESSION_SECRET", "a" * 64)
    monkeypatch.setenv("HOOKPLAYBOOK_SESSION_SECRET", "b" * 64)
    local, registry = apps_with_secrets(load_packs(APPS, "http://localhost:8000"), [HOOKPLAYBOOK])
    assert sorted(local) == ["demo", "hookplaybook"]
    assert registry == [HOOKPLAYBOOK]


def test_no_app_with_its_secret_stops_the_server_naming_the_variables(monkeypatch):
    monkeypatch.delenv("DEMO_SESSION_SECRET", raising=False)
    monkeypatch.delenv("HOOKPLAYBOOK_SESSION_SECRET", raising=False)
    with pytest.raises(PackError, match="DEMO_SESSION_SECRET"):
        apps_with_secrets(load_packs(APPS, "http://localhost:8000"), [HOOKPLAYBOOK])
