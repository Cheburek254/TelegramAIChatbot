import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.llm import LLMClient, LLMError
from tests.test_llm import configuration


async def test_native_tools_http_contract_and_parsed_arguments():
    captured = []

    async def endpoint(request):
        captured.append(await request.json())
        return web.json_response(
            {
                "model": "primary-model:free",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_schedule",
                                        "arguments": '{"date":"2026-10-12"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"cost": 0},
            }
        )

    app = web.Application()
    app.router.add_post("/primary/chat/completions", endpoint)
    tools = [
        {"type": "function", "function": {"name": "get_schedule", "parameters": {"type": "object"}}}
    ]
    choice = {"type": "function", "function": {"name": "get_schedule"}}
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        result = await LLMClient(
            configuration(str(server.make_url("")).rstrip("/")), session
        ).complete(
            [{"role": "user", "content": "Сегодня"}],
            0.3,
            "native-test",
            tools=tools,
            tool_choice=choice,
            allow_fallback=False,
        )
    assert result.tool_calls[0].arguments == {"date": "2026-10-12"}
    assert result.content == "" and result.cost == 0
    assert captured[0]["tools"] == tools and captured[0]["tool_choice"] == choice
    assert captured[0]["provider"]["max_price"] == {"prompt": 0, "completion": 0, "request": 0}


@pytest.mark.parametrize("arguments", ["not json", "[]", "null"])
async def test_native_invalid_arguments_are_rejected(arguments):
    async def endpoint(request):
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "get_schedule", "arguments": arguments},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/primary/chat/completions", endpoint)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        with pytest.raises(LLMError, match="invalid_response"):
            await LLMClient(configuration(str(server.make_url("")).rstrip("/")), session).complete(
                [], 0.3, "invalid-test", tools=[], allow_fallback=False
            )


async def test_strict_schema_is_transmitted_for_router():
    captured = []
    schema = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
        "additionalProperties": False,
    }

    async def endpoint(request):
        captured.append(await request.json())
        return web.json_response(
            {
                "choices": [
                    {
                        "message": {"content": json.dumps({"action": "respond"})},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/primary/chat/completions", endpoint)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        await LLMClient(configuration(str(server.make_url("")).rstrip("/")), session).complete(
            [], 0.0, "schema-test", tools=[], response_schema=schema, allow_fallback=False
        )
    assert captured[0]["response_format"]["json_schema"]["strict"] is True
    assert captured[0]["response_format"]["json_schema"]["schema"] == schema
    assert "tools" not in captured[0]
