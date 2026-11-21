"""Test doubles shared by the graph and chat API tests: a scripted Llm and recording AppTools."""

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from assistant.checks import Fact
from assistant.llm import LlmResult, ToolCall, ToolTurn
from assistant.tools import AppContext, SavedDraft

_MARKER = re.compile(r"^\[\[([A-Z_]+)\]\]")


@dataclass
class Out:
    """One scripted output with its own cost (otherwise FakeLlm.cost is used)."""

    value: Any
    cost: float | None = None


@dataclass
class Reply:
    """One scripted `chat_tools` turn: streamed text and/or tool calls (ToolCall, or a dict of its fields; a dict
    without an `id` gets one)."""

    text: str = ""
    calls: list = field(default_factory=list)


def call(name: str, **arguments: Any) -> dict:
    """A tool call for a `Reply`."""
    return {"name": name, "arguments": arguments}


def marker(messages: list[dict]) -> str:
    """The [[NAME]] marker at the start of the first system message."""
    for m in messages:
        if m.get("role") == "system":
            found = _MARKER.match(m["content"] if isinstance(m["content"], str) else "")
            return found.group(1) if found else "?"
    return "?"


class FakeLlm:
    """Returns scripted outputs keyed by the prompt's marker, popped in order. Records every call.

    A scripted value is the model's output (a str for chat, a dict for chat_json, a str or a `Reply` for
    chat_tools, a (text, seconds) tuple for transcribe under the key "TRANSCRIBE"), an Exception to raise, or an
    `Out` carrying its own cost.
    """

    def __init__(self, script: dict[str, list] | None = None, cost: float = 0.001):
        self.script: dict[str, list] = {k: list(v) for k, v in (script or {}).items()}
        self.cost = cost
        self.calls: list[dict] = []
        self._pending: dict = {}  # max_tokens and reasoning of the call being made

    def add(self, name: str, *values: Any) -> None:
        self.script.setdefault(name, []).extend(values)

    def names(self) -> list[str]:
        return [c["name"] for c in self.calls]

    def _next(self, name: str, messages: list[dict] | None, model: str | None) -> tuple[Any, float]:
        self.calls.append({"name": name, "messages": messages, "model": model, **self._pending})
        self._pending = {}
        queue = self.script.get(name)
        if not queue:
            raise AssertionError(f"FakeLlm: no scripted output left for {name}")
        item = queue.pop(0)
        value, cost = item, self.cost
        if isinstance(item, Out):
            value, cost = item.value, self.cost if item.cost is None else item.cost
        if isinstance(value, BaseException):
            raise value
        return value, cost

    async def chat(self, messages, model, fallbacks, max_tokens, on_text=None, reasoning=None) -> LlmResult:
        self._pending = {"max_tokens": max_tokens, "reasoning": reasoning}
        text, cost = self._next(marker(messages), messages, model)
        if on_text is not None:
            await on_text(text)
        return LlmResult(text=text, cost_usd=cost, model=model)

    async def chat_json(self, messages, model, fallbacks, schema: type[BaseModel], max_tokens, reasoning=None):
        self._pending = {"max_tokens": max_tokens, "reasoning": reasoning}
        value, cost = self._next(marker(messages), messages, model)
        return schema.model_validate(value), cost

    async def chat_tools(
        self, messages, model, fallbacks, tools, max_tokens, on_text=None, reasoning=None, temperature=None
    ) -> ToolTurn:
        self._pending = {"max_tokens": max_tokens, "reasoning": reasoning, "tools": tools, "temperature": temperature}
        value, cost = self._next(marker(messages), list(messages), model)  # a copy: the agent appends to its list
        reply = value if isinstance(value, Reply) else Reply(text=value)
        calls = []
        for n, c in enumerate(reply.calls, 1):
            calls.append(c if isinstance(c, ToolCall) else ToolCall.model_validate({"id": f"call_{n}", **c}))
        if reply.text and on_text is not None:
            await on_text(reply.text)
        return ToolTurn(text=reply.text, calls=calls, cost_usd=cost, model=model)

    async def transcribe(self, audio: bytes, format: str) -> tuple[str, float, float]:
        (text, seconds), cost = self._next("TRANSCRIBE", None, None)
        return text, seconds, cost


def default_context(**changes: Any) -> AppContext:
    base = AppContext(
        voice="friendly, plain, a small team's founder",
        topics=["product updates"],
        recent_posts=["We fixed the login page bug for everyone this week, thanks for your patience."],
        credits_left=None,
        out_of_credits_message=None,
        language="en",
    )
    return AppContext.model_validate(base.model_dump() | changes)  # validated, so kept_facts may be given as dicts


class FakeTools:
    """AppTools that records the name of every method called in `.calls`. `errors` maps a method to raise."""

    def __init__(self, context: AppContext | None = None, errors: dict[str, Exception] | None = None):
        self._context = context or default_context()
        self.errors = errors or {}
        self.calls: list[str] = []
        self.drafts: list[dict] = []
        self.materials: list[dict] = []
        self.charges: list[dict] = []
        self.schedule_note: str | None = None  # set: the app can't schedule, and says why

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self.errors:
            raise self.errors[name]

    async def context(self) -> AppContext:
        self._record("context")
        return self._context

    async def save_draft(
        self, text: str, thread: list[str], media: list[dict], key: str, schedule_at: str | None = None
    ) -> SavedDraft:
        """Schedules at `schedule_at` unless `schedule_note` is set (then it only saves the draft, with that note)."""
        self._record("save_draft")
        for d in self.drafts:
            if d["key"] == key:
                return SavedDraft(id=d["id"], edit_url=d["edit_url"], scheduled_at=d["scheduled_at"])
        draft = {"id": f"draft-{len(self.drafts) + 1}", "text": text, "thread": thread, "media": media, "key": key}
        draft["edit_url"] = f"https://app.example/drafts/{draft['id']}"
        draft["schedule_at"] = schedule_at
        draft["scheduled_at"] = schedule_at if self.schedule_note is None else None
        self.drafts.append(draft)
        note = self.schedule_note if schedule_at is not None else None
        return SavedDraft(
            id=draft["id"], edit_url=draft["edit_url"], scheduled_at=draft["scheduled_at"], schedule_note=note
        )

    async def save_material(self, facts: list[Fact], draft_id: str | None, key: str) -> None:
        self._record("save_material")
        self.materials.append({"facts": facts, "draft_id": draft_id, "key": key})

    async def charge(self, cost_usd: float, ref: str) -> None:
        self._record("charge")
        if any(c["ref"] == ref for c in self.charges):  # the app charges once per ref, as HookPlaybook does
            return
        self.charges.append({"cost_usd": cost_usd, "ref": ref})


class FakeActions:
    """An app's MCP server as app_actions.AppActions: `tools` its list, `answers` what each tool returns (a str, an
    ActionResult, or an Exception to raise). Records every call in `.calls`."""

    def __init__(self, tools: list | None = None, answers: dict | None = None):
        from assistant.app_actions import AppTool

        self.tools = tools or [
            AppTool("list_items", "List items", "The user's items, newest first.", OBJECT, True),
            AppTool("get_stats", "Get stats", "The user's numbers for the last 7 days.", OBJECT, True),
            AppTool(
                "move_item",
                "Move an item",
                "Moves an item to another day.",
                {"type": "object", "properties": {"id": {"type": "string"}, "day": {"type": "string"}}},
                False,
            ),
        ]
        self.answers = answers or {}
        self.calls: list[tuple[str, dict, str | None]] = []
        self.listed = 0

    async def list_tools(self):
        self.listed += 1
        return list(self.tools)

    async def call(self, name: str, arguments: dict, key: str | None = None):
        from assistant.app_actions import ActionResult

        self.calls.append((name, arguments, key))
        got = self.answers.get(name, "ok")
        if isinstance(got, Exception):
            raise got
        return got if isinstance(got, ActionResult) else ActionResult(got, False)


OBJECT = {"type": "object", "properties": {}}
