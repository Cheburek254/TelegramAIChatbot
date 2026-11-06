"""Контракты, время, внешняя граница и настоящий stdio без внешних API."""

import json
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl, ValidationError

from app.agent_store import ActionError
from app.config import Settings
from app.mcp_client import MCPBridge, MCPFailure
from app.mcp_service.auth import sign, verify
from app.mcp_service.contracts import ReminderInput, ScheduleInput, WeatherInput
from app.mcp_service.weather import ToolFailure, Weather
from app.timezones import reminder_time, zone

NOW = datetime(2026, 10, 12, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    "payload", [{"city": ""}, {"city": " "}, {"city": "x" * 101}, {"city": "Казань", "user_id": 1}]
)
def test_weather_contract_rejects_invalid_fields(payload):
    with pytest.raises(ValidationError):
        WeatherInput.model_validate(payload)


@pytest.mark.parametrize("value", ["2026-02-30", "12.10.2026", "2026-1-1"])
def test_schedule_rejects_invalid_date(value):
    with pytest.raises(ValidationError):
        ScheduleInput(date=value)


@pytest.mark.parametrize(
    "payload",
    [
        {"text": " ", "remind_at": "2026-11-01T10:00:00+03:00"},
        {"text": "дело", "remind_at": "2026-11-01T10:00:00"},
        {"text": "дело", "remind_at": "2026-11-01T10:00:00+03:00", "owner": 42},
    ],
)
def test_reminder_strict_contract(payload):
    with pytest.raises(ValidationError):
        ReminderInput.model_validate(payload)


@pytest.mark.parametrize(
    "value,timezone,code",
    [
        ("2026-10-11T09:00:00+03:00", "Europe/Moscow", "past_time"),
        ("2026-10-13T09:00:00", "Europe/Moscow", "timezone_required"),
        ("2026-10-13T09:00:00+01:00", "Europe/Moscow", "timezone_mismatch"),
        ("2026-11-01T01:30:00-04:00", "America/New_York", "ambiguous_local_time"),
        ("2027-03-14T02:30:00-05:00", "America/New_York", "ambiguous_local_time"),
    ],
)
def test_time_validation_and_dst(value, timezone, code):
    with pytest.raises(ValueError, match=code):
        reminder_time(value, timezone, NOW)


def test_unknown_timezone_and_valid_future():
    with pytest.raises(ValueError, match="unknown_timezone"):
        zone("Mars/Unknown")
    assert (
        reminder_time("2026-10-13T18:30:00+03:00", "Europe/Moscow", NOW).isoformat()
        == "2026-10-13T18:30:00+03:00"
    )


def test_metadata_signed_scoped_expiring():
    import uuid

    action = uuid.uuid4()
    meta = sign("secret", 42, action, now=100)
    assert verify("secret", meta, now=101) == (42, action)
    with pytest.raises(ActionError):
        verify("other", meta, now=101)
    with pytest.raises(ActionError):
        verify("secret", meta, now=160)
    meta["study_assistant"]["claims"]["owner"] = 99
    with pytest.raises(ActionError):
        verify("secret", meta, now=101)


CITY = {
    "name": "Казань",
    "country": "Россия",
    "admin1": "Татарстан",
    "latitude": 55.79,
    "longitude": 49.12,
}
CURRENT = {
    "current": {
        "temperature_2m": 12.5,
        "weather_code": 3,
        "wind_speed_10m": 2.1,
        "time": "2026-10-12T09:00",
    },
    "current_units": {"temperature_2m": "°C", "wind_speed_10m": "m/s"},
    "timezone": "Europe/Moscow",
}


async def test_weather_success_has_exact_values():
    weather = Weather(None)
    weather.get_json = AsyncMock(side_effect=[{"results": [CITY]}, CURRENT])
    result = await weather.current("Казань")
    assert result.status == "ok"
    assert (result.temperature_c, result.wind_speed, result.wind_unit, result.weather_code) == (
        12.5,
        2.1,
        "m/s",
        3,
    )
    assert result.place.country == "Россия"
    assert result.observed_at == "2026-10-12T09:00"


async def test_weather_not_found():
    weather = Weather(None)
    weather.get_json = AsyncMock(return_value={})
    assert (await weather.current("Несуществующий город")).status == "not_found"
    weather.get_json.assert_awaited_once()


async def test_weather_ambiguity_does_not_choose_first():
    weather = Weather(None)
    weather.get_json = AsyncMock(
        return_value={
            "results": [
                CITY,
                {**CITY, "latitude": 56.1, "country": "Другая страна", "admin1": "Другой регион"},
            ]
        }
    )
    result = await weather.current("Казань")
    assert result.status == "ambiguous"
    assert len(result.options) == 2
    assert result.temperature_c is None
    weather.get_json.assert_awaited_once()


@pytest.mark.parametrize("country", [None, ""])
async def test_weather_country_name_missing_keeps_ambiguous_candidate(country):
    # Public API regression: Санкт-Петербург also matches a place in MS,
    # whose geocoding row has country_code but no localized country name.
    alternative = {
        "name": "Санкт-Петербург",
        "latitude": 16.7727,
        "longitude": -62.21729,
        "country_code": "MS",
    }
    if country is not None:
        alternative["country"] = country
    weather = Weather(None)
    weather.get_json = AsyncMock(return_value={"results": [CITY, alternative]})
    result = await weather.current("Санкт-Петербург")
    assert result.status == "ambiguous"
    assert len(result.options) == 2
    assert result.options[1].country == "MS"
    assert result.options[1].query == "Санкт-Петербург, MS"
    assert result.temperature_c is None
    weather.get_json.assert_awaited_once()


async def test_weather_qualified_city_without_country_name_returns_forecast():
    row = {key: value for key, value in CITY.items() if key not in {"country", "admin1"}}
    row["country_code"] = "RU"
    weather = Weather(None)
    weather.get_json = AsyncMock(side_effect=[{"results": [row]}, CURRENT])
    result = await weather.current("Казань, RU")
    assert result.status == "ok"
    assert result.place.country == "RU"
    assert result.place.region == ""
    assert result.temperature_c == 12.5


@pytest.mark.parametrize(
    "raw",
    [
        {"results": {}},
        {"results": [{}]},
        {"results": [{key: value for key, value in CITY.items() if key != "country"}]},
    ],
)
async def test_weather_invalid_geocoding(raw):
    weather = Weather(None)
    weather.get_json = AsyncMock(return_value=raw)
    with pytest.raises(ToolFailure, match="weather_invalid_response"):
        await weather.current("Казань")


@pytest.mark.parametrize(
    "raw", [{}, {**CURRENT, "current": {**CURRENT["current"], "temperature_2m": float("nan")}}]
)
async def test_weather_invalid_current(raw):
    weather = Weather(None)
    weather.get_json = AsyncMock(side_effect=[{"results": [CITY]}, raw])
    with pytest.raises(ToolFailure, match="weather_invalid_response"):
        await weather.current("Казань")


async def test_weather_http_timeout_is_bounded():
    response = Mock(__aenter__=AsyncMock(side_effect=TimeoutError), __aexit__=AsyncMock())
    session = Mock(get=Mock(return_value=response))
    with pytest.raises(ToolFailure, match="weather_timeout"):
        await Weather(session).get_json("https://api.open-meteo.com/v1/forecast", {})
    assert session.get.call_count == 2


async def test_independent_mcp_client_discovers_resource_and_denies_write():
    # Независимый SDK-клиент, без MCPBridge и без доверенного owner/signature.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_service.server"],
        env={"PYTHONIOENCODING": "utf-8"},
    )
    async with stdio_client(params) as streams, ClientSession(*streams) as client:
        initialized = await client.initialize()
        assert initialized.protocolVersion
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "get_weather",
            "get_schedule",
            "get_tasks",
            "add_reminder",
            "search_materials",
        }
        assert all(
            tool.outputSchema and tool.inputSchema["additionalProperties"] is False
            for tool in tools
        )
        resource = (await client.list_resources()).resources
        assert {str(item.uri) for item in resource} == {
            "schedule://current-week",
            "materials://catalog",
        }
        catalog = await client.read_resource(AnyUrl("materials://catalog"))
        assert len(json.loads(catalog.contents[0].text)["documents"]) == 3
        found = await client.call_tool("search_materials", {"query": "идемпотентность", "limit": 1})
        assert found.structuredContent["hits"][0]["doc_id"] == "reliable-delivery"
        result = await client.call_tool("get_schedule", {"date": "2026-10-12"})
        assert result.isError and result.content[0].text == "trusted_context_required"
        task_denied = await client.call_tool("get_tasks", {"scope": "upcoming"})
        assert task_denied.isError and task_denied.content[0].text == "trusted_context_required"
        task_forgery = await client.call_tool("get_tasks", {"scope": "upcoming", "owner": 101})
        assert (
            task_forgery.isError and task_forgery.content[0].text == "invalid_arguments_or_result"
        )
        with pytest.raises(McpError):
            await client.read_resource(AnyUrl("schedule://current-week"))
        denied = await client.call_tool(
            "add_reminder",
            {"text": "Проверка", "remind_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        )
        assert denied.isError and denied.content[0].text == "trusted_context_required"
        extra = await client.call_tool(
            "get_schedule", {"date": "2026-10-12", "path": "/etc/passwd"}
        )
        assert extra.isError and extra.content[0].text == "invalid_arguments_or_result"
        unknown = await client.call_tool("shell", {"command": "whoami"})
        assert unknown.isError and unknown.content[0].text == "unknown_tool"


async def test_bridge_discovery_validation_and_disconnect():
    settings = Settings(bot_token="unused", postgres_password="unused")
    bridge = MCPBridge(settings, "test-context-secret")
    try:
        assert await bridge.start()
        assert len(bridge.schemas()) == 5
        with pytest.raises(MCPFailure, match="unknown_tool"):
            await bridge.call("exec", {})
        with pytest.raises(MCPFailure, match="invalid_arguments"):
            await bridge.call("get_schedule", {"date": "2026-10-12", "user_id": 99})
        with pytest.raises(MCPFailure, match="trusted_context_required"):
            await bridge.call("get_schedule", {"date": "2026-10-18"})
    finally:
        await bridge.close()
    with pytest.raises(MCPFailure, match="mcp_unavailable"):
        await bridge.call("get_schedule", {"date": "2026-10-12"})


async def test_bad_tool_result_leaves_other_tools_available(monkeypatch):
    from mcp import ClientSession, types

    original = ClientSession.call_tool

    async def one_bad_result(self, name, *args, **kwargs):
        if name == "get_weather":
            return types.CallToolResult(content=[], structuredContent={"invalid": True})
        return await original(self, name, *args, **kwargs)

    monkeypatch.setattr(ClientSession, "call_tool", one_bad_result)
    settings = Settings(bot_token="unused", postgres_password="unused")
    bridge = MCPBridge(settings, "test-context-secret")
    try:
        assert await bridge.start()
        with pytest.raises(MCPFailure, match="mcp_invalid_result"):
            await bridge.call("get_weather", {"city": "Тест"})
        assert (await bridge.call("search_materials", {"query": "MCP"}))["status"] == "ok"
        assert bridge.available
    finally:
        await bridge.close()
