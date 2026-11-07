import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.assistant import Assistant
from app.config import Settings
from app.mcp_service.contracts import INPUTS
from app.orchestration.contracts import ReminderDraft, RouteDecision
from app.orchestration.supervisor import Supervisor, current_weather_city, explicit_route
from app.reminder_requests import relative_reminder
from app.timezones import reminder_time, zone
from app.tool_examples import argument_examples, reminder_examples, route_examples
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
        ("через минутка проверить чай", 60),
        ("через минутку проверить чай", 60),
        ("через минуточку проверить чай", 60),
        ("через одну минутку проверить чай", 60),
        ("через пару минуточек проверить чай", 120),
        ("через секундочку проверить чай", 1),
        ("через 10 секундочек проверить чай", 10),
        ("через часик проверить чай", 3600),
        ("через часок проверить чай", 3600),
        ("через полтора часика проверить чай", 5400),
        ("через денёк проверить чай", 86400),
        ("через недельку проверить чай", 604800),
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
    for message in reminder_examples(NOW, "Europe/Moscow"):
        if message["role"] == "assistant":
            ReminderDraft.model_validate_json(message["content"])


@pytest.mark.parametrize("mode", ["study", "review", "translate"])
@pytest.mark.parametrize(
    "phrase,minutes",
    [
        ("Напомни мне через пару минут проверить чай", 2),
        ("Напомни мне через минуточку проверить чай", 1),
        ("Поставь мне напоминалочку через минутку проверить чай", 1),
    ],
)
async def test_relative_reminder_prepares_exact_time_without_llm_or_mcp_write(
    mode, phrase, minutes
):
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
    result = await asyncio.wait_for(service.respond(101, phrase, "relative"), 5)
    expected = NOW + timedelta(minutes=minutes)
    assert (
        result.pending_id
        and expected.astimezone(zone("Europe/Moscow")).strftime("%H:%M:%S") in result.content
    )
    prepared = pending_store.prepared[0]
    assert prepared[3]["text"] == "проверить чай"
    assert prepared[5].astimezone(UTC) == expected
    llm.complete.assert_not_awaited()
    raw.call.assert_not_awaited()
    assert (await service.respond(101, phrase, "relative")).pending_id == result.pending_id
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


@pytest.mark.parametrize("mode", ["study", "review", "translate"])
@pytest.mark.parametrize(
    "phrase,target,scope",
    [
        ("Как там погодка в Питере?", "lifestyle", "default"),
        ("Что у меня по парочкам завтра?", "schedule", "day"),
        ("Глянь мои тасочки", "tasks", "all"),
        ("Поищи в конспектиках про транзакции", "knowledge", "default"),
        ("Какие конспектики у меня загружены?", "knowledge", "all"),
    ],
)
async def test_unknown_word_forms_reach_model_router_in_every_mode(mode, phrase, target, scope):
    from app.llm import Completion
    from app.orchestration.contracts import AgentResult, EmptyFacts
    from app.repository import Profile

    route = dict(target=target, intent="read", scope=scope, confidence=1.0, reason="colloquial")
    llm = Mock(complete=AsyncMock(return_value=Completion(json.dumps(route), "fixture", "fixture")))
    repository = MemoryRepository()
    repository.profiles[101] = Profile(mode=mode)
    settings = Settings(bot_token="unused", postgres_password="unused")
    service = Supervisor(
        settings, MemoryAssistantStore(), Assistant(settings, repository, llm), bridge()
    )
    service.base.store.timezone = AsyncMock(return_value="Europe/Moscow")
    service.specialist = AsyncMock(
        return_value=(
            AgentResult(
                status="success",
                message="Проверенный ответ",
                confidence=1.0,
                facts=EmptyFacts(),
                used_tools=[],
                error_code=None,
            ),
            None,
        )
    )
    reply = await service.respond(101, phrase, "colloquial")
    assert reply.content == "Проверенный ответ"
    decision = service.specialist.await_args.args[1]
    assert (decision.target, decision.scope) == (target, scope)
    assert repository.profiles[101].mode == mode


@pytest.mark.parametrize("mode", ["study", "review", "translate"])
@pytest.mark.parametrize(
    "phrase,needs_route",
    [
        ("Ассистент, напоммни плиз через минутку про чай", True),
        ("Напомни плиз через минутку про чай", False),
    ],
)
async def test_semantic_reminder_handles_address_and_typos(mode, phrase, needs_route):
    from app.llm import Completion
    from app.repository import Profile

    route = dict(
        target="schedule", intent="reminder", scope="default", confidence=1.0, reason="request"
    )
    draft = dict(action="prepare", text="про чай", delay_seconds=60, remind_at=None)
    responses = [Completion(json.dumps(draft), "fixture", "fixture")]
    if needs_route:
        responses.insert(0, Completion(json.dumps(route), "fixture", "fixture"))
    llm = Mock(complete=AsyncMock(side_effect=responses))
    repository = MemoryRepository()
    repository.profiles[101] = Profile(mode=mode)
    settings = Settings(bot_token="unused", postgres_password="unused")
    raw, traces = bridge(), MemoryAssistantStore()
    service = Supervisor(
        settings, traces, Assistant(settings, repository, llm), raw, clock=lambda: NOW
    )
    store = FakeStore()
    store.timezone = AsyncMock(return_value="Europe/Moscow")
    service.base.store = store
    result = await service.respond(101, phrase, "typos")
    assert result.pending_id
    assert store.prepared[0][3]["text"] == "про чай"
    assert store.prepared[0][5].astimezone(UTC) == NOW + timedelta(minutes=1)
    traces.authorize_action.assert_awaited_once()
    raw.call.assert_not_awaited()
    assert repository.profiles[101].mode == mode


@pytest.mark.parametrize("action", ["ignore", "clarify"])
async def test_semantic_reminder_without_clear_request_never_prepares(action):
    from app.llm import Completion

    route = dict(
        target="schedule", intent="reminder", scope="default", confidence=1.0, reason="uncertain"
    )
    draft = dict(action=action, text=None, delay_seconds=None, remind_at=None)
    llm = Mock(
        complete=AsyncMock(
            side_effect=[
                Completion(json.dumps(route), "fixture", "fixture"),
                Completion(json.dumps(draft), "fixture", "fixture"),
            ]
        )
    )
    settings = Settings(bot_token="unused", postgres_password="unused")
    raw, traces = bridge(), MemoryAssistantStore()
    service = Supervisor(settings, traces, Assistant(settings, MemoryRepository(), llm), raw)
    store = FakeStore()
    store.timezone = AsyncMock(return_value="Europe/Moscow")
    service.base.store = store
    result = await service.respond(101, "Это пример текста с напоминанием", "unclear")
    assert result.pending_id is None and not store.prepared
    traces.authorize_action.assert_not_awaited()
    raw.call.assert_not_awaited()
