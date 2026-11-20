from pathlib import Path

import pytest

from assistant.packs import PackError, load_packs


def _pack_yaml(pack_id: str, extra: str = "") -> str:
    return f"""
id: {pack_id}
name: Test App
mcp_url: "{{base}}/{pack_id}/mcp"
secret_env: TEST_PACK_SECRET
allowed_origins: ["{{base}}/"]
interview_guide: Ask what shipped.
writes:
  - kind: changelog
    count: 1
    instructions: Write a changelog.
tools:
  context: assistant_context
  save_draft: save_draft
  save_material: save_material
  charge: assistant_charge
models:
  main: a/main
  cheap: a/cheap
  vision: a/vision
  fallbacks: []
files: {{}}
message_usd_cap: 0.1
{extra}
"""


def test_loads_demo_pack():
    pack = load_packs(Path("apps"))["demo"]
    assert pack.files.per_message == 2
    assert pack.files.kinds == ["image", "voice"]
    assert pack.files.voice_seconds == 60
    assert pack.demo is True
    assert [w.kind for w in pack.writes] == ["launch_post", "changelog"]
    assert pack.tools["charge"] == "assistant_charge"


def test_base_url_is_substituted():
    pack = load_packs(Path("apps"), base_url="https://bot.example.com/")["demo"]
    assert pack.mcp_url == "https://bot.example.com/demo/mcp"
    assert pack.allowed_origins == ["https://bot.example.com"]


def test_duplicate_id_names_both_files(tmp_path: Path):
    (tmp_path / "a.yaml").write_text(_pack_yaml("same"))
    (tmp_path / "b.yaml").write_text(_pack_yaml("same"))
    with pytest.raises(PackError) as err:
        load_packs(tmp_path)
    assert "a.yaml" in str(err.value) and "b.yaml" in str(err.value)


def test_invalid_yaml_names_file(tmp_path: Path):
    (tmp_path / "bad.yaml").write_text("id: [unclosed")
    with pytest.raises(PackError, match="bad.yaml"):
        load_packs(tmp_path)


def test_invalid_pack_names_file(tmp_path: Path):
    (tmp_path / "thin.yaml").write_text("id: thin\n")
    with pytest.raises(PackError, match="thin.yaml"):
        load_packs(tmp_path)


def test_secret_unset_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "a.yaml").write_text(_pack_yaml("a"))
    pack = load_packs(tmp_path)["a"]
    monkeypatch.delenv("TEST_PACK_SECRET", raising=False)
    with pytest.raises(PackError, match="TEST_PACK_SECRET"):
        pack.secret()
    monkeypatch.setenv("TEST_PACK_SECRET", "s3cret")
    assert pack.secret() == "s3cret"
