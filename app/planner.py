"""Разбор явных просьб и подтверждения личных задач без вызова модели."""

import re
from datetime import UTC, date, datetime, time, timedelta

from app.material_service import format_briefing_time
from app.timezones import local_candidates, zone

TASK_PREFIX = r"^(?:задача\s*:\s*|(?:добавь|создай|поставь)\s+задачу\s*[:—]?\s+)"
DAILY_PATTERN = re.compile(
    r"^напом(?:ни|инай)\s+(?:мне\s+)?(?:каждый день|ежедневно)\s+в\s+(\S+)\s+(.+)$",
    re.I,
)
WEEKDAYS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресенье")
DAY = (
    r"послезавтра|завтра|сегодня|понедельник(?:а)?|вторник(?:а)?|сред[уыа]|четверг(?:а)?|"
    r"пятниц[уыа]|суббот[уыа]|воскресень[ея]|[0-9]{4}-[0-9]{2}-[0-9]{2}|"
    r"[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]{4})?"
)
DEADLINE = re.compile(
    rf"^(?P<title>.+?)\s+(?:(?P<link>до|к|в|на)\s+)?(?P<day>{DAY})"
    r"(?:\s+(?:(?:до|к|в)\s+)?(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2}))?[.!]?$",
    re.I,
)
TODAY_DEADLINE = re.compile(
    r"^(?P<title>.+?)\s+(?:до|к|в)\s+(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})[.!]?$",
    re.I,
)
EXPLICIT_TASK = re.compile(
    r"^(?:сдать|сделать|купить|подготовить|прочитать|закончить|отправить|выучить|"
    r"написать|решить|проверить|оплатить|заполнить|записаться|забрать)\b",
    re.I,
)
MOVE_CHOICES = {
    "t": (("На час позже", "hour"), ("На день позже", "day"), ("На неделю позже", "week")),
    "r": (("На час позже", "hour"), ("На день позже", "day"), ("На неделю позже", "week")),
    "d": (("На час раньше", "earlier"), ("На час позже", "hour")),
}


def task_intent(text):
    text = text.strip()
    if text.endswith("?") or text.startswith("/") or "\n" in text:
        return None
    prefix = re.match(TASK_PREFIX, text, re.I)
    if prefix:
        text = text[prefix.end() :].strip()
    match = DEADLINE.fullmatch(text) or TODAY_DEADLINE.fullmatch(text)
    if match and (prefix or EXPLICIT_TASK.match(text)):
        return match["title"].rstrip(" ,"), match
    if prefix:
        return text, None
    return None


def local_due(match, now, timezone):
    local = now.astimezone(zone(timezone))
    today = local.date()
    raw_day = (match.groupdict().get("day") or "сегодня").lower()
    clock = time(int(match["hour"]), int(match["minute"])) if match["hour"] else time(23, 59)
    relative = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    if raw_day in relative:
        day = today + timedelta(days=relative[raw_day])
    elif "-" in raw_day:
        day = date.fromisoformat(raw_day)
    elif "." in raw_day:
        day_number, month, *year = map(int, raw_day.split("."))
        day = date(year[0] if year else today.year, month, day_number)
    else:
        weekday = next(n for n, stem in enumerate(WEEKDAYS) if raw_day.startswith(stem[:8]))
        day = today + timedelta(days=(weekday - today.weekday()) % 7)
        if datetime.combine(day, clock) <= local.replace(tzinfo=None):
            day += timedelta(days=7)
    candidates = local_candidates(datetime.combine(day, clock), timezone)
    if len(candidates) != 1 or candidates[0].astimezone(UTC) <= now:
        raise ValueError("invalid_deadline")
    return candidates[0].astimezone(UTC)


def when_text(kind, item):
    if kind == "d":
        return f"{format_briefing_time(item['minute'])} · {item['timezone']}"
    due = item["due_at" if kind == "t" else "remind_at"]
    if due is None:
        return "Без срока"
    return f"{due.astimezone(zone(item['timezone'])):%d.%m.%Y %H:%M} · {item['timezone']}"


class Planner:
    def __init__(self, store, notifications, timezone, *, clock=None):
        self.store, self.notifications, self.timezone = store, notifications, timezone
        self.clock = clock or (lambda: datetime.now(UTC))

    async def add_task(self, owner, key, intent):
        title, match = intent
        if not 1 <= len(title) <= 500 or "\0" in title:
            return "Введите название задачи длиной от 1 до 500 символов.", None
        if match is None and re.search(r"\d:\d|\b(?:сегодня|завтра|послезавтра)\b", title, re.I):
            return "Укажите срок в конце сообщения, например «Задача: отчёт завтра в 18:00».", None
        profile = await self.notifications.store.profile(owner)
        timezone = (profile and profile["timezone"]) or self.timezone
        now = self.clock()
        try:
            due = local_due(match, now, timezone) if match else None
        except ValueError:
            return (
                "Выберите корректный будущий срок, например «Задача: отчёт завтра в 18:00».",
                None,
            )
        pending = await self.store.proposal(
            owner, key, now, "t", "add", text=title, timezone=timezone, due_at=due
        )
        when = when_text("t", {"timezone": timezone, "due_at": due})
        return (
            f"📌 Добавить задачу?\n\n{title}\n{when}\n\nПодтверждение действует 5 минут.",
            pending,
        )

    async def change(self, owner, key, kind, item, option):
        now = self.clock()
        if option == "done":
            today = now.astimezone(zone(item["timezone"])).date() if kind == "d" else None
            if today and item["skip_on"] == today:
                return "Сегодняшнее напоминание уже отмечено.", None
            pending = await self.store.proposal(
                owner,
                key,
                now,
                kind,
                "done",
                target=item["id"],
                version=item["version"],
                local_date=today,
            )
            title = f"Отметить готовым за {today:%d.%m}?" if today else "Отметить готовым?"
            return f"✅ {title}\n\n{item['text']}", pending
        timezone = item["timezone"]
        if kind == "d":
            minute = (item["minute"] + (60 if option == "hour" else -60)) % 1440
            due = None
            label = when_text(kind, {"minute": minute, "timezone": timezone})
        else:
            minute = None
            current = item["due_at" if kind == "t" else "remind_at"]
            if option == "hour":
                due = max(now, current or now) + timedelta(hours=1)
            else:
                local = now.astimezone(zone(timezone))
                current_local = current.astimezone(zone(timezone)) if current else None
                day = max(local.date(), current_local.date() if current_local else local.date())
                day += timedelta(days=1 if option == "day" else 7)
                clock = current_local.time() if current_local else time(23, 59)
                candidates = local_candidates(datetime.combine(day, clock), timezone)
                if len(candidates) != 1:
                    return "В новой дате это время неоднозначно. Выберите другой перенос.", None
                due = candidates[0].astimezone(UTC)
            label = when_text("t", {"due_at": due, "timezone": timezone})
        pending = await self.store.proposal(
            owner,
            key,
            now,
            kind,
            "move",
            target=item["id"],
            version=item["version"],
            due_at=due,
            minute=minute,
        )
        return f"🕒 Перенести на {label}?\n\n{item['text']}", pending
