"""The demo app's own MCP server (`/demo/mcp`), reached by the bot's real MCP client as any app's would be."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from chat_api import SECRET, Api
from test_demo_contract import WORKSPACE, demo_token

from assistant import app_actions
from assistant.app_actions import McpAppActions
from assistant.auth import Session
from assistant.packs import load_packs

pytestmark = pytest.mark.db


@pytest.fixture
def demo(api: Api, monkeypatch):
    monkeypatch.setenv("DEMO_SESSION_SECRET", SECRET)
    app_actions._lists.clear()
    pack = load_packs(Path("apps"), base_url="http://testserver")["demo"]
    api.app.state.packs = type(api.app.state.packs)({"demo": pack}, api.app.state.settings)
    return pack


def client(api: Api) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver")


def session(token: str) -> Session:
    return Session(app="demo", workspace=WORKSPACE, access="draft", token=token, expires_at=datetime.now(UTC))


async def saved(api: Api, *texts: str) -> None:
    async with api.pool.connection() as conn:
        for i, text in enumerate(texts):
            await conn.execute(
                "insert into demo_drafts (id, workspace, text, idem_key) values (%s, %s, %s, %s)",
                (f"e{i}", WORKSPACE, text, f"k{i}"),
            )


async def test_the_demo_pack_has_actions_on_its_own_site(demo):
    assert demo.actions.mcp_url == "http://testserver/demo/mcp"
    assert demo.actions.starters == ["What have I saved so far?", "Write a changelog entry"]


async def test_the_bots_client_lists_reads_and_changes_through_the_demos_server(api: Api, demo):
    await saved(api, "Dark mode is live.", "Exports are 2x faster.")
    async with client(api) as c:
        actions = McpAppActions(demo, session(demo_token("valid")), c)
        tools = {t.name: t for t in await actions.list_tools()}
        assert [n for n in tools] == ["list_entries", "count_entries", "delete_entry"]
        assert tools["list_entries"].read_only and not tools["delete_entry"].read_only
        assert (await actions.call("count_entries", {})).text == "2 saved entries."
        assert "Dark mode is live." in (await actions.call("list_entries", {})).text
        gone = await actions.call("delete_entry", {"id": "e0"}, key="confirm:p1")
        assert (gone.text, gone.is_error) == ("Deleted.", False)
        again = await actions.call("delete_entry", {"id": "e0"})
        assert (again.text, again.is_error) == ("No such entry.", True)
        assert (await actions.call("count_entries", {})).text == "1 saved entry."


async def test_another_workspaces_entries_are_never_seen(api: Api, demo):
    await saved(api, "Mine.")
    async with api.pool.connection() as conn:
        await conn.execute("update demo_drafts set workspace = 'someone-else'")
    async with client(api) as c:
        actions = McpAppActions(demo, session(demo_token("valid")), c)
        assert (await actions.call("list_entries", {})).text == "No entries saved yet."


async def test_a_bad_token_is_refused(api: Api, demo):
    async with client(api) as c:
        r = await c.post(
            "/demo/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {demo_token('expired')}"},
        )
    assert r.status_code == 401
