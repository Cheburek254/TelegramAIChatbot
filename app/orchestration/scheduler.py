"""Постоянный outbox, ограниченные повторы и восстановление lease после сбоя."""

import asyncio
from datetime import UTC, datetime, time, timedelta

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.context import split_telegram
from app.material_contracts import SearchOutput
from app.mcp_service.contracts import ScheduleOutput, WeatherOutput
from app.orchestration.contracts import City
from app.orchestration.policy import PolicyBridge
from app.orchestration.store import decode
from app.orchestration.tracing import Trace, span
from app.planner_store import PlannerStore
from app.presentation import planner_item_rows
from app.timezones import zone


def daily_time(local_date, timezone, minute):
    tz = zone(timezone)
    due = datetime.combine(local_date, time(minute // 60, minute % 60), tz)
    # Missing DST time moves forward by the gap; repeated time uses fold=0.
    # Compare instants so the second fold cannot make a due event look future.
    return due.astimezone(UTC).astimezone(tz)


def briefing_due(now, timezone, minute=480):
    local = now.astimezone(zone(timezone))
    due = daily_time(local.date(), timezone, minute)
    return due if now >= due.astimezone(UTC) else None


def daily_reminder_times(reminder, first, last):
    timezone = reminder["timezone"]
    local_date = first.astimezone(zone(timezone)).date()
    end_date = last.astimezone(zone(timezone)).date()
    while local_date <= end_date:
        due = daily_time(local_date, timezone, reminder["minute"])
        instant = due.astimezone(UTC)
        if (
            due.date() == local_date
            and local_date != reminder.get("skip_on")
            and first <= instant <= last
            and instant > reminder["starts_at"]
        ):
            yield instant, local_date
        local_date += timedelta(days=1)


class BriefingService:
    def __init__(self, store, bridge, *, timeout=50):
        self.store, self.bridge, self.timeout = store, bridge, timeout

    async def prepare(self, owner, profile, now):
        timezone = profile["timezone"]
        local = now.astimezone(zone(timezone))
        city = City.model_validate(decode(profile["city"]))
        sections = []

        async def section(component, operation):
            try:
                async with (
                    asyncio.timeout(self.timeout),
                    span(component, "briefing_section", intent="daily_briefing"),
                ):
                    return await operation()
            except Exception:
                return "⚠️ Данные временно недоступны."

        async def schedule():
            policy = PolicyBridge(self.bridge, "schedule", readonly=True, owner=owner)
            data = ScheduleOutput.model_validate(
                await policy.call("get_schedule", {"date": local.date().isoformat()}, owner=owner)
            )
            return (
                data.source
                + "\n"
                + (
                    "\n".join(f"{p.start}–{p.end} {p.title} · {p.location}" for p in data.lessons)
                    or "На сегодня занятий нет."
                )
            )

        async def reminders():
            first = datetime.combine(local.date(), time(), zone(timezone))
            last = datetime.combine(local.date() + timedelta(days=1), time(), zone(timezone))
            rows = await self.store.reminders_between(owner, first, last)
            entries = [(row["remind_at"], row["text"]) for row in rows]
            for reminder in await self.store.daily_reminders(owner):
                for due, _ in daily_reminder_times(
                    reminder,
                    first.astimezone(UTC),
                    last.astimezone(UTC) - timedelta(microseconds=1),
                ):
                    entries.append((due, reminder["text"]))
            return (
                "\n".join(
                    f"{due.astimezone(zone(timezone)):%H:%M} {text}"
                    for due, text in sorted(entries)
                )
                or "На сегодня напоминаний нет."
            )

        async def weather():
            policy = PolicyBridge(self.bridge, "lifestyle", readonly=True, owner=owner)
            data = WeatherOutput.model_validate(
                await policy.call("get_weather", {"city": city.name + ", " + city.country})
            )
            if data.status != "ok":
                return "⚠️ Погода не определена; проверьте подтверждённый город."
            recommendation = (
                "Возьмите тёплую верхнюю одежду."
                if data.temperature_c < 10
                else "Подберите одежду по температуре ниже."
            )
            if data.wind_speed >= 10:
                recommendation += " Сильный ветер: учтите его при прогулке."
            return (
                f"{city.name}: {data.temperature_c} °C, ветер {data.wind_speed} {data.wind_unit}, "
                f"код условий {data.weather_code}. {recommendation}\n"
                f"{data.source} · {data.observed_at}"
            )

        async def tasks():
            end = datetime.combine(local.date() + timedelta(days=1), time(), zone(timezone))
            rows = await PlannerStore(self.store.pool).briefing_tasks(owner, end)
            lines = []
            for row in rows:
                due = row["due_at"]
                if due:
                    due = due.astimezone(zone(timezone))
                    label = f"{due:%d.%m %H:%M} · " + ("просрочено · " if due < local else "")
                else:
                    label = "Без срока · "
                lines.append(label + row["text"])
            return "\n".join(lines) or "Нет открытых задач на сегодня."

        async def knowledge():
            policy = PolicyBridge(self.bridge, "knowledge", readonly=True)
            await policy.resource("materials://catalog")
            # Fixed rotating focus avoids retaining/caching personal lesson names.
            topics = ["MCP инструменты", "контекстное окно", "идемпотентность доставка"]
            query = topics[local.date().toordinal() % len(topics)]
            data = SearchOutput.model_validate(
                await policy.call("search_materials", {"query": query, "limit": 1})
            )
            if not data.hits:
                return "В материалах не найден учебный фокус."
            hit = data.hits[0]
            return f"Повторите: {hit.title}.\n{hit.excerpt[:650]}\n[{hit.doc_id}]"

        for component, name, operation in [
            ("schedule", "📅 Занятия", schedule),
            ("schedule", "🔔 Напоминания", reminders),
            ("schedule", "📌 Задачи", tasks),
            ("lifestyle", "🌦 Погода", weather),
            ("knowledge", "📚 Учебный фокус", knowledge),
        ]:
            value = await section(component, operation)
            parts = split_telegram(value, limit=720)
            sections.append(name + "\n" + parts[0] + ("…" if len(parts) > 1 else ""))
        return (
            f"☀️ Ежедневная сводка · {local:%d.%m.%Y}\n"
            f"Подготовлено {local:%H:%M} · {timezone}\n\n" + "\n\n".join(sections)
        )[:3900]


class Scheduler:
    def __init__(
        self,
        store,
        bridge,
        sender,
        *,
        clock=None,
        interval=30,
        briefing_timeout=50,
        itmo_watch=None,
    ):
        self.store, self.bridge, self.sender = store, bridge, sender
        self.clock = clock or (lambda: datetime.now(UTC))
        self.interval = interval
        self.briefing = BriefingService(store, bridge, timeout=briefing_timeout)
        self.stopped = asyncio.Event()
        self.itmo_watch = itmo_watch

    async def prepare_due(self):
        now = self.clock()
        await self.store.enqueue_reminders(now)
        await self.store.enqueue_task_alerts(now)
        for reminder in await self.store.active_daily_reminders():
            for due, local_date in daily_reminder_times(reminder, now - timedelta(minutes=15), now):
                await self.store.enqueue_daily_reminder(reminder, due, local_date)
        for profile in await self.store.profiles():
            due = briefing_due(now, profile["timezone"], profile.get("briefing_minute", 480))
            if due:
                await self.store.enqueue_briefing(profile, due, due.date())

    async def deliver_one(self):
        event = await self.store.claim(self.clock())
        if not event:
            return False
        async with Trace(
            self.store, event["user_id"], clock=self.clock, kind=event["kind"]
        ) as trace:
            try:
                profile = await self.store.profile(event["user_id"])
                send_options = {"parse_mode": None}
                if event["kind"] in {"reminder_delivery", "daily_reminder_delivery"}:
                    daily = event["kind"] == "daily_reminder_delivery"
                    if daily:
                        reminder = await self.store.daily_reminder(
                            event["user_id"], event["daily_reminder_id"]
                        )
                    else:
                        reminder = await self.store.reminder(event["user_id"], event["reminder_id"])
                    if not reminder or reminder["version"] != event["reminder_version"]:
                        async with self.store.delivery(event, self.clock()):
                            pass
                        return True
                    send_options["reply_markup"] = InlineKeyboardMarkup(
                        inline_keyboard=planner_item_rows("d" if daily else "r", reminder)
                    )
                    local = event["due_at"].astimezone(zone(reminder["timezone"]))
                    label = "Ежедневное напоминание" if daily else "Напоминание"
                    text = (
                        f"🔔 {label} · {local:%d.%m.%Y %H:%M} ({reminder['timezone']})\n"
                        f"{reminder['text']}"
                    )
                    if self.clock() > event["due_at"] + timedelta(minutes=1 if daily else 0):
                        text += "\n⏱ Доставлено с задержкой."
                elif event["kind"] == "task_deadline":
                    task = await PlannerStore(self.store.pool).item(
                        event["user_id"], "t", event["task_id"]
                    )
                    if not task or task["version"] != event["reminder_version"]:
                        async with self.store.delivery(event, self.clock()):
                            pass
                        return True
                    local = task["due_at"].astimezone(zone(task["timezone"]))
                    text = (
                        f"🔔 Дедлайн · {local:%d.%m.%Y %H:%M} ({task['timezone']})\n{task['text']}"
                    )
                    send_options["reply_markup"] = InlineKeyboardMarkup(
                        inline_keyboard=planner_item_rows("t", task)
                    )
                elif event["kind"] == "itmo_change":
                    text = event["payload"]
                    send_options["reply_markup"] = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="🎓 Открыть расписание ИТМО",
                                    callback_data="schedule:itmo_get",
                                )
                            ]
                        ]
                    )
                else:
                    if (
                        not profile
                        or not profile["briefing_enabled"]
                        or profile["consent_version"] != event["consent_version"]
                    ):
                        async with self.store.delivery(event, self.clock()):
                            pass
                        return True
                    text = event["payload"] or await self.briefing.prepare(
                        event["user_id"], profile, self.clock()
                    )
                    send_options["reply_markup"] = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="📌 Открыть задачи", callback_data="plan:list:t:0"
                                )
                            ]
                        ]
                    )
                if not await self.store.payload(event, text, trace.id):
                    return True
                async with self.store.delivery(event, self.clock()) as target:
                    if target:
                        destination, conn = target
                        try:
                            async with (
                                asyncio.timeout(20),
                                span("telegram", "delivery", intent=event["kind"]),
                            ):
                                await self.sender.send_message(destination, text, **send_options)
                        except (TelegramForbiddenError, TelegramBadRequest):
                            await self.store.complete(
                                event,
                                self.clock(),
                                error="permanent_telegram",
                                permanent=True,
                                conn=conn,
                            )
                        except (
                            TelegramNetworkError,
                            TelegramServerError,
                            TelegramRetryAfter,
                            TimeoutError,
                            OSError,
                        ) as exc:
                            await self.store.complete(
                                event,
                                self.clock(),
                                error="temporary_telegram",
                                conn=conn,
                                retry_after=getattr(exc, "retry_after", 0),
                            )
                        else:
                            await self.store.complete(event, self.clock(), conn=conn)
            except Exception:
                await self.store.complete(event, self.clock(), error="preparation_failed")
        return True

    async def tick(self):
        await self.prepare_due()
        # Bound work per tick and let cancellation shut down promptly.
        for _ in range(20):
            if not await self.deliver_one():
                break
        await self.store.cleanup(self.clock())

    async def run(self):
        async with asyncio.TaskGroup() as group:
            if self.itmo_watch:
                group.create_task(self.itmo_watch.run(self.stopped))
            await self._run()

    async def _run(self):
        while not self.stopped.is_set():
            try:
                await self.tick()
            except Exception:
                # No user-facing error spam, payload/owner never enter logs.
                pass
            try:
                await asyncio.wait_for(self.stopped.wait(), self.interval)
            except TimeoutError:
                pass
