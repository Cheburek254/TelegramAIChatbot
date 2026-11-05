"""Безопасное оформление: Telegram entities вместо HTML из ответа модели."""

import re
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

from app.context import split_telegram

MODE_ICONS = {"study": "📚", "translate": "🌐", "review": "🔎"}


def confirmation_keyboard(identity, *, prefix="reminder") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить", callback_data=f"{prefix}:confirm:{identity}"
                ),
                InlineKeyboardButton(
                    text="✖️ Отменить", callback_data=f"{prefix}:cancel:{identity}"
                ),
            ]
        ]
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data="mode:" + mode)
                for mode, label in (
                    ("study", "📚 Учёба"),
                    ("translate", "🌐 Перевод"),
                    ("review", "🔎 Ревью"),
                )
            ],
            [
                InlineKeyboardButton(text="📅 Расписание", callback_data="schedule:menu"),
                InlineKeyboardButton(text="🧰 Инструменты", callback_data="agent:tools"),
            ],
            [
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
            ],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=value, callback_data="temperature:" + value)
                for value in ("0.0", "0.3", "0.7", "1.0")
            ],
            [
                InlineKeyboardButton(text="🕒 Часовой пояс", callback_data="agent:timezone"),
                InlineKeyboardButton(text="🧹 Очистить историю", callback_data="reset"),
            ],
            [InlineKeyboardButton(text="🔔 Уведомления", callback_data="features:notifications")],
            [InlineKeyboardButton(text="← В меню", callback_data="menu")],
        ]
    )


def section_keyboard(section):
    sections = {
        "schedule": [
            ("📥 Поставить", "schedule:put"),
            ("📤 Достать", "schedule:get"),
            ("← Назад", "menu"),
        ],
        "put": [
            ("➕ Разовое занятие", "schedule:single"),
            ("🔁 Повторяющиеся", "schedule:repeat"),
            ("✏️ Изменить / удалить", "schedule:edit_help"),
            ("🎓 Сохранить ИТМО · 14 дней", "schedule:itmo_save"),
            ("← Назад", "schedule:menu"),
        ],
        "get": [
            ("🔐 Подключить ИТМО", "itmo:connect"),
            ("Сегодня", "schedule:today"),
            ("Эта неделя", "schedule:week"),
            ("🎓 Из ИТМО", "schedule:itmo_get"),
            ("← Назад", "schedule:menu"),
        ],
        "repeat": [
            ("Каждую неделю", "schedule:all"),
            ("Чётная", "schedule:even"),
            ("Нечётная", "schedule:odd"),
            ("← Назад", "schedule:put"),
        ],
        "tools": [
            ("📚 Учебные материалы", "features:materials"),
            ("🧭 Трасса запроса", "features:trace"),
            ("🌦 Погода", "tool:weather"),
            ("🔔 Разовое напоминание", "tool:reminder"),
            ("🔁 Ежедневные напоминания", "daily:menu"),
            ("🔎 Почему?", "agent:why"),
            ("← Назад", "menu"),
        ],
    }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data)]
            for label, data in sections[section]
        ]
    )


@dataclass
class FormattedPart:
    text: str
    entities: list[MessageEntity]


PATTERN = re.compile(
    r"```(?P<language>[a-zA-Z0-9_+.-]*)\n(?P<pre>.*?)```"
    r"|\*\*(?P<bold>[^\n]+?)\*\*|`(?P<code>[^`\n]+)`",
    re.DOTALL,
)


def formatted_parts(raw: str) -> list[FormattedPart]:
    text, entities, offset, cursor = [], [], 0, 0
    for match in PATTERN.finditer(raw):
        prefix = raw[cursor : match.start()]
        text.append(prefix)
        offset += len(prefix.encode("utf-16-le")) // 2
        kind = next(key for key in ("pre", "bold", "code") if match.group(key) is not None)
        value = match.group(kind)
        text.append(value)
        length = len(value.encode("utf-16-le")) // 2
        if length:
            entities.append(
                MessageEntity(
                    type=kind,
                    offset=offset,
                    length=length,
                    language=match.group("language") or None if kind == "pre" else None,
                )
            )
        offset += length
        cursor = match.end()
    text.append(raw[cursor:])
    rendered = "".join(text)
    if not rendered.strip():
        rendered, entities = raw, []
    result = []
    position = 0
    for chunk in split_telegram(rendered):
        size = len(chunk.encode("utf-16-le")) // 2
        current = []
        for entity in entities:
            start = max(entity.offset, position)
            end = min(entity.offset + entity.length, position + size)
            if start < end:
                current.append(
                    entity.model_copy(update={"offset": start - position, "length": end - start})
                )
        result.append(FormattedPart(chunk, current))
        position += size
    return result
