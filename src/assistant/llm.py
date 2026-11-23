"""OpenRouter client: chat (optionally streamed), JSON-schema chat with one repair, and transcription.

Every call reports the cost OpenRouter charged (`usage.cost`); callers charge it to the user.
"""

import base64
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

BASE_URL = "https://openrouter.ai/api/v1"
TRANSCRIBE_MODEL = "openai/whisper-large-v3-turbo"
CHAT_TIMEOUT = 60.0
TRANSCRIBE_TIMEOUT = 120.0

ErrorKind = Literal["bad_output", "unavailable", "bad_request"]


class LlmError(Exception):
    def __init__(self, kind: ErrorKind, message: str = "", cost_usd: float = 0.0):
        super().__init__(f"{kind}: {message}" if message else kind)
        self.kind = kind
        self.cost_usd = cost_usd


@dataclass
class LlmResult:
    text: str
    cost_usd: float
    model: str


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict = {}
    bad_arguments: str | None = None  # the raw text when the arguments were not valid JSON


class ToolTurn(BaseModel):
    text: str
    calls: list[ToolCall]
    cost_usd: float
    model: str


def _tool_calls(raw: dict[int, dict[str, str]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index in sorted(raw):
        item = raw[index]
        arguments: dict = {}
        bad: str | None = None
        text = item["arguments"]
        try:
            parsed = json.loads(text) if text.strip() else {}
            if isinstance(parsed, dict):
                arguments = parsed
            else:
                bad = text
        except ValueError:
            bad = text
        calls.append(ToolCall(id=item["id"], name=item["name"], arguments=arguments, bad_arguments=bad))
    return calls


class _Retryable(Exception):
    def __init__(self, message: str, cost: float = 0.0):
        super().__init__(message)
        self.cost = cost


class _Partial(Exception):
    """A stream failed after text was already emitted: no fallback is possible."""

    def __init__(self, message: str, cost: float = 0.0):
        super().__init__(message)
        self.cost = cost


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _cost(usage: Any) -> float:
    """What a call cost in US dollars, from OpenRouter's `usage`; 0 when it does not say.

    A call on our own provider key (OpenRouter's BYOK: Claude on the Anthropic key, paid from the Claude plan's
    monthly API credits) comes back with `cost` as OpenRouter's fee alone, often 0, and what the provider charged in
    `cost_details.upstream_inference_cost`. Both are added, so the user is charged what the call cost, whichever key
    paid for it. On OpenRouter's credits `cost` holds everything and the upstream cost is part of it, never more.
    """
    if not isinstance(usage, dict):
        return 0.0
    cost = _number(usage.get("cost")) or 0.0
    details = usage.get("cost_details")
    upstream = (_number(details.get("upstream_inference_cost")) if isinstance(details, dict) else None) or 0.0
    return cost + upstream if usage.get("is_byok") is True else max(cost, upstream)


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message", ""))
        if isinstance(err, str):
            return err
    return ""


def _model_unavailable(status: int, message: str) -> bool:
    if status not in (400, 404):
        return False
    m = message.lower()
    return status == 404 or (
        "model" in m and any(w in m for w in ("not found", "unavailable", "not available", "no endpoints"))
    )


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {}


def _classify(resp: httpx.Response, body: Any) -> None:
    """Raise _Retryable or LlmError("bad_request") for a non-2xx response."""
    status = resp.status_code
    message = _error_message(body)
    cost = _cost(body.get("usage")) if isinstance(body, dict) else 0.0
    if status == 429 or status >= 500 or _model_unavailable(status, message):
        raise _Retryable(f"HTTP {status}", cost)
    raise LlmError("bad_request", f"HTTP {status}: {message}"[:300], cost)


class Llm:
    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None, referer: str = ""):
        self._key = api_key
        self._client = client or httpx.AsyncClient()
        self._referer = referer

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._key}", "X-Title": "app-assistant"}
        if self._referer:
            headers["HTTP-Referer"] = self._referer  # OpenRouter's app attribution: this deployment's public URL
        return headers

    async def _once(
        self,
        body: dict,
        on_text: Callable[[str], Awaitable[None]] | None,
        stream: bool | None = None,
    ) -> tuple[str, list[ToolCall], float]:
        if stream is None:
            stream = on_text is not None
        if not stream:
            resp = await self._client.post(
                f"{BASE_URL}/chat/completions", json=body, headers=self._headers(), timeout=CHAT_TIMEOUT
            )
            data = _json(resp)
            if resp.status_code >= 400:
                _classify(resp, data)
            cost = _cost(data.get("usage")) if isinstance(data, dict) else 0.0
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError) as e:
                raise _Retryable("malformed response", cost) from e
            return text, [], cost

        parts: list[str] = []
        raw_calls: dict[int, dict[str, str]] = {}
        cost = 0.0

        def sent() -> bool:
            return on_text is not None and bool(parts)  # text nobody received can still fall back

        def fail(message: str) -> Exception:
            return _Partial(message, cost) if sent() else _Retryable(message, cost)

        try:
            async with self._client.stream(
                "POST",
                f"{BASE_URL}/chat/completions",
                json={**body, "stream": True},
                headers=self._headers(),
                timeout=CHAT_TIMEOUT,
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    _classify(resp, _json(resp))
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # blank lines and ":" comments
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("usage"):
                        cost += _cost(event["usage"])
                    choices = event.get("choices") or []
                    first = choices[0] if choices else {}
                    if event.get("error") or first.get("finish_reason") == "error":
                        raise fail("stream error")
                    delta = first.get("delta") or {}
                    for tc in delta.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        slot = raw_calls.setdefault(int(tc.get("index") or 0), {"id": "", "name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    piece = delta.get("content")
                    if piece:
                        parts.append(piece)
                        if on_text is not None:
                            await on_text(piece)
        except httpx.TransportError as e:
            if sent():
                raise _Partial(type(e).__name__, cost) from None
            raise
        except httpx.TimeoutException as e:
            if sent():
                raise _Partial(type(e).__name__, cost) from None
            raise
        return "".join(parts), _tool_calls(raw_calls), cost

    async def _complete(
        self,
        messages: list[dict],
        model: str,
        fallbacks: list[str],
        max_tokens: int,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
        reasoning: str | None = None,
        temperature: float | None = None,
    ) -> ToolTurn:
        spent = 0.0
        last = "no model"
        for m in [model, *fallbacks]:
            body: dict = {"model": m, "messages": messages, "max_tokens": max_tokens, "usage": {"include": True}}
            if response_format is not None:
                body["response_format"] = response_format
            if tools:
                body["tools"] = tools
            if reasoning:
                body["reasoning"] = {"effort": reasoning}
            if temperature is not None:
                body["temperature"] = temperature
            try:
                text, calls, cost = await self._once(body, on_text, stream=True if tools else None)
            except _Retryable as e:
                spent += e.cost
                last = str(e)
            except _Partial as e:
                raise LlmError("unavailable", f"stream failed after output ({e})", spent + e.cost) from None
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = type(e).__name__
            except LlmError as e:
                raise LlmError(e.kind, str(e), spent + e.cost_usd) from None
            else:
                return ToolTurn(text=text, calls=calls, cost_usd=spent + cost, model=m)
        raise LlmError("unavailable", f"every model failed ({last})", spent)

    async def chat(
        self,
        messages: list[dict],
        model: str,
        fallbacks: list[str],
        max_tokens: int,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        reasoning: str | None = None,
    ) -> LlmResult:
        turn = await self._complete(messages, model, fallbacks, max_tokens, on_text, reasoning=reasoning)
        return LlmResult(text=turn.text, cost_usd=turn.cost_usd, model=turn.model)

    async def chat_tools(
        self,
        messages: list[dict],
        model: str,
        fallbacks: list[str],
        tools: list[dict],
        max_tokens: int,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        reasoning: str | None = None,
        temperature: float | None = None,
    ) -> ToolTurn:
        return await self._complete(
            messages, model, fallbacks, max_tokens, on_text, tools=tools, reasoning=reasoning, temperature=temperature
        )

    async def chat_json(
        self,
        messages: list[dict],
        model: str,
        fallbacks: list[str],
        schema: type[BaseModel],
        max_tokens: int,
        reasoning: str | None = None,
    ) -> tuple[BaseModel, float]:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": True},
        }
        total = 0.0
        history = messages
        for attempt in range(2):
            try:
                res = await self._complete(
                    history, model, fallbacks, max_tokens, response_format=response_format, reasoning=reasoning
                )
            except LlmError as e:
                raise LlmError(e.kind, str(e), total + e.cost_usd) from None
            total += res.cost_usd
            try:
                return schema.model_validate_json(res.text), total
            except ValidationError as e:
                if attempt == 1:
                    raise LlmError("bad_output", "invalid JSON after one repair", total) from None
                history = [
                    *messages,
                    {"role": "assistant", "content": res.text},
                    {
                        "role": "user",
                        "content": "That was not valid for the required JSON schema: "
                        f"{e}. Reply again with only valid JSON that matches the schema.",
                    },
                ]
        raise AssertionError("unreachable")

    async def transcribe(self, audio: bytes, format: str) -> tuple[str, float, float]:
        body = {
            "model": TRANSCRIBE_MODEL,
            "input_audio": {"data": base64.b64encode(audio).decode(), "format": format},
        }
        try:
            resp = await self._client.post(
                f"{BASE_URL}/audio/transcriptions", json=body, headers=self._headers(), timeout=TRANSCRIBE_TIMEOUT
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise LlmError("unavailable", type(e).__name__) from None
        data = _json(resp)
        if resp.status_code >= 400:
            try:
                _classify(resp, data)
            except _Retryable as e:
                raise LlmError("unavailable", str(e), e.cost) from None
        usage = data.get("usage") if isinstance(data, dict) else None
        seconds = float(usage.get("seconds", 0.0)) if isinstance(usage, dict) else 0.0
        return str(data.get("text", "")), seconds, _cost(usage)
