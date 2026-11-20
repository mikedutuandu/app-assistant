"""The cost-accounting core of a turn: what each node may use, what the turn has spent, and the message's cap.

Every model call goes through `Turn.json` or `Turn.stream`, which first check the cap and then add what the call
cost (even a failed one). `bind` turns a node into a LangGraph node that keeps that cost in state.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel

from assistant.files.read import FileReading
from assistant.graph.state import ChatState
from assistant.llm import Llm, LlmError
from assistant.packs import AppPack
from assistant.tools import AppTools

CAP_TEXT = "That's as far as I can go on this message. Send another to continue."


class Capped(Exception):
    """The message's cost cap was reached before a model call."""


@dataclass
class Deps:
    pack: AppPack
    llm: Llm
    tools_factory: Callable[[], AppTools]
    on_event: Callable[[str, dict], Awaitable[None]]
    read_files: Callable[[list[str]], Awaitable[list[FileReading]]]
    read_link: Callable[[str], Awaitable[FileReading]]


class Turn:
    """A node's view of the turn: its dependencies and everything the turn has spent so far."""

    def __init__(self, deps: Deps, state: ChatState):
        self.deps = deps
        self.pack = deps.pack
        self.cost: float = state.get("cost_usd", 0.0)

    async def emit(self, kind: str, payload: dict) -> None:
        await self.deps.on_event(kind, payload)

    async def note(self, text: str) -> None:
        await self.emit("note", {"text": text})

    def gate(self) -> None:
        if self.cost >= self.pack.message_usd_cap:
            raise Capped

    async def json[T: BaseModel](self, system: str, user: str, schema: type[T], max_tokens: int, cheap=False) -> T:
        self.gate()
        models = self.pack.models
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        try:
            out, cost = await self.deps.llm.chat_json(
                messages, models.cheap if cheap else models.main, models.fallbacks, schema, max_tokens
            )
        except LlmError as e:
            self.cost += e.cost_usd
            raise
        self.cost += cost
        if not isinstance(out, schema):
            raise LlmError("bad_output", f"expected {schema.__name__}")
        return out

    async def stream(self, system: str, user: str, max_tokens: int) -> str:
        self.gate()
        models = self.pack.models
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

        async def forward(delta: str) -> None:
            await self.emit("text", {"delta": delta})

        try:
            result = await self.deps.llm.chat(messages, models.main, models.fallbacks, max_tokens, on_text=forward)
        except LlmError as e:
            self.cost += e.cost_usd
            raise
        self.cost += result.cost_usd
        return result.text


def bind(node: Callable[[ChatState, Turn], Awaitable[dict]], deps: Deps) -> Callable[[ChatState], Awaitable[dict]]:
    """A LangGraph node: runs `node`, keeps the turn's cost in state, and ends the turn at the cost cap.

    An error leaving the graph carries `cost_usd`: everything this turn spent, so the caller can still charge it.
    """

    async def run(state: ChatState) -> dict:
        turn = Turn(deps, state)
        try:
            update = await node(state, turn)
        except Capped:
            await turn.emit("text", {"delta": CAP_TEXT})
            return {"capped": True, "cost_usd": turn.cost}
        except Exception as e:
            e.cost_usd = turn.cost  # type: ignore[attr-defined]
            raise
        return {**update, "cost_usd": turn.cost}

    run.__name__ = node.__name__
    return run
