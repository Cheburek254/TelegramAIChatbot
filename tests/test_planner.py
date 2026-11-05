import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.handlers.planner import create_planner_router
from app.planner import Planner, local_due, task_intent

NOW = datetime(2026, 11, 6, 10, tzinfo=UTC)
ZONE = "Europe/Moscow"


@pytest.mark.parametrize(
    "text,title,due",
    [
        ("Сдать отчёт в пятницу до 18:00", "Сдать отчёт", "2026-11-06T15:00:00+00:00"),
        ("Задача: отчёт завтра в 9:30", "отчёт", "2026-11-07T06:30:00+00:00"),
        ("Задача: отчёт до 18:00", "отчёт", "2026-11-06T15:00:00+00:00"),
        ("Добавь задачу отчёт до 09.11", "отчёт", "2026-11-09T20:59:00+00:00"),
        ("Задача: проверить конспект", "проверить конспект", None),
    ],
)
def test_task_language(text, title, due):
    extracted, match = task_intent(text)
    assert extracted == title
    assert (local_due(match, NOW, ZONE).isoformat() if match else None) == due


def test_task_does_not_capture_ordinary_questions_or_reminders():
    for text in ("Что сдать до пятницы?", "Напомни завтра в 18:00 сдать отчёт", "Сделай обзор"):
        assert task_intent(text) is None
    with pytest.raises(ValueError):
        local_due(task_intent("Задача: отчёт сегодня в 09:00")[1], NOW, ZONE)


async def test_telegram_task_and_daily_routes_and_buttons():
    identity, pending = uuid.uuid4(), uuid.uuid4()
    item = {"id": identity, "version": 1, "text": "Сдать отчёт", "due_at": NOW, "timezone": ZONE}
    store = Mock(
        proposal=AsyncMock(return_value=pending),
        item=AsyncMock(return_value=item),
        confirm=AsyncMock(return_value=("added", "t", identity)),
    )
    notifications = Mock(
        store=Mock(profile=AsyncMock(return_value={"timezone": ZONE})),
        add_daily=AsyncMock(return_value=("Daily", pending)),
    )
    service = Planner(store, notifications, ZONE, clock=lambda: NOW)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_planner_router(service))
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    user = User(id=101, first_name="Test", is_bot=False)
    message = Message(message_id=1, date=NOW, chat=Chat(id=101, type="private"), from_user=user)

    async def send(text):
        await dispatcher.feed_update(
            bot, Update(update_id=1, message=message.model_copy(update={"text": text}))
        )
        return bot.session.call_args.args[1]

    async def press(data, actor=user):
        query = CallbackQuery(
            id="btn", from_user=actor, message=message, chat_instance="x", data=data
        )
        await dispatcher.feed_update(bot, Update(update_id=2, callback_query=query))
        return bot.session.call_args.args[1]

    proposal = await send("Сдать отчёт в пятницу до 18:00")
    assert "18:00" in proposal.text
    confirm = proposal.reply_markup.inline_keyboard[0][0].callback_data
    assert confirm == f"plan:confirm:{pending}"
    await press(confirm, user.model_copy(update={"id": 202}))
    store.confirm.assert_not_called()
    card = await press(confirm)
    ready, move = card.reply_markup.inline_keyboard[0]
    assert ready.text == "✅ Готово"
    menu = await press(move.callback_data)
    later = menu.reply_markup.inline_keyboard[1][0].callback_data
    change = await press(later)
    assert "Перенести" in change.text
    assert store.proposal.call_args.args[-2:] == ("t", "move")
    await press(ready.callback_data)
    assert store.proposal.call_args.args[-2:] == ("t", "done")
    daily = await send("Напомни каждый день в 19:00 повторить английский")
    assert daily.reply_markup.inline_keyboard[0][0].callback_data == f"daily:confirm:{pending}"
    notifications.add_daily.assert_awaited_once_with(
        101, "telegram:101:1", "add 19:00 повторить английский"
    )
