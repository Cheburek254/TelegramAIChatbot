"""Настройки отправок меняются командами пользователя, не ответом модели."""

import uuid
from datetime import UTC, datetime

from app.material_service import format_briefing_time, parse_briefing_time
from app.material_store import MaterialStore
from app.orchestration.contracts import City
from app.orchestration.store import DAILY_REMINDER_LIMIT, decode

DAILY_HELP = (
    "Чтобы добавить напоминание, отправьте:\n"
    "/daily_reminders add 19:00 Повторить английский\n\n"
    "Время берётся из /timezone и сохраняется при создании. "
    "Первое напоминание придёт в ближайшее выбранное время после подтверждения."
)


class Notifications:
    def __init__(self, store, geography, *, clock=None):
        self.store, self.geography = store, geography
        self.clock = clock or (lambda: datetime.now(UTC))

    async def city(self, owner, text):
        if not 1 <= len(text.strip()) <= 100:
            return "Укажите /city Санкт-Петербург, Россия", None
        candidates = await self.geography.search(text.strip())
        if not candidates:
            return "Город не найден. Укажите город и страну: /city …", None
        if len(candidates) != 1:
            return (
                "Уточните страну или регион командой /city:\n"
                + "\n".join(f"• {c.name}, {c.country}, {c.region}" for c in candidates[:5]),
                None,
            )
        city = candidates[0]
        identity = await self.store.city_proposal(owner, city.model_dump(), self.clock())
        return (
            f"📍 Подтвердите город: {city.name}, {city.country}, {city.region}.\n"
            "Этот город будет использоваться для утренней погоды. Смена города отключает "
            "сводку до повторного /briefing_on. Подтверждение действует 5 минут.",
            identity,
        )

    async def confirm(self, owner, identity, *, cancel=False):
        ok = await self.store.confirm_city(owner, uuid.UUID(str(identity)), self.clock(), cancel)
        return (
            "✅ Город сохранён. Для сводки задайте /timezone и выполните /briefing_on."
            if ok
            else "Город не изменён: отменено, срок истёк или карточка недействительна."
        )

    async def consent(self, owner, enabled):
        if not await self.store.consent(owner, enabled):
            return "Сначала подтвердите город через /city и задайте /timezone Europe/Moscow."
        row = await self.store.profile(owner)
        when = format_briefing_time(row.get("briefing_minute", 480)) if row else "08:00"
        return (
            f"☀️ Ежедневная сводка включена: в {when} вашей зоны. Отключить: /briefing_off."
            if enabled
            else "🔕 Ежедневная сводка отключена. Личные напоминания продолжат приходить."
        )

    async def settings(self, owner):
        row = await self.store.profile(owner)
        if not row:
            return "Сводка выключена. Настройте /city и /timezone."
        city = City.model_validate(decode(row["city"])) if row["city"] else None
        return (
            "🔔 Уведомления\n\nГород: "
            + (f"{city.name}, {city.country}" if city else "не настроен")
            + "\nЗона: "
            + (row.get("timezone") or "не настроена")
            + "\nСводка в "
            + format_briefing_time(row.get("briefing_minute", 480))
            + ": "
            + ("включена" if row["briefing_enabled"] else "выключена")
            + "\n/city — город\n/briefing_time ЧЧ:ММ — время\n"
            "/briefing_on — включить\n/briefing_off — выключить"
        )

    async def time(self, owner, key, text):
        try:
            minute = parse_briefing_time(text.strip())
        except ValueError:
            return "🕒 Укажите время вашей зоны: /briefing_time 09:30 (00:00–23:59).", None
        row = await self.store.profile(owner)
        if not row:
            return "Сначала откройте /notifications в личном чате.", None
        pending = await MaterialStore(self.store.pool).proposal(
            owner, key, "briefing_time", {"minute": minute}, self.clock()
        )
        return (
            f"🕒 Отправлять ежедневную сводку в {format_briefing_time(minute)} "
            f"в зоне {row.get('timezone') or 'из /timezone'}?\n\n"
            "Подтверждение действует 5 минут. Если сводка сегодня уже отправлена, "
            "новое время применяется со следующего дня. Включение: /briefing_on.",
            pending,
        )

    async def add_daily(self, owner, key, args):
        parts = args.split(maxsplit=2)
        if len(parts) != 3 or parts[0] != "add":
            return DAILY_HELP, None
        try:
            minute = parse_briefing_time(parts[1])
        except ValueError:
            return "Укажите время в формате ЧЧ:ММ (00:00–23:59).", None
        text = parts[2].strip()
        if not 1 <= len(text) <= 500 or "\0" in text:
            return "Введите текст напоминания длиной от 1 до 500 символов.", None
        profile = await self.store.profile(owner)
        timezone = profile and profile["timezone"]
        if not timezone:
            return "Сначала задайте свой часовой пояс, например /timezone Europe/Moscow.", None
        pending = await self.store.daily_proposal(
            owner, key, self.clock(), text=text, minute=minute, timezone=timezone
        )
        return (
            f"🔔 Напоминать каждый день в {format_briefing_time(minute)} · {timezone}?\n\n"
            f"{text}\n\nПодтверждение действует 5 минут.",
            pending,
        )

    async def list_daily(self, owner):
        return await self.store.daily_reminders(owner)

    async def stop_daily(self, owner, key, identity):
        reminder = await self.store.daily_reminder(owner, identity)
        if not reminder:
            return "Напоминание уже отключено или карточка устарела.", None
        pending = await self.store.daily_proposal(owner, key, self.clock(), identity=identity)
        return (
            f"🔕 Отключить ежедневное напоминание в "
            f"{format_briefing_time(reminder['minute'])} · {reminder['timezone']}?\n\n"
            f"{reminder['text']}\n\nПодтверждение действует 5 минут.",
            pending,
        )

    async def confirm_daily(self, owner, identity, *, cancel=False):
        status = await self.store.confirm_daily(owner, identity, self.clock(), cancel=cancel)
        return {
            "added": "✅ Ежедневное напоминание включено.",
            "stopped": "🔕 Ежедневное напоминание отключено.",
            "cancelled": "Действие отменено.",
            "limit": f"Можно включить до {DAILY_REMINDER_LIMIT} ежедневных напоминаний.",
            "invalid": "Карточка уже использована, устарела или недействительна.",
        }[status]
