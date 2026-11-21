"""The cost-accounting core of a turn: what each node may use, what the turn has spent, and the message's cap.

Every model call goes through `Turn.chat_tools`, through the drafting tools (which charge through `Turn.spend`), or
(reading a file) through the turn as the reader's Meter. The cap is checked before each chat call, each drafting
tool and each file read's model call; every call's cost (even a failed one's) is added and reported at once through
`Deps.on_cost`, so a turn that is cut off has still recorded every call that ended. `bind` turns a node into a
LangGraph node that keeps that cost in state.

A node is written `async def node(state, turn) -> dict`: `state` is the thread (LangGraph's), `turn` this turn's view
(its dependencies and what it has spent). `bind` adapts it to LangGraph's `node(state) -> dict`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from assistant.files.read import FileReading, Meter
from assistant.graph.state import ChatState
from assistant.llm import Llm, LlmError, ToolTurn
from assistant.packs import AppPack
from assistant.timing import TimedLlm, TurnTimer, current
from assistant.tools import AppTools

CHAT_THINKING_TOKENS = 1024  # added to a chat call's max_tokens when it thinks (thinking counts toward it)

# Said when the message's cost cap stops it (`done` carries `capped`, so an app can offer a Continue button that
# sends "Continue"); what the turn's tools read is kept for the next message (`carried`), so it goes on from there.
CAP_TEXT = (
    "This took more work than one message allows, so I stopped here. Say continue and I'll pick up where I left off."
)


class Capped(Exception):
    """The message's cost cap was reached before a model call."""


# on_view's answer for a card this chat already shows with the same answer: not drawn again.
SEEN_VIEW: dict = {"seen": True}


@dataclass
class Deps:
    pack: AppPack
    llm: Llm
    tools_factory: Callable[[], AppTools]
    on_event: Callable[[str, dict], Awaitable[None]]
    read_files: Callable[[list[str], Meter], Awaitable[list[FileReading]]]  # the Meter is the turn
    read_link: Callable[[str], Awaitable[FileReading]]
    on_cost: Callable[
        [float, int], Awaitable[None]
    ]  # (cost of one finished call, its number in the turn); never raises
    calls: int = 0  # model calls whose cost has been reported this turn
    agent: Any = None  # the conversation agent's work this turn (agent.AgentTurn), shared by call_model and run_tools
    actions_factory: Callable[[], Any] | None = None  # the app's actions (app_actions.AppActions), or None
    on_pending: Callable[[dict], Awaitable[str]] | None = None  # keeps a change waiting for the user's tap
    # a read's card (MCP Apps) to show, kept; SEEN_VIEW when this chat already shows the same answer; or None
    on_view: Callable[[dict], Awaitable[dict | None]] | None = None
    app_tools: list = field(default_factory=list)  # the app's tools offered this turn (app_actions.AppTool)
    app_actions: Any = None  # the client that listed them, reused for this turn's calls
    timer: TurnTimer = field(default_factory=lambda: current.get() or TurnTimer())  # where this turn's time goes


class Turn:
    """A node's view of the turn: its dependencies and everything the turn has spent so far."""

    def __init__(self, deps: Deps, state: ChatState):
        self.deps = deps
        self.pack = deps.pack
        self.cost: float = state.get("cost_usd", 0.0)
        self.timer = deps.timer
        self.llm = TimedLlm(deps.llm, deps.timer)  # for callers that make their own model calls (the writer)

    async def emit(self, kind: str, payload: dict) -> None:
        if kind == "text" and payload.get("delta"):
            self.timer.mark_first_text()
        await self.deps.on_event(kind, payload)

    async def note(self, text: str) -> None:
        await self.emit("note", {"text": text})

    async def spend(self, cost_usd: float) -> None:
        """Adds one finished model call's cost to the turn and reports it, numbered from 1 within the turn."""
        self.cost += cost_usd
        self.deps.calls += 1
        await self.deps.on_cost(cost_usd, self.deps.calls)

    def gate(self) -> None:
        if self.cost >= self.pack.message_usd_cap:
            raise Capped

    async def chat_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        on_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> ToolTurn:
        """One call of the pack's chat model, streamed to `on_text`, offering `tools` (none: it must answer in text)."""
        self.gate()
        models = self.pack.models
        thinking = models.chat_reasoning == "low"
        try:
            async with self.timer.span("model", models.chat):
                out = await self.deps.llm.chat_tools(
                    list(messages),
                    models.chat,
                    models.fallbacks,
                    tools,
                    max_tokens + (CHAT_THINKING_TOKENS if thinking else 0),
                    on_text=on_text,
                    reasoning="low" if thinking else None,
                    temperature=models.chat_temperature,
                )
        except LlmError as e:
            await self.spend(e.cost_usd)
            raise
        await self.spend(out.cost_usd)
        return out


def bind(node: Callable[[ChatState, Turn], Awaitable[dict]], deps: Deps) -> Callable[[ChatState], Awaitable[dict]]:
    """A LangGraph node: runs `node`, keeps the turn's cost in state, and ends the turn at the cost cap.

    An error leaving the graph carries `cost_usd`: everything this turn spent, so the caller can still charge it.
    """

    async def run(state: ChatState) -> dict:
        turn = Turn(deps, state)
        try:
            async with deps.timer.span("step", node.__name__):
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
