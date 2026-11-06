import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.handlers.assistant import create_router


@pytest.mark.parametrize("operation", ["status", "confirm", "cancel"])
async def test_login_buttons_use_telegram_owner_and_never_model(operation):
    identity = uuid.uuid4()
    auth = Mock(
        status=AsyncMock(return_value={"state": "verified", "account": "111111"}),
        confirm=AsyncMock(return_value="111111"),
    )
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"), text="Подключение"
    )
    query = CallbackQuery(
        id="c",
        chat_instance="c",
        message=message,
        from_user=User(id=42, is_bot=False, first_name="Тест"),
        data=f"itmo:{operation}:{identity}",
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router())
    agent = Mock()
    await dispatcher.feed_update(
        bot, Update(update_id=1, callback_query=query), itmo_auth=auth, agent=agent
    )
    if operation == "status":
        auth.status.assert_awaited_once_with(42, str(identity))
    else:
        auth.confirm.assert_awaited_once_with(42, str(identity), cancel=operation == "cancel")
    agent.respond.assert_not_called()


async def test_login_link_is_in_button_only_and_foreign_chat_cannot_use_callback():
    identity = uuid.uuid4()
    auth = Mock(
        issue=AsyncMock(return_value=(identity, "https://study.example/itmo/login#private-ticket"))
    )
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router())
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Тест"),
        text="/itmo connect",
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=message), itmo_auth=auth)
    auth.issue.assert_awaited_once_with(42)
    sent = [c.args[1] for c in bot.session.call_args_list if len(c.args) > 1]
    card = next(item for item in sent if isinstance(item, SendMessage))
    assert "private-ticket" not in card.text
    assert card.reply_markup.inline_keyboard[0][0].url.endswith("#private-ticket")
    auth.confirm = AsyncMock()
    query = CallbackQuery(
        id="c",
        chat_instance="c",
        message=message,
        from_user=User(id=73, is_bot=False, first_name="Другой"),
        data=f"itmo:confirm:{identity}",
    )
    await dispatcher.feed_update(bot, Update(update_id=2, callback_query=query), itmo_auth=auth)
    auth.confirm.assert_not_called()
