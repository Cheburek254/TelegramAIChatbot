"""Детерминированные примеры планирования и сравнения расписаний."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Chat, Message, Update, User

from app.handlers.planner import create_planner_router
from app.itmo_watch import changes
from app.planner import Planner, repeat_task_intent
from app.study_planning import FREE_HOURS, StudyPlanning, free_deadline, free_query, free_windows
from app.timezones import next_task_due


def lesson(day, start="11:00", end="12:00", title="Алгебра", room="301"):
    return {"date": day, "start": start, "end": end, "title": title, "location": room}


def test_free_slots_merge_busy_intervals_convert_zone_and_respect_cutoff():
    args = free_query("полтора часа до пятницы в 18:00, чтобы сдать отчёт")
    assert args == (90, "пятницы 18:00", "сдать отчёт")
    assert free_query("10 минут") is None
    busy = [
        lesson("2026-11-06", "13:30", "15:30"),
        lesson("2026-11-06", "14:30", "16:15"),
    ]
    first = datetime(2026, 11, 6, 8, 37, tzinfo=UTC)
    last = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
    windows = list(free_windows(busy, "Europe/Moscow", "Europe/Berlin", first, last, 90))
    assert [(start.hour, start.minute, end.hour, end.minute) for start, end in windows] == [
        (8, 45, 10, 15),
        (13, 15, 14, 45),
    ]


def test_next_week_is_calendar_week_in_users_timezone():
    now = datetime(2026, 11, 8, 22, tzinfo=UTC)  # Monday 01:00 in Moscow
    _, cutoff, _ = free_query("90 до следующей пятницы в 18:00")
    assert free_deadline(cutoff, now, "Europe/Moscow") == datetime(2026, 11, 20, 15, tzinfo=UTC)
    assert free_deadline(cutoff, now, "UTC") == datetime(2026, 11, 13, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    "text,last",
    [
        ("/free 90 до пятницы следующей недели", 13),
        ("Найди мне 90 минут до следующей пятницы?", 13),
        (f"/free 90 до пятницы. {FREE_HOURS}", 6),
        (FREE_HOURS, None),
    ],
)
async def test_free_telegram_routes_to_itmo_or_asks_duration(text, last):
    now = datetime(2026, 11, 6, 10, tzinfo=UTC)
    planner = Mock(
        clock=lambda: now,
        zone_for=AsyncMock(return_value="Europe/Moscow"),
        store=Mock(proposal=AsyncMock(return_value="synthetic-id")),
    )
    itmo = Mock(connected=lambda owner: True, fetch=AsyncMock(return_value=[]))
    llm = Mock()
    study = StudyPlanning(planner, None, itmo, llm, 6000)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_planner_router(planner, study=study))
    fallback = Router()

    @fallback.message()
    async def general(message):
        pytest.fail(f"Free-window request reached the general assistant: {message.text}")

    dispatcher.include_router(fallback)
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    message = Message(
        message_id=1,
        date=now,
        chat=Chat(id=123, type="private"),
        from_user=User(id=123, is_bot=False, first_name="Test"),
        text=text,
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    llm.complete.assert_not_called()
    answer = bot.session.call_args.args[1].text
    if last is None:
        itmo.fetch.assert_not_called()
        assert "Сколько минут" in answer
    else:
        itmo.fetch.assert_awaited_once_with(123, now.date(), now.date().replace(day=last))
        assert f"До {last:02d}.11.2026 23:59" in answer


async def test_recurring_task_parsing_and_calendar_recurrence():
    now = datetime(2026, 11, 6, 16, tzinfo=UTC)
    proposal = AsyncMock(return_value="synthetic-id")
    notifications = Mock(store=Mock(profile=AsyncMock(return_value=None)))
    planner = Planner(Mock(proposal=proposal), notifications, "Europe/Moscow", clock=lambda: now)
    intent, period = repeat_task_intent("Каждый день в 18:00 сдавать отчёт")
    assert period == 1
    await planner.add_task(123, "request", intent, repeat_days=period)
    assert proposal.call_args.kwargs["due_at"] == datetime(2026, 11, 7, 15, tzinfo=UTC)
    intent, period = repeat_task_intent("Каждую пятницу сдавать отчёт в 18:00")
    assert period == 7
    await planner.add_task(123, "request-week", intent, repeat_days=period)
    assert proposal.call_args.kwargs["due_at"] == datetime(2026, 11, 13, 15, tzinfo=UTC)
    due = datetime(2026, 3, 28, 1, 30, tzinfo=UTC)  # 02:30 in Berlin
    assert next_task_due(due, "Europe/Berlin", 1, due) == datetime(2026, 3, 30, 0, 30, tzinfo=UTC)


async def test_forwarding_proposes_separate_verified_candidates():
    text = "До 8 ноября сдать отчёт. Повторить конспект."
    results = [
        {"text": "Сдать отчёт", "deadline": "2026-11-08", "evidence": "До 8 ноября сдать отчёт"},
        {"text": "Повторить конспект", "deadline": None, "evidence": "Повторить конспект"},
        {"text": "Сдать отчёт", "deadline": "2026-11-08", "evidence": "До 8 ноября сдать отчёт"},
        {"text": "Подделка", "deadline": None, "evidence": "Нет в тексте"},
        {"text": "Недопустимый срок", "deadline": "20261108", "evidence": "Повторить конспект"},
    ]
    planner = Mock(
        clock=lambda: datetime(2026, 11, 6, 10, tzinfo=UTC),
        zone_for=AsyncMock(return_value="Europe/Moscow"),
        propose_task=AsyncMock(side_effect=[("Отчёт", "first"), ("Конспект", "second")]),
    )
    llm = Mock(complete=AsyncMock(return_value=Mock(content=json.dumps({"tasks": results}))))
    study = StudyPlanning(planner, None, None, llm, 6000)
    cards = await study.extract(123, "forward:7", text)
    assert [identity for _, identity in cards] == ["first", "second"]
    assert planner.propose_task.await_args_list[0].args == (
        123,
        "forward:7:0",
        "Сдать отчёт",
        "Europe/Moscow",
        datetime(2026, 11, 8, 20, 59, tzinfo=UTC),
    )
    assert planner.propose_task.await_args_list[1].args[-1] is None
    assert llm.complete.call_args.kwargs["tools"] == []


def test_itmo_compares_shared_future_only_and_classifies_changes():
    old = {
        "first": "2026-11-05",
        "last": "2026-11-11",
        "entries": [
            lesson("2026-11-05", title="Ушло из окна"),
            lesson("2026-11-06", "08:00", "09:00", "Прошедшее"),
            lesson("2026-11-07"),
            lesson("2026-11-08", title="Физика"),
            lesson("2026-11-09", title="Отмена"),
        ],
    }
    current = {
        "first": "2026-11-06",
        "last": "2026-11-12",
        "entries": [
            lesson("2026-11-07", room="305"),
            lesson("2026-11-10", title="Физика"),
            lesson("2026-11-12", title="Вошло в окно"),
        ],
    }
    now = datetime(2026, 11, 6, 10, tzinfo=UTC)
    assert changes(None, current, now) is None
    assert changes(current, current, now) is None
    message = changes(old, current, now)
    assert all(word in message for word in ("Смена аудитории", "Перенос", "Убрано", "Отмена"))
    assert all(word not in message for word in ("Прошедшее", "Вошло", "Ушло"))
