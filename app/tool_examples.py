"""Synthetic few-shot examples. Relative dates always use the trusted clock."""

import json
from datetime import UTC, timedelta

from app.timezones import zone

ROUTES = (
    ("Чё у меня завтра за парочки?", "schedule", "read", "day"),
    ("Глянь расписашку на эту недельку", "schedule", "read", "week"),
    ("Какие дедлайники у меня горят?", "tasks", "read", "upcoming"),
    ("Есть у меня что-нибудь просроченное?", "tasks", "read", "overdue"),
    ("Глянь мои таски без ограничения по дате", "tasks", "read", "all"),
    ("Как там погодка в Питере прямо сейчас?", "lifestyle", "read", "default"),
    ("Нужен ли сейчас зонт в Казани?", "lifestyle", "read", "default"),
    ("Напомни мне через пару минут проверить чай", "schedule", "reminder", "default"),
    ("Поставь напоминалочку через минуточку проверить чай", "schedule", "reminder", "default"),
    ("Можешь мне напомнить через полтора часа про отчёт?", "schedule", "reminder", "default"),
    ("Найди материал про идемпотентность", "knowledge", "read", "default"),
    ("Что в моих конспектиках говорится про MCP?", "knowledge", "answer", "default"),
    ("Перечисли мои конспектики и учебные материалы", "knowledge", "read", "all"),
    ("Переведи фразу «напомни через пару минут»", "legacy", "legacy", "default"),
    ("Проверь код приложения с расписанием и погодой", "legacy", "legacy", "default"),
    ("Что такое дедлайн?", "legacy", "legacy", "default"),
    ("Подготовь меня к завтра", "clarify", "clarify", "default"),
)


def route_examples():
    messages = []
    for text, target, intent, scope in ROUTES:
        messages.extend(
            [
                {"role": "user", "content": text},
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "target": target,
                            "intent": intent,
                            "scope": scope,
                            "confidence": 1.0,
                            "reason": "example",
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
    return messages


def argument_examples(name, now, timezone):
    local = now.astimezone(zone(timezone))
    tomorrow = local.date() + timedelta(days=1)
    examples = {
        "get_weather": [
            ("Как там погодка в Питере прямо сейчас?", {"city": "Санкт-Петербург, Россия"}),
            ("Какая погода в Казани, Россия?", {"city": "Казань, Россия"}),
        ],
        "get_schedule": [
            ("Что у меня завтра по занятиям?", {"date": tomorrow.isoformat()}),
            ("Покажи пары сегодня", {"date": local.date().isoformat()}),
            (
                "Какие у меня парочки через пару деньков?",
                {
                    "date": (local.date() + timedelta(days=2)).isoformat(),
                },
            ),
        ],
        "get_tasks": [
            ("Какие дедлайники у меня горят?", {"scope": "upcoming"}),
            ("Есть что-нибудь просроченное?", {"scope": "overdue"}),
        ],
        "search_materials": [
            ("Найди конспектик про идемпотентность", {"query": "идемпотентность", "limit": 5}),
            ("Поищи в моих конспектиках про MCP", {"query": "MCP", "limit": 5}),
        ],
        "add_reminder": [
            (
                "Напомни через минутку проверить чай",
                {
                    "text": "проверить чай",
                    "remind_at": (now.astimezone(UTC) + timedelta(minutes=1))
                    .astimezone(zone(timezone))
                    .isoformat(),
                },
            ),
            (
                "Напомни мне через пару минут проверить чай",
                {
                    "text": "проверить чай",
                    "remind_at": (now.astimezone(UTC) + timedelta(minutes=2))
                    .astimezone(zone(timezone))
                    .isoformat(),
                },
            ),
            (
                "Можешь напомнить через полтора часа про отчёт?",
                {
                    "text": "про отчёт",
                    "remind_at": (now.astimezone(UTC) + timedelta(minutes=90))
                    .astimezone(zone(timezone))
                    .isoformat(),
                },
            ),
            (
                "Напомни завтра в 18:00 проверить расписание",
                {
                    "text": "проверить расписание",
                    "remind_at": local.replace(
                        year=tomorrow.year,
                        month=tomorrow.month,
                        day=tomorrow.day,
                        hour=18,
                        minute=0,
                        second=0,
                        microsecond=0,
                    ).isoformat(),
                },
            ),
        ],
    }
    return examples[name]


def extraction_examples(name, now, timezone):
    messages = []
    for text, arguments in argument_examples(name, now, timezone):
        messages.extend(
            [
                {"role": "user", "content": text},
                {"role": "assistant", "content": json.dumps(arguments, ensure_ascii=False)},
            ]
        )
    return messages
