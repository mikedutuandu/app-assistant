"""Where a turn's time went: one timer per turn, one log line at its end.

Spans are named by what they call (a model id, an app tool's name, a graph node), never by anything the user wrote:
the line holds names and milliseconds only.
"""

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from typing import Any, Literal

log = logging.getLogger("assistant.timing")

SpanKind = Literal["model", "tool", "step"]
Outcome = Literal["done", "failed", "capped", "cancelled"]


class TurnTimer:
    def __init__(self, clock: Callable[[], float] = time.perf_counter):
        self._clock = clock
        self._start = clock()
        self._first_text: float | None = None
        self._spans: dict[str, list[dict[str, Any]]] = {"model": [], "tool": [], "step": []}

    def span(self, kind: SpanKind, name: str) -> AbstractAsyncContextManager[None]:
        return self._span(kind, name)

    @asynccontextmanager
    async def _span(self, kind: SpanKind, name: str) -> AsyncIterator[None]:
        began = self._clock()
        try:
            yield
        finally:  # a failed call took time too
            self._spans[kind].append({"name": name, "ms": self._ms(self._clock() - began)})

    def mark_first_text(self) -> None:
        if self._first_text is None:
            self._first_text = self._clock()

    def summary(self) -> dict:
        return {
            "total_ms": self._ms(self._clock() - self._start),
            "first_text_ms": None if self._first_text is None else self._ms(self._first_text - self._start),
            **{kind: list(spans) for kind, spans in self._spans.items()},
        }

    @staticmethod
    def _ms(seconds: float) -> int:
        return round(seconds * 1000)


current: ContextVar["TurnTimer | None"] = ContextVar("turn_timer", default=None)
"""The running turn's timer: set by `run_turn`, picked up by the graph it builds (`graph.turn.Deps`)."""


def log_turn(timer: TurnTimer, app: str, outcome: Outcome) -> None:
    """The turn's one timing line. Spans that had not ended by now (a charge still running) are not in it."""
    log.info("turn timing %s", json.dumps({**timer.summary(), "app": app, "outcome": outcome}, separators=(",", ":")))


class TimedLlm:
    """An `Llm` whose model calls are timed, named by model id. Everything else passes through."""

    def __init__(self, llm: Any, timer: TurnTimer):
        self._llm = llm
        self._timer = timer

    async def chat(self, messages, model, *args, **kwargs):
        async with self._timer.span("model", model):
            return await self._llm.chat(messages, model, *args, **kwargs)

    async def chat_tools(self, messages, model, *args, **kwargs):
        async with self._timer.span("model", model):
            return await self._llm.chat_tools(messages, model, *args, **kwargs)

    async def chat_json(self, messages, model, *args, **kwargs):
        async with self._timer.span("model", model):
            return await self._llm.chat_json(messages, model, *args, **kwargs)

    async def transcribe(self, *args, **kwargs):
        async with self._timer.span("model", "transcribe"):
            return await self._llm.transcribe(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)
