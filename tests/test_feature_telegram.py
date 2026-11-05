import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.handlers.features import ContactMiddleware, create_feature_router


@pytest.mark.parametrize(
    "command,method",
    [
        ("/city X, Y", "city"),
        ("/briefing_on", "consent"),
        ("/briefing_off", "consent"),
        ("/notifications", "settings"),
        ("/trace last", "trace"),
    ],
)
async def test_assistant_commands_derive_owner_from_private_update_without_model(command, method):
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    service = Mock(
        city=AsyncMock(return_value=("Confirm", uuid.uuid4())),
        consent=AsyncMock(return_value="OK"),
        settings=AsyncMock(return_value="Settings"),
    )
    supervisor = Mock(trace=AsyncMock(return_value="Trace"))
    store = Mock(contact=AsyncMock())
    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(ContactMiddleware(store))
    dispatcher.include_router(create_feature_router(service, supervisor))
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=101, type="private"),
        from_user=User(id=101, is_bot=False, first_name="Test"),
        text=command,
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    store.contact.assert_awaited_once_with(101, 101)
    target = supervisor if method == "trace" else service
    assert getattr(target, method).call_args.args[0] == 101
    supervisor.respond.assert_not_called()


async def test_foreign_city_callback_and_group_contact_are_rejected():
    service = Mock(confirm=AsyncMock())
    supervisor = Mock()
    store = Mock(contact=AsyncMock())
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(ContactMiddleware(store))
    dispatcher.include_router(create_feature_router(service, supervisor))
    user = User(id=202, is_bot=False, first_name="Test")
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=101, type="private"), text="Card"
    )
    query = CallbackQuery(
        id="x",
        chat_instance="x",
        from_user=user,
        message=message,
        data=f"city:confirm:{uuid.uuid4()}",
    )
    await dispatcher.feed_update(bot, Update(update_id=1, callback_query=query))
    service.confirm.assert_not_called()
    group = message.model_copy(
        update={"chat": Chat(id=-321, type="group"), "from_user": user, "text": "/briefing_on"}
    )
    await dispatcher.feed_update(bot, Update(update_id=2, message=group))
    store.contact.assert_not_called()


async def test_daily_reminder_menu_and_confirmation_use_private_owner():
    series, proposal = uuid.uuid4(), uuid.uuid4()
    notifications = Mock(
        list_daily=AsyncMock(
            return_value=[
                {
                    "id": series,
                    "version": 1,
                    "minute": 1140,
                    "timezone": "Europe/Moscow",
                    "text": "Read",
                }
            ]
        ),
        add_daily=AsyncMock(return_value=("Add?", proposal)),
        stop_daily=AsyncMock(return_value=("Stop?", proposal)),
        confirm_daily=AsyncMock(return_value="Stopped"),
    )
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    supervisor = Mock()
    dispatcher = Dispatcher()
    dispatcher.include_router(create_feature_router(notifications, supervisor))
    user = User(id=101, is_bot=False, first_name="Test")
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=101, type="private"),
        from_user=user,
        text="/daily_reminders",
    )

    async def callback(action, actor=user):
        query = CallbackQuery(
            id=action,
            chat_instance="x",
            from_user=actor,
            message=message,
            data=action,
        )
        await dispatcher.feed_update(bot, Update(update_id=3, callback_query=query))

    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    menu = bot.session.call_args.args[1]
    assert "19:00" in menu.text and "Read" in menu.text
    assert menu.reply_markup.inline_keyboard[0][0].callback_data == f"daily:stop:{series}"

    add = message.model_copy(update={"text": "/daily_reminders add 19:00 Read"})
    await dispatcher.feed_update(bot, Update(update_id=2, message=add))
    notifications.add_daily.assert_awaited_once_with(101, "telegram:101:1", "add 19:00 Read")
    assert bot.session.call_args.args[1].reply_markup.inline_keyboard[0][0].callback_data == (
        f"daily:confirm:{proposal}"
    )
    await callback(f"daily:stop:{series}")
    notifications.stop_daily.assert_awaited_once_with(101, f"callback:daily:stop:{series}", series)
    await callback(f"daily:confirm:{proposal}", user.model_copy(update={"id": 202}))
    notifications.confirm_daily.assert_not_called()
    await callback(f"daily:confirm:{proposal}")
    notifications.confirm_daily.assert_awaited_once_with(101, proposal, cancel=False)
    supervisor.respond.assert_not_called()
