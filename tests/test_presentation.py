from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.handlers.assistant import create_router
from app.presentation import formatted_parts, main_keyboard, settings_keyboard


def test_model_markup_uses_entities_and_leaves_html_as_literal_text():
    # Arrange / Act
    parts = formatted_parts('**Важно** <b>это не HTML</b>\n```python\nprint("😀")\n```\n`x`')
    # Assert
    assert len(parts) == 1
    assert parts[0].text == 'Важно <b>это не HTML</b>\nprint("😀")\n\nx'
    assert [e.type for e in parts[0].entities] == ["bold", "pre", "code"]
    assert parts[0].entities[1].language == "python"
    encoded = parts[0].text.encode("utf-16-le")
    assert encoded[parts[0].entities[2].offset * 2 :].decode("utf-16-le") == "x"


def test_code_entity_is_reopened_on_each_lossless_unicode_chunk():
    # Arrange
    code = 'print("😀")\n' * 1000
    # Act
    parts = formatted_parts("```python\n" + code + "```")
    # Assert
    assert "".join(p.text for p in parts) == code
    for part in parts:
        units = len(part.text.encode("utf-16-le")) // 2
        assert 0 < units <= 4096
        assert len(part.entities) == 1
        assert part.entities[0].offset == 0 and part.entities[0].length == units


def test_formatting_never_creates_empty_telegram_message():
    # Arrange / Act / Assert
    assert formatted_parts("` `")[0].text == "` `"
    assert len(main_keyboard().inline_keyboard[0]) == 3
    assert [b.text for b in settings_keyboard().inline_keyboard[0]] == ["0.0", "0.3", "0.7", "1.0"]


@pytest.mark.parametrize(
    "data,method,arguments",
    [
        ("mode:review", "mode", (42, "review")),
        ("temperature:0.7", "preferences", (42, "0.7")),
        ("reset", "reset", (42,)),
    ],
)
async def test_buttons_apply_to_trusted_user_without_model_call(data, method, arguments):
    # Arrange
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    service = AsyncMock()
    getattr(service, method).return_value = "Готово"
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router())
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"), text="Меню"
    )
    callback = CallbackQuery(
        id="test",
        chat_instance="test",
        from_user=User(id=42, is_bot=False, first_name="Тест"),
        message=message,
        data=data,
    )
    # Act
    await dispatcher.feed_update(
        bot, Update(update_id=1, callback_query=callback), assistant=service
    )
    # Assert
    getattr(service, method).assert_awaited_once_with(*arguments)
    service.respond.assert_not_called()
    replies = [
        c.args[1]
        for c in bot.session.call_args_list
        if len(c.args) > 1 and isinstance(c.args[1], SendMessage)
    ]
    assert len(replies) == 1 and replies[0].reply_markup is not None
