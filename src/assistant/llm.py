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


class _Retryable(Exception):
    def __init__(self, message: str, cost: float = 0.0):
        super().__init__(message)
        self.cost = cost


class _Partial(Exception):
    """A stream failed after text was already emitted: no fallback is possible."""

    def __init__(self, message: str, cost: float = 0.0):
        super().__init__(message)
        self.cost = cost


def _cost(usage: Any) -> float:
    if isinstance(usage, dict):
        value = usage.get("cost")
        if isinstance(value, int | float):
            return float(value)
    return 0.0


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
    ) -> tuple[str, float]:
        if on_text is None:
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
            return text, cost

        parts: list[str] = []
        cost = 0.0

        def fail(message: str) -> Exception:
            return _Partial(message, cost) if parts else _Retryable(message, cost)

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
                    piece = (first.get("delta") or {}).get("content")
                    if piece:
                        parts.append(piece)
                        await on_text(piece)
        except httpx.TransportError as e:
            if parts:
                raise _Partial(type(e).__name__, cost) from None
            raise
        except httpx.TimeoutException as e:
            if parts:
                raise _Partial(type(e).__name__, cost) from None
            raise
        return "".join(parts), cost

    async def _complete(
        self,
        messages: list[dict],
        model: str,
        fallbacks: list[str],
        max_tokens: int,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        response_format: dict | None = None,
    ) -> LlmResult:
        spent = 0.0
        last = "no model"
        for m in [model, *fallbacks]:
            body: dict = {"model": m, "messages": messages, "max_tokens": max_tokens, "usage": {"include": True}}
            if response_format is not None:
                body["response_format"] = response_format
            try:
                text, cost = await self._once(body, on_text)
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
                return LlmResult(text=text, cost_usd=spent + cost, model=m)
        raise LlmError("unavailable", f"every model failed ({last})", spent)

    async def chat(
        self,
        messages: list[dict],
        model: str,
        fallbacks: list[str],
        max_tokens: int,
        on_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> LlmResult:
        return await self._complete(messages, model, fallbacks, max_tokens, on_text)

    async def chat_json(
        self,
        messages: list[dict],
        model: str,
        fallbacks: list[str],
        schema: type[BaseModel],
        max_tokens: int,
    ) -> tuple[BaseModel, float]:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": True},
        }
        total = 0.0
        history = messages
        for attempt in range(2):
            try:
                res = await self._complete(history, model, fallbacks, max_tokens, response_format=response_format)
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
