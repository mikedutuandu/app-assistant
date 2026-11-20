import base64
import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from assistant.llm import Llm, LlmError

CHAT = "https://openrouter.ai/api/v1/chat/completions"
TRANSCRIBE = "https://openrouter.ai/api/v1/audio/transcriptions"
MSGS = [{"role": "user", "content": "hi"}]


def ok(text: str, cost: float | None = 0.001) -> httpx.Response:
    body: dict = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if cost is not None:
        body["usage"] = {"cost": cost}
    return httpx.Response(200, json=body)


def sse(*events: str) -> httpx.Response:
    return httpx.Response(200, content="".join(events).encode(), headers={"content-type": "text/event-stream"})


def chunk(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@respx.mock
async def test_chat_returns_text_and_cost():
    route = respx.post(CHAT).mock(return_value=ok("hello", 0.0042))
    res = await Llm("k").chat(MSGS, "a/m", [], 100)
    assert (res.text, res.cost_usd, res.model) == ("hello", 0.0042, "a/m")
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer k"
    body = json.loads(req.content)
    assert body["model"] == "a/m" and body["max_tokens"] == 100 and body["usage"] == {"include": True}
    assert body["messages"] == MSGS


@respx.mock
async def test_missing_usage_costs_zero():
    respx.post(CHAT).mock(return_value=ok("x", None))
    assert (await Llm("k").chat(MSGS, "a/m", [], 10)).cost_usd == 0.0


@respx.mock
async def test_falls_back_on_429():
    route = respx.post(CHAT).mock(
        side_effect=[
            httpx.Response(429, json={"error": {"message": "slow down"}, "usage": {"cost": 0.5}}),
            ok("b", 0.25),
        ]
    )
    res = await Llm("k").chat(MSGS, "a/m", ["b/m"], 10)
    assert res.text == "b" and res.model == "b/m"
    assert res.cost_usd == pytest.approx(0.75)
    assert [json.loads(c.request.content)["model"] for c in route.calls] == ["a/m", "b/m"]


@respx.mock
async def test_fallback_on_5xx_timeout_and_model_not_found_sums_costs():
    respx.post(CHAT).mock(
        side_effect=[
            httpx.Response(503, json={"usage": {"cost": 0.1}}),
            httpx.ConnectTimeout("t"),
            httpx.Response(404, json={"error": {"message": "No endpoints found: model not found"}}),
            ok("d", 0.2),
        ]
    )
    res = await Llm("k").chat(MSGS, "a", ["b", "c", "d"], 10)
    assert res.model == "d" and res.cost_usd == pytest.approx(0.3)


@respx.mock
async def test_all_models_failing_is_unavailable_with_cost():
    respx.post(CHAT).mock(return_value=httpx.Response(500, json={"usage": {"cost": 0.1}}))
    with pytest.raises(LlmError) as e:
        await Llm("k").chat(MSGS, "a", ["b"], 10)
    assert e.value.kind == "unavailable" and e.value.cost_usd == pytest.approx(0.2)


@respx.mock
async def test_no_fallback_on_400():
    route = respx.post(CHAT).mock(return_value=httpx.Response(400, json={"error": {"message": "bad input"}}))
    with pytest.raises(LlmError) as e:
        await Llm("k").chat(MSGS, "a/m", ["b/m"], 10)
    assert e.value.kind == "bad_request"
    assert route.call_count == 1


@respx.mock
async def test_stream_calls_on_text_in_order():
    route = respx.post(CHAT).mock(
        return_value=sse(
            ": OPENROUTER PROCESSING\n\n",
            chunk({"choices": [{"delta": {"content": "Hel"}}]}),
            chunk({"choices": [{"delta": {"content": "lo"}}]}),
            chunk({"choices": [{"delta": {}}], "usage": {"cost": 0.002}}),
            "data: [DONE]\n\n",
        )
    )
    seen: list[str] = []

    async def on_text(t: str) -> None:
        seen.append(t)

    res = await Llm("k").chat(MSGS, "a/m", [], 10, on_text=on_text)
    assert seen == ["Hel", "lo"]
    assert (res.text, res.cost_usd) == ("Hello", 0.002)
    body = json.loads(route.calls[0].request.content)
    assert body["stream"] is True and body["usage"] == {"include": True}


@respx.mock
async def test_stream_falls_back_on_429():
    respx.post(CHAT).mock(
        side_effect=[
            httpx.Response(429, json={}),
            sse(chunk({"choices": [{"delta": {"content": "ok"}}], "usage": {"cost": 0.1}}), "data: [DONE]\n\n"),
        ]
    )
    seen: list[str] = []

    async def on_text(t: str) -> None:
        seen.append(t)

    res = await Llm("k").chat(MSGS, "a", ["b"], 10, on_text=on_text)
    assert res.text == "ok" and seen == ["ok"] and res.model == "b"


class Out(BaseModel):
    answer: int


@respx.mock
async def test_chat_json_parses_and_sends_schema():
    route = respx.post(CHAT).mock(return_value=ok('{"answer": 4}', 0.01))
    out, cost = await Llm("k").chat_json(MSGS, "a/m", [], Out, 50)
    assert out == Out(answer=4) and cost == 0.01
    rf = json.loads(route.calls[0].request.content)["response_format"]
    assert rf == {
        "type": "json_schema",
        "json_schema": {"name": "Out", "schema": Out.model_json_schema(), "strict": True},
    }


@respx.mock
async def test_chat_json_repairs_once_then_raises():
    route = respx.post(CHAT).mock(side_effect=[ok("not json", 0.01), ok('{"answer": "x"}', 0.02)])
    with pytest.raises(LlmError) as e:
        await Llm("k").chat_json(MSGS, "a/m", [], Out, 50)
    assert e.value.kind == "bad_output" and e.value.cost_usd == pytest.approx(0.03)
    assert route.call_count == 2
    repair = json.loads(route.calls[1].request.content)["messages"]
    assert repair[:1] == MSGS
    assert repair[1] == {"role": "assistant", "content": "not json"}
    assert repair[2]["role"] == "user" and "valid" in repair[2]["content"].lower()


@respx.mock
async def test_chat_json_repair_succeeds():
    respx.post(CHAT).mock(side_effect=[ok("nope", 0.01), ok('{"answer": 7}', 0.02)])
    out, cost = await Llm("k").chat_json(MSGS, "a/m", [], Out, 50)
    assert out.answer == 7 and cost == pytest.approx(0.03)


@respx.mock
async def test_transcribe_sends_base64_and_format():
    route = respx.post(TRANSCRIBE).mock(
        return_value=httpx.Response(200, json={"text": "hello there", "usage": {"seconds": 3.5, "cost": 0.0003}})
    )
    text, seconds, cost = await Llm("k").transcribe(b"\x01\x02audio", "webm")
    assert (text, seconds, cost) == ("hello there", 3.5, 0.0003)
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "model": "openai/whisper-large-v3-turbo",
        "input_audio": {"data": base64.b64encode(b"\x01\x02audio").decode(), "format": "webm"},
    }


@respx.mock
async def test_transcribe_error_raises():
    respx.post(TRANSCRIBE).mock(return_value=httpx.Response(502, json={}))
    with pytest.raises(LlmError) as e:
        await Llm("k").transcribe(b"x", "mp3")
    assert e.value.kind == "unavailable"


@respx.mock
async def test_stream_error_after_text_raises_without_fallback():
    route = respx.post(CHAT).mock(
        side_effect=[
            sse(
                chunk({"choices": [{"delta": {"content": "Par"}}]}),
                chunk(
                    {
                        "error": {"message": "provider died"},
                        "choices": [{"delta": {}, "finish_reason": "error"}],
                        "usage": {"cost": 0.04},
                    }
                ),
            ),
            sse(chunk({"choices": [{"delta": {"content": "SECOND"}}]}), "data: [DONE]\n\n"),
        ]
    )
    seen: list[str] = []

    async def on_text(t: str) -> None:
        seen.append(t)

    with pytest.raises(LlmError) as e:
        await Llm("k").chat(MSGS, "a", ["b"], 10, on_text=on_text)
    assert e.value.kind == "unavailable" and e.value.cost_usd == pytest.approx(0.04)
    assert route.call_count == 1
    assert seen == ["Par"]


@respx.mock
async def test_stream_error_event_before_text_falls_back():
    respx.post(CHAT).mock(
        side_effect=[
            sse(chunk({"choices": [{"delta": {}, "finish_reason": "error"}], "error": {"message": "x"}})),
            sse(chunk({"choices": [{"delta": {"content": "fine"}}], "usage": {"cost": 0.1}}), "data: [DONE]\n\n"),
        ]
    )
    seen: list[str] = []

    async def on_text(t: str) -> None:
        seen.append(t)

    res = await Llm("k").chat(MSGS, "a", ["b"], 10, on_text=on_text)
    assert res.text == "fine" and res.model == "b" and seen == ["fine"]
