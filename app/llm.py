"""Асинхронный OpenAI-совместимый клиент: OpenRouter и резерв Groq."""

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field

import aiohttp

from app.config import Settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, code: str, *, retry_after: float = 0, failure_kind: str | None = None):
        self.code = code
        self.retry_after = retry_after
        self.failure_kind = failure_kind
        super().__init__(code)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict

    def message(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    cost: float | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class LLMClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession):
        self.settings = settings
        self.session = session
        self.capacity = asyncio.Semaphore(4)

    async def complete(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        request_id: str,
        *,
        allow_fallback: bool = True,
        tools: list[dict] | None = None,
        response_schema: dict | None = None,
        tool_choice: dict | str | None = None,
    ) -> Completion:
        primary = self.settings.llm_provider
        providers = [primary]
        if allow_fallback:
            providers.append("groq" if primary == "openrouter" else "openrouter")
        error = LLMError("unavailable")
        async with self.capacity:
            for provider in providers:
                key = getattr(self.settings, f"{provider}_api_key")
                if not key:
                    continue
                model = getattr(self.settings, f"{provider}_model")
                started = time.monotonic()
                logger.info(
                    "llm_start request=%s provider=%s model=%s", request_id, provider, model
                )
                try:
                    result = (
                        await self._request(
                            provider,
                            key,
                            model,
                            messages,
                            temperature,
                            tools=tools,
                            response_schema=response_schema,
                            tool_choice=tool_choice,
                        )
                        if tools is not None
                        else await self._request(provider, key, model, messages, temperature)
                    )
                except LLMError as exc:
                    error = exc
                    logger.warning(
                        "llm_end request=%s provider=%s model=%s status=%s seconds=%.3f",
                        request_id,
                        provider,
                        model,
                        exc.code,
                        time.monotonic() - started,
                    )
                else:
                    logger.info(
                        "llm_end request=%s provider=%s model=%s status=ok seconds=%.3f",
                        request_id,
                        provider,
                        result.model,
                        time.monotonic() - started,
                    )
                    return result
        raise error

    async def _request(
        self,
        provider,
        key,
        model,
        messages,
        temperature,
        *,
        tools=None,
        response_schema=None,
        tool_choice=None,
    ) -> Completion:
        if provider == "openrouter" and not model.endswith(":free"):
            raise LLMError("paid_model_blocked")
        url = getattr(self.settings, f"{provider}_base_url") + "/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": self.settings.llm_max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
            if isinstance(tool_choice, dict):
                payload["max_tokens"] = min(self.settings.llm_max_tokens, 512)
        if response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "agent_answer", "strict": True, "schema": response_schema},
            }
        if provider == "openrouter":
            payload["reasoning"] = {"enabled": False}
            payload["provider"] = {
                "max_price": {"prompt": 0, "completion": 0, "request": 0},
                "require_parameters": True,
            }
        elif model == "qwen/qwen3.8-27b":
            payload["reasoning_effort"] = "none"
        try:
            async with self.session.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.settings.llm_timeout),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    retry_after = 0.0
                    failure_kind = None
                    header = response.headers.get("retry-after") or response.headers.get(
                        "x-ratelimit-reset-tokens", ""
                    )
                    if header.replace(".", "", 1).isdigit():
                        retry_after = float(header)
                    else:
                        retry_after = sum(
                            float(value) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
                            for value, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", header)
                        )
                    try:
                        error_raw = await response.content.read(16385)
                        error_data = json.loads(error_raw) if len(error_raw) <= 16384 else {}
                        raw_kind = error_data.get("error", {}).get("code")
                        if raw_kind in {"tool_use_failed", "json_validate_failed"}:
                            failure_kind = raw_kind
                    except (ValueError, TypeError, AttributeError, aiohttp.ClientError):
                        pass
                    raise LLMError(
                        f"http_{response.status}",
                        retry_after=retry_after,
                        failure_kind=failure_kind,
                    )
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise LLMError("invalid_response")
                data = json.loads(body)
        except TimeoutError:
            raise LLMError("timeout") from None
        except (aiohttp.ClientError, OSError):
            raise LLMError("unavailable") from None
        except (ValueError, UnicodeError):
            raise LLMError("invalid_response") from None
        try:
            choice = data["choices"][0]
            message = choice["message"]
            text = message.get("content")
            calls = []
            if tools is not None and message.get("tool_calls"):
                raw_calls = message["tool_calls"]
                if not isinstance(raw_calls, list) or not 1 <= len(raw_calls) <= 16:
                    raise ValueError
                for call in raw_calls:
                    if (
                        call["type"] != "function"
                        or not isinstance(call["id"], str)
                        or not call["id"]
                    ):
                        raise ValueError
                    function = call["function"]
                    arguments = json.loads(function["arguments"])
                    if not isinstance(arguments, dict) or not isinstance(function["name"], str):
                        raise ValueError
                    calls.append(ToolCall(call["id"], function["name"], arguments))
                if len({call.id for call in calls}) != len(calls):
                    raise ValueError
                text = text or ""
            if not isinstance(text, str) or (not text.strip() and not calls):
                raise ValueError
            finish = choice.get("finish_reason")
            if finish not in (
                None,
                "stop",
                "length",
                *(("tool_calls",) if tools is not None else ()),
            ):
                raise ValueError
            usage = data.get("usage")
            if usage is None:
                usage = {}
            if not isinstance(usage, dict):
                raise ValueError
            cost = usage.get("cost")
            if cost is not None:
                if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                    raise ValueError
                cost = float(cost)
                if provider == "openrouter" and cost != 0:
                    raise LLMError("unexpected_cost")
            usage = {
                key: value
                for key, value in usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and type(value) is int
                and value >= 0
            }
            actual_model = data.get("model") or model
            if not isinstance(actual_model, str):
                raise ValueError
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            raise LLMError("invalid_response") from None
        return Completion(
            text.strip(), actual_model, provider, usage, finish == "length", cost, tuple(calls)
        )
