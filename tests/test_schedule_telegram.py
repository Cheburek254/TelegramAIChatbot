import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.handlers.assistant import create_router
from app.schedule_service import ScheduleReply


async def feed(*, command=None, callback=None, chat=42):
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    service = Mock(
        command=AsyncMock(return_value=ScheduleReply("Список")),
        confirm=AsyncMock(return_value=ScheduleReply("Сохранено")),
    )
    message = Message(
        message_id=17,
        date=datetime.now(UTC),
        chat=Chat(id=chat, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Тест"),
        text=command or "Карточка",
    )
    if callback:
        update = Update(
            update_id=1,
            callback_query=CallbackQuery(
                id="c",
                chat_instance="c",
                message=message,
                from_user=message.from_user,
                data=callback,
            ),
        )
    else:
        update = Update(update_id=1, message=message)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router())
    await dispatcher.feed_update(
        bot, update, schedule_service=service, assistant=Mock(), agent=Mock()
    )
    return service, bot


async def test_schedule_command_uses_telegram_owner_without_llm():
    service, _ = await feed(command="/schedule add 2026-11-02 10:00-11:30 | X | A")
    service.command.assert_awaited_once_with(42, "add 2026-11-02 10:00-11:30 | X | A", "message:17")
    service.confirm.assert_not_called()


@pytest.mark.parametrize("operation", ["confirm", "cancel"])
async def test_schedule_callback_is_owner_scoped(operation):
    identity = str(uuid.uuid4())
    service, _ = await feed(callback=f"schedule:{operation}:{identity}")
    service.confirm.assert_awaited_once_with(42, identity, cancel=operation == "cancel")


async def test_foreign_chat_callback_cannot_change_any_schedule():
    service, _ = await feed(callback=f"schedule:confirm:{uuid.uuid4()}", chat=73)
    service.confirm.assert_not_called()


async def test_schedule_menu_reads_only_clicking_owner():
    service, bot = await feed(callback="schedule:menu")
    service.command.assert_not_called()
    sent = [call.args[1] for call in bot.session.call_args_list if len(call.args) > 1]
    menu = next(item for item in sent if isinstance(item, SendMessage))
    assert [row[0].callback_data for row in menu.reply_markup.inline_keyboard] == [
        "schedule:put",
        "schedule:get",
        "menu",
    ]
