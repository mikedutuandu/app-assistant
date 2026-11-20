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
guide: A launch app. A good story says what shipped.
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
  chat: a/chat
  writer: a/writer
  main: a/main
  cheap: a/cheap
  vision: a/vision
  fallbacks: []
files: {{}}
message_usd_cap: 0.1
{extra}
"""


def test_loads_demo_pack():
    pack = load_packs(Path("apps"), base_url="http://testserver")["demo"]
    assert pack.files.per_message == 2
    assert pack.files.kinds == ["image", "voice"]
    assert pack.files.voice_seconds == 60
    assert pack.demo is True
    assert [w.kind for w in pack.writes] == ["x_post", "x_thread"]
    assert pack.tools["charge"] == "assistant_charge"


def test_base_url_is_substituted():
    pack = load_packs(Path("apps"), base_url="https://bot.example.com/")["demo"]
    assert pack.mcp_url == "https://bot.example.com/demo/mcp"
    assert pack.allowed_origins == ["https://bot.example.com"]


def test_duplicate_id_names_both_files(tmp_path: Path):
    (tmp_path / "a.yaml").write_text(_pack_yaml("same"))
    (tmp_path / "b.yaml").write_text(_pack_yaml("same"))
    with pytest.raises(PackError) as err:
        load_packs(tmp_path, "http://testserver")
    assert "a.yaml" in str(err.value) and "b.yaml" in str(err.value)


def test_invalid_yaml_names_file(tmp_path: Path):
    (tmp_path / "bad.yaml").write_text("id: [unclosed")
    with pytest.raises(PackError, match="bad.yaml"):
        load_packs(tmp_path, "http://testserver")


def test_invalid_pack_names_file(tmp_path: Path):
    (tmp_path / "thin.yaml").write_text("id: thin\n")
    with pytest.raises(PackError, match="thin.yaml"):
        load_packs(tmp_path, "http://testserver")


def test_secret_unset_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "a.yaml").write_text(_pack_yaml("a"))
    pack = load_packs(tmp_path, "http://testserver")["a"]
    monkeypatch.delenv("TEST_PACK_SECRET", raising=False)
    with pytest.raises(PackError, match="TEST_PACK_SECRET"):
        pack.secret()
    monkeypatch.setenv("TEST_PACK_SECRET", "s3cret")
    assert pack.secret() == "s3cret"


def test_base_placeholder_without_base_url_raises(tmp_path: Path):
    (tmp_path / "a.yaml").write_text(_pack_yaml("a"))
    with pytest.raises(PackError, match="a.yaml"):
        load_packs(tmp_path)
    with pytest.raises(PackError, match="public_base_url"):
        load_packs(tmp_path, "  ")


def test_no_base_placeholder_needs_no_base_url(tmp_path: Path):
    text = _pack_yaml("a").replace("{base}", "https://app.example")
    (tmp_path / "a.yaml").write_text(text)
    pack = load_packs(tmp_path)["a"]
    assert pack.mcp_url == "https://app.example/a/mcp"
    assert pack.allowed_origins == ["https://app.example"]


def test_tools_must_name_all_four(tmp_path: Path):
    text = _pack_yaml("a").replace("  charge: assistant_charge\n", "")
    (tmp_path / "a.yaml").write_text(text)
    with pytest.raises(PackError, match="missing: charge"):
        load_packs(tmp_path, "http://testserver")


def test_tool_names_must_not_be_empty(tmp_path: Path):
    text = _pack_yaml("a").replace("save_draft: save_draft", 'save_draft: ""')
    (tmp_path / "a.yaml").write_text(text)
    with pytest.raises(PackError, match="missing: save_draft"):
        load_packs(tmp_path, "http://testserver")


def test_pack_models_have_chat_and_writer():
    packs = load_packs(Path("apps"), base_url="http://testserver")
    for pack_id in ("hookplaybook", "demo"):
        m = packs[pack_id].models
        assert m.chat == "anthropic/claude-haiku-5.5"
        assert m.writer == "anthropic/claude-sonnet-5.5"
        assert m.writer_reasoning == "low"
        assert m.fallbacks == ["anthropic/claude-haiku-5.5", "openai/gpt-6-luna"]
        assert (m.main, m.cheap, m.vision) == ("openai/gpt-6-luna",) * 3


def test_x_thread_spec_loads():
    for pack_id in ("hookplaybook", "demo"):
        pack = load_packs(Path("apps"), base_url="http://testserver")[pack_id]
        assert [w.kind for w in pack.writes] == ["x_post", "x_thread"]
        post, thread = pack.writes
        assert (post.count, post.max_chars, post.max_posts) == (1, 280, None)
        assert (thread.count, thread.max_chars, thread.max_posts) == (1, 280, 8)
        assert thread.no_links and thread.no_dashes and thread.may_carry_image


def test_guide_replaces_interview_guide(tmp_path):
    (tmp_path / "old.yaml").write_text(_pack_yaml("old").replace("\nguide:", "\ninterview_guide:"))
    with pytest.raises(PackError, match="rename it to `guide`"):
        load_packs(tmp_path, base_url="http://testserver")
