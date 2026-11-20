"""Per-step timing of a turn: the timer, its one log line, and what that line never holds."""

import json
import logging

import pytest
from chat_api import FACT, Api, ask_script, happy_script
from fakes import FakeLlm
from graph_harness import Harness
from test_api_chat import of

from assistant.graph import nodes
from assistant.llm import LlmError
from assistant.timing import TurnTimer, log_turn

pytestmark = pytest.mark.db


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_spans_are_measured():
    clock = Clock()
    timer = TurnTimer(clock)
    async with timer.span("model", "m"):
        clock.now = 0.120
    async with timer.span("tool", "save_draft"):
        clock.now = 0.150
    summary = timer.summary()
    assert summary["model"] == [{"name": "m", "ms": 120}]
    assert summary["tool"] == [{"name": "save_draft", "ms": 30}]
    assert summary["step"] == []
    assert summary["total_ms"] == 150
    assert summary["first_text_ms"] is None


async def test_a_failing_span_is_still_measured():
    clock = Clock()
    timer = TurnTimer(clock)
    with pytest.raises(ValueError):
        async with timer.span("step", "converse"):
            clock.now = 0.05
            raise ValueError("x")
    assert timer.summary()["step"] == [{"name": "converse", "ms": 50}]


def test_first_text_is_the_first_mark_only():
    clock = Clock()
    timer = TurnTimer(clock)
    clock.now = 0.4
    timer.mark_first_text()
    clock.now = 0.9
    timer.mark_first_text()
    assert timer.summary()["first_text_ms"] == 400


def records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "assistant.timing"]


@pytest.fixture
def timing_log(monkeypatch, caplog):
    own = logging.getLogger("assistant")  # it does not propagate to the root logger, where caplog listens
    monkeypatch.setattr(own, "handlers", [*own.handlers, caplog.handler])
    with caplog.at_level("INFO"):
        yield caplog


def line(record: logging.LogRecord) -> dict:
    assert record.msg == "turn timing %s"
    return json.loads(record.args[0])  # type: ignore[index]


async def test_turn_logs_one_timing_line(api: Api, timing_log):
    api.llm.script = happy_script()
    events = await api.send(FACT)
    assert events[-1][0] == "done"
    [rec] = records(timing_log)
    data = line(rec)
    assert data["outcome"] == "done" and data["app"] == "real"
    assert data["first_text_ms"] is not None
    assert {s["name"] for s in data["step"]} == {"read_files", "load_context", "converse"}
    assert {m["name"] for m in data["model"]} >= {
        "chat/model",
        "writer/model",
        "cheap/model",
    }  # the writer's calls are timed too


async def test_timing_is_logged_once_when_the_turn_fails(api: Api, timing_log):
    api.llm.script = {"CONVERSE": [LlmError("unavailable", "down", cost_usd=0.002)]}
    events = await api.send("shipped auto-reply")
    assert events[-1][0] == "failed"
    [rec] = records(timing_log)
    assert line(rec)["outcome"] == "failed"


async def test_timing_line_holds_no_user_text(api: Api, timing_log):
    api.llm.script = ask_script()
    secret = "my secret launch plan for forty customers"
    events = await api.send(secret)
    assert events[-1][0] == "done"
    [rec] = records(timing_log)
    text = rec.getMessage()
    assert secret not in text
    for post in api.tools._context.recent_posts:
        assert post not in text
    assert api.tools._context.voice not in text
    assert of(events, "text")  # the reply streamed, so first_text_ms is set
    assert line(rec)["first_text_ms"] is not None


def test_log_turn_is_one_compact_info_line(caplog):
    timer = TurnTimer(Clock())
    with caplog.at_level("INFO", logger="assistant.timing"):
        log_turn(timer, "real", "capped")
    [rec] = records(caplog)
    assert rec.levelno == logging.INFO
    assert line(rec)["outcome"] == "capped"
    assert " " not in rec.args[0]  # type: ignore[index]


async def test_context_is_reused_for_ten_minutes(monkeypatch):
    assert nodes.CONTEXT_TTL == 600
    clock = [1_000.0]
    monkeypatch.setattr(nodes, "_now", lambda: clock[0])
    h = Harness(FakeLlm({"CONVERSE": ["A", "B"]}))
    await h.send("one")
    clock[0] += 599
    await h.send("two")
    assert h.tools.calls == ["context"]
