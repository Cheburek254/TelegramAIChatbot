from unittest.mock import AsyncMock, Mock

from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup, MessageEntity

from app.handlers.assistant import START_TEXT
from app.presentation import draft_button, main_keyboard, section_keyboard
from app.system_speech import SystemSpeechMiddleware


async def test_start_menu_is_styled_only_for_owner_and_keeps_commands_and_callbacks():
    current = AsyncMock(side_effect=lambda owner: "yiddish" if owner == 101 else "plain")
    middleware = SystemSpeechMiddleware(Mock(current=current))
    sender = AsyncMock()
    original = SendMessage(
        chat_id=101, text=START_TEXT, parse_mode="HTML", reply_markup=main_keyboard()
    )
    await middleware(sender, None, original)
    styled = sender.await_args.args[1]
    assert styled.parse_mode == "HTML"
    assert all(command in styled.text for command in ("/study", "/translate", "/review", "/tasks"))
    assert "Шо за ошибка" in styled.text and "Смысл таки бережём" in styled.text
    old = original.reply_markup.inline_keyboard[0][0]
    new = styled.reply_markup.inline_keyboard[0][0]
    assert old.text != new.text and old.callback_data == new.callback_data
    plain = original.model_copy(update={"chat_id": 202})
    await middleware(sender, None, plain)
    assert sender.await_args.args[1] is plain
    schedule = original.model_copy(
        update={
            "text": "📅 Ваше личное расписание",
            "reply_markup": section_keyboard("schedule"),
        }
    )
    await middleware(sender, None, schedule)
    rows = sender.await_args.args[1].reply_markup.inline_keyboard
    assert [row[0].text for row in rows] == [
        "✏️ Добавить / изменить занятия",
        "📅 Посмотреть расписание",
        "← Таки назад",
    ]


async def test_system_heading_changes_without_altering_data_or_command_draft():
    middleware = SystemSpeechMiddleware(Mock(current=AsyncMock(return_value="yiddish")))
    sender = AsyncMock()
    text = "📅 Расписание на 2026-11-07\nИсточник: ИТМО\n\n09:50 · А&Б"
    button = draft_button("➕ Добавить задачу", "/tasks add ", "synthetic_bot")
    method = SendMessage(
        chat_id=101,
        text=text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )
    await middleware(sender, None, method)
    rendered = sender.await_args.args[1]
    assert rendered.text == text.replace("📅 Расписание на", "📅 Шо по расписанию на", 1)
    assert rendered.reply_markup.inline_keyboard[0][0].url == button.url
    model_reply = method.model_copy(
        update={"entities": [MessageEntity(type="bold", offset=0, length=2)]}
    )
    await middleware(sender, None, model_reply)
    assert sender.await_args.args[1].text == text
