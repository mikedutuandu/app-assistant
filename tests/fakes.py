"""Test doubles shared by the graph and chat API tests: a scripted Llm and recording AppTools."""

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from assistant.checks import Fact
from assistant.llm import LlmResult
from assistant.tools import AppContext, SavedDraft

_MARKER = re.compile(r"^\[\[([A-Z_]+)\]\]")


@dataclass
class Out:
    """One scripted output with its own cost (otherwise FakeLlm.cost is used)."""

    value: Any
    cost: float | None = None


def marker(messages: list[dict]) -> str:
    """The [[NAME]] marker at the start of the first system message."""
    for m in messages:
        if m.get("role") == "system":
            found = _MARKER.match(m["content"] if isinstance(m["content"], str) else "")
            return found.group(1) if found else "?"
    return "?"


class FakeLlm:
    """Returns scripted outputs keyed by the prompt's marker, popped in order. Records every call.

    A scripted value is the model's output (a str for chat, a dict for chat_json, a (text, seconds) tuple for
    transcribe under the key "TRANSCRIBE"), an Exception to raise, or an `Out` carrying its own cost.
    """

    def __init__(self, script: dict[str, list] | None = None, cost: float = 0.001):
        self.script: dict[str, list] = {k: list(v) for k, v in (script or {}).items()}
        self.cost = cost
        self.calls: list[dict] = []

    def add(self, name: str, *values: Any) -> None:
        self.script.setdefault(name, []).extend(values)

    def names(self) -> list[str]:
        return [c["name"] for c in self.calls]

    def _next(self, name: str, messages: list[dict] | None, model: str | None) -> tuple[Any, float]:
        self.calls.append({"name": name, "messages": messages, "model": model})
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

    async def chat(self, messages, model, fallbacks, max_tokens, on_text=None) -> LlmResult:
        text, cost = self._next(marker(messages), messages, model)
        if on_text is not None:
            await on_text(text)
        return LlmResult(text=text, cost_usd=cost, model=model)

    async def chat_json(self, messages, model, fallbacks, schema: type[BaseModel], max_tokens):
        value, cost = self._next(marker(messages), messages, model)
        return schema.model_validate(value), cost

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
    return base.model_copy(update=changes)


class FakeTools:
    """AppTools that records the name of every method called in `.calls`. `errors` maps a method to raise."""

    def __init__(self, context: AppContext | None = None, errors: dict[str, Exception] | None = None):
        self._context = context or default_context()
        self.errors = errors or {}
        self.calls: list[str] = []
        self.drafts: list[dict] = []
        self.materials: list[dict] = []
        self.charges: list[dict] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name in self.errors:
            raise self.errors[name]

    async def context(self) -> AppContext:
        self._record("context")
        return self._context

    async def save_draft(self, text: str, media: list[dict], key: str) -> SavedDraft:
        self._record("save_draft")
        for d in self.drafts:
            if d["key"] == key:
                return SavedDraft(id=d["id"], edit_url=d["edit_url"])
        draft = {"id": f"draft-{len(self.drafts) + 1}", "text": text, "media": media, "key": key}
        draft["edit_url"] = f"https://app.example/drafts/{draft['id']}"
        self.drafts.append(draft)
        return SavedDraft(id=draft["id"], edit_url=draft["edit_url"])

    async def save_material(self, facts: list[Fact], draft_id: str | None, key: str) -> None:
        self._record("save_material")
        self.materials.append({"facts": facts, "draft_id": draft_id, "key": key})

    async def charge(self, cost_usd: float, ref: str) -> None:
        self._record("charge")
        self.charges.append({"cost_usd": cost_usd, "ref": ref})
