import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import EditMessageReplyMarkup, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.agent import AgentReply
from app.handlers.assistant import create_router


@pytest.mark.parametrize(
    "command,method,args",
    [
        ("/tools", "tools", ()),
        ("/timezone Europe/Moscow", "timezone", (42, "Europe/Moscow")),
        ("/why", "why", (42,)),
    ],
)
async def test_agent_commands_do_not_call_model(command, method, args):
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    agent = Mock(
        tools=AsyncMock(return_value="Инструменты"),
        timezone=AsyncMock(return_value="Зона"),
        why=AsyncMock(return_value="Решение"),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router())
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=User(id=42, is_bot=False, first_name="Тест"),
        text=command,
    )
    await dispatcher.feed_update(
        bot, Update(update_id=1, message=message), agent=agent, assistant=Mock()
    )
    getattr(agent, method).assert_awaited_once_with(*args)
    agent.respond.assert_not_called()


@pytest.mark.parametrize(
    "data,method,args",
    [
        ("agent:tools", "tools", ()),
        ("agent:timezone", "timezone", (42,)),
        ("agent:why", "why", (42,)),
    ],
)
async def test_agent_menu_buttons_use_owner(data, method, args):
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    agent = Mock(
        tools=AsyncMock(return_value="Инструменты"),
        timezone=AsyncMock(return_value="Зона"),
        why=AsyncMock(return_value="Решение"),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router())
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"), text="Меню"
    )
    callback = CallbackQuery(
        id="c",
        chat_instance="c",
        message=message,
        from_user=User(id=42, is_bot=False, first_name="Тест"),
        data=data,
    )
    await dispatcher.feed_update(
        bot, Update(update_id=1, callback_query=callback), agent=agent, assistant=Mock()
    )
    getattr(agent, method).assert_awaited_once_with(*args)
    agent.respond.assert_not_called()


@pytest.mark.parametrize("retry", [True, False])
async def test_confirmation_keeps_buttons_only_for_retry(retry):
    identity = uuid.uuid4()
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    agent = Mock(
        confirm=AsyncMock(return_value=AgentReply("Результат", identity if retry else None))
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router())
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"), text="Подтверждение"
    )
    callback = CallbackQuery(
        id="c",
        chat_instance="c",
        message=message,
        from_user=User(id=42, is_bot=False, first_name="Тест"),
        data="reminder:confirm:" + str(identity),
    )
    await dispatcher.feed_update(
        bot, Update(update_id=1, callback_query=callback), agent=agent, assistant=Mock()
    )
    agent.confirm.assert_awaited_once_with(42, str(identity), cancel=False)
    sent = [c.args[1] for c in bot.session.call_args_list if len(c.args) > 1]
    assert any(isinstance(item, SendMessage) for item in sent)
    assert any(isinstance(item, EditMessageReplyMarkup) for item in sent) is not retry
