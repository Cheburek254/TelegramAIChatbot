import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.assistant import Assistant
from app.config import Settings
from app.mcp_service.contracts import INPUTS
from app.orchestration.contracts import RouteDecision
from app.orchestration.supervisor import Supervisor, current_weather_city, explicit_route
from app.reminder_requests import relative_reminder
from app.timezones import reminder_time, zone
from app.tool_examples import argument_examples, route_examples
from tests.fakes import MemoryRepository
from tests.test_agent import FakeStore
from tests.test_supervisor import MemoryAssistantStore, bridge

NOW = datetime(2026, 10, 24, 23, 59, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "body,seconds",
    [
        ("через пару минут проверить чай", 120),
        ("через парочку минут проверить чай", 120),
        ("через полчаса проверить чай", 1800),
        ("через четверть часа проверить чай", 900),
        ("через полтора часа проверить чай", 5400),
        ("через две минуты проверить чай", 120),
        ("через двадцать пять минут проверить чай", 1500),
        ("через 1 час 30 минут проверить чай", 5400),
        ("через час и 15 минут проверить чай", 4500),
        ("через 0,5 минуты проверить чай", 30),
        ("проверить чай через 10 секунд", 10),
        ("через сутки проверить чай", 86400),
    ],
)
def test_relative_duration_and_body(body, seconds):
    parsed = relative_reminder(body)
    assert parsed.text == "проверить чай"
    assert parsed.delay == timedelta(seconds=seconds)
    moment = parsed.moment(NOW, zone("Europe/Berlin"))
    assert moment.astimezone(UTC) == NOW + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "body",
    [
        "через несколько минут проверить чай",
        "через -2 минуты проверить чай",
        "через 0 минут проверить чай",
        "через пару минут",
        "через один два часа проверить чай",
    ],
)
def test_unclear_delay_requires_clarification(body):
    with pytest.raises(ValueError, match="unclear_reminder_delay"):
        relative_reminder(body)


def test_confirmed_elapsed_time_keeps_offset_during_dst_overlap():
    now = datetime(2026, 10, 25, 0, 45, tzinfo=UTC)
    moment = relative_reminder("через полчаса проверить чай").moment(now, zone("Europe/Berlin"))
    with pytest.raises(ValueError, match="ambiguous_local_time"):
        reminder_time(moment.isoformat(), "Europe/Berlin", now)
    assert reminder_time(moment.isoformat(), "Europe/Berlin", now, confirmed=True).astimezone(
        UTC
    ) == now + timedelta(minutes=30)


def test_few_shots_cover_every_tool_and_have_valid_arguments():
    for name, model in INPUTS.items():
        for _, arguments in argument_examples(name, NOW, "Europe/Moscow"):
            model.model_validate(arguments)
    routes = route_examples()
    decisions = [
        RouteDecision.model_validate_json(message["content"])
        for message in routes
        if message["role"] == "assistant"
    ]
    assert {item.target for item in decisions} == {
        "schedule",
        "tasks",
        "knowledge",
        "lifestyle",
        "legacy",
        "clarify",
    }
    assert current_weather_city("Что с погодой в Питере прямо сейчас?") == "Санкт-Петербург, Россия"
    assert explicit_route("Перечисли мои учебные материалы").target == "knowledge"


@pytest.mark.parametrize("mode", ["study", "review", "translate"])
async def test_relative_reminder_prepares_exact_time_without_llm_or_mcp_write(mode):
    from app.repository import Profile

    repository = MemoryRepository()
    repository.profiles[101] = Profile(mode=mode)
    llm = Mock(complete=AsyncMock())
    raw, traces = bridge(), MemoryAssistantStore()
    settings = Settings(bot_token="unused", postgres_password="unused")
    service = Supervisor(
        settings, traces, Assistant(settings, repository, llm), raw, clock=lambda: NOW
    )
    pending_store = FakeStore()
    pending_store.timezone = AsyncMock(return_value="Europe/Moscow")
    service.base.store = pending_store
    result = await asyncio.wait_for(
        service.respond(101, "Напомни мне через пару минут проверить чай", "relative"), 5
    )
    assert result.pending_id and "03:01:30" in result.content
    prepared = pending_store.prepared[0]
    assert prepared[3]["text"] == "проверить чай"
    assert prepared[5].astimezone(UTC) == NOW + timedelta(minutes=2)
    llm.complete.assert_not_awaited()
    raw.call.assert_not_awaited()
    assert (
        await service.respond(101, "Напомни мне через пару минут проверить чай", "relative")
    ).pending_id == result.pending_id
    assert len(pending_store.prepared) == 1
    assert repository.profiles[101].mode == mode


async def test_free_form_tools_in_review_use_router_examples():
    from app.llm import Completion
    from app.repository import Profile

    repository = MemoryRepository()
    repository.profiles[101] = Profile(mode="review")
    route = dict(target="tasks", intent="read", scope="overdue", confidence=1.0, reason="overdue")
    llm = Mock(complete=AsyncMock(return_value=Completion(json.dumps(route), "fixture", "fixture")))
    raw = bridge()
    raw.call.return_value = {"scope": "overdue", "timezone": "UTC", "tasks": [], "more": False}
    settings = Settings(bot_token="unused", postgres_password="unused")
    service = Supervisor(
        settings, MemoryAssistantStore(), Assistant(settings, repository, llm), raw
    )
    service.base.store.timezone = AsyncMock(return_value="UTC")
    await service.respond(101, "Есть у меня что-нибудь просроченное?", "overdue")
    raw.call.assert_awaited_once_with("get_tasks", {"scope": "overdue"}, owner=101)
    messages = llm.complete.call_args.args[0]
    assert messages[-1]["content"] == "Есть у меня что-нибудь просроченное?"
    assert any(
        message["content"] == "Напомни мне через пару минут проверить чай" for message in messages
    )
