import pytest

from assistant.main import refuse_placeholder_secrets
from assistant.packs import PackError

PLACEHOLDER = "local-demo-secret-change-before-you-expose-this-bot"


def _settings(test_settings, url):
    return test_settings.model_copy(update={"public_base_url": url})


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"])
def test_placeholders_run_on_localhost(test_settings, url):
    refuse_placeholder_secrets({"DEMO_SESSION_SECRET": PLACEHOLDER}, _settings(test_settings, url))


@pytest.mark.parametrize("url", ["https://bot.example.com", "http://192.168.1.5:8000", "http://localhost.evil.com"])
def test_placeholders_refused_anywhere_else_naming_the_variable(test_settings, url):
    with pytest.raises(PackError, match="DEMO_SESSION_SECRET"):
        refuse_placeholder_secrets({"DEMO_SESSION_SECRET": PLACEHOLDER}, _settings(test_settings, url))


def test_real_secrets_run_anywhere(test_settings):
    refuse_placeholder_secrets({"DEMO_SESSION_SECRET": "a" * 64}, _settings(test_settings, "https://bot.example.com"))


def test_compose_defaults_are_recognised_as_placeholders():
    import re
    from pathlib import Path

    text = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
    for name in ("DEMO_SESSION_SECRET",):
        default = re.search(rf"{name}: \$\{{{name}:-([^}}]+)\}}", text)
        assert default and "change-before-you-expose" in default.group(1)
