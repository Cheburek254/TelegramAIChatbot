import asyncio
from dataclasses import replace
from unittest.mock import Mock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.config import Settings
from app.llm import LLMClient, LLMError


def configuration(base_url):
    return Settings(
        bot_token="unused",
        postgres_password="unused",
        openrouter_api_key="fake-primary",
        groq_api_key="fake-reserve",
        openrouter_base_url=base_url + "/primary",
        groq_base_url=base_url + "/reserve",
        openrouter_model="primary-model:free",
        groq_model="reserve-model",
    )


async def test_http_contract_and_fallback_are_bounded_and_preserve_temperature(caplog):
    # Arrange
    calls = []

    async def endpoint(request):
        calls.append((request.path, await request.json(), request.headers["Authorization"]))
        if request.path.startswith("/primary"):
            return web.json_response({"error": "private provider detail"}, status=503)
        return web.json_response(
            {
                "model": "reserve-model",
                "choices": [{"message": {"content": "Успех"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 14, "completion_tokens": 2, "unknown": "private"},
            }
        )

    app = web.Application()
    app.router.add_post("/{provider}/chat/completions", endpoint)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = LLMClient(configuration(str(server.make_url("")).rstrip("/")), session)
        # Act
        result = await client.complete([{"role": "user", "content": "personal text"}], 0.7, "req")
    # Assert
    assert result.content == "Успех"
    assert result.provider == "groq"
    assert result.usage == {"prompt_tokens": 14, "completion_tokens": 2}
    assert len(calls) == 2
    assert calls[0][1]["temperature"] == calls[1][1]["temperature"] == 0.7
    assert calls[0][1]["messages"] == calls[1][1]["messages"]
    assert calls[0][2] == "Bearer fake-primary"
    assert calls[1][2] == "Bearer fake-reserve"
    assert calls[0][1]["provider"]["max_price"] == {"prompt": 0, "completion": 0, "request": 0}
    assert calls[0][1]["reasoning"] == {"enabled": False}
    assert "provider" not in calls[1][1]
    assert "personal text" not in caplog.text
    assert "private provider detail" not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": "answer"}, "finish_reason": "content_filter"}]},
        {"choices": [{"message": {"content": "answer"}}], "usage": []},
    ],
)
async def test_invalid_or_empty_response_is_safe_error(body):
    # Arrange
    async def endpoint(request):
        return web.json_response(body)

    app = web.Application()
    app.router.add_post("/{provider}/chat/completions", endpoint)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = LLMClient(configuration(str(server.make_url("")).rstrip("/")), session)
        # Act / Assert
        with pytest.raises(LLMError, match="invalid_response"):
            await client.complete([], 0.3, "req", allow_fallback=False)


async def test_invalid_json_and_large_body_are_rejected():
    # Arrange
    count = 0

    async def endpoint(request):
        nonlocal count
        count += 1
        return web.Response(body=b"not JSON" if count == 1 else b"x" * 2_000_001)

    app = web.Application()
    app.router.add_post("/{provider}/chat/completions", endpoint)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = LLMClient(configuration(str(server.make_url("")).rstrip("/")), session)
        # Act / Assert
        for _ in range(2):
            with pytest.raises(LLMError, match="invalid_response"):
                await client.complete([], 0.3, "req", allow_fallback=False)


async def test_timeout_has_finite_limit_and_does_not_leak_exception():
    # Arrange
    session = Mock()
    session.post.side_effect = TimeoutError("secret provider detail")
    config = replace(configuration("https://example.invalid"), llm_timeout=17)
    client = LLMClient(config, session)
    # Act / Assert
    with pytest.raises(LLMError, match="^timeout$"):
        await client.complete([], 0.3, "req")
    assert session.post.call_count == 2
    assert session.post.call_args.kwargs["timeout"].total == 17
    assert session.post.call_args.kwargs["allow_redirects"] is False


async def test_concurrent_requests_do_not_block_event_loop_and_mark_truncation():
    # Arrange
    received = 0
    both_received = asyncio.Event()

    async def endpoint(request):
        nonlocal received
        received += 1
        if received == 2:
            both_received.set()
        await asyncio.wait_for(both_received.wait(), timeout=2)
        return web.json_response(
            {"choices": [{"message": {"content": "часть"}, "finish_reason": "length"}]}
        )

    app = web.Application()
    app.router.add_post("/{provider}/chat/completions", endpoint)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        client = LLMClient(configuration(str(server.make_url("")).rstrip("/")), session)
        # Act
        results = await asyncio.gather(client.complete([], 0.3, "1"), client.complete([], 0.7, "2"))
    # Assert
    assert all(r.truncated for r in results)
    assert received == 2


async def test_paid_openrouter_model_never_reaches_http():
    # Arrange
    session = Mock()
    settings = replace(configuration("https://example.invalid"), openrouter_model="paid/model")
    # Act / Assert
    with pytest.raises(LLMError, match="paid_model_blocked"):
        await LLMClient(settings, session).complete([], 0.3, "req", allow_fallback=False)
    session.post.assert_not_called()
