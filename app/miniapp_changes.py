"""Валидированные действия панели на общих механизмах подтверждения."""

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent_store import ActionError
from app.material_service import format_briefing_time, parse_briefing_time
from app.planner import MAX_ALERT_MINUTES, Planner, when_text
from app.schedule_contracts import ScheduleLesson
from app.timezones import local_candidates, zone

Version = Annotated[int, Field(ge=1, lt=2**31)]
SUCCESS = {
    "added",
    "done",
    "repeated",
    "moved",
    "edited",
    "focus_added",
    "restored",
    "done_today",
    "stopped",
    "removed",
}
MESSAGES = {
    "done": "Готово!",
    "repeated": "Готово. Следующая задача уже в списке.",
    "restored": "Задача снова в открытых.",
    "removed": "Разовое напоминание удалено.",
    "stopped": "Ежедневное напоминание выключено.",
    "too_soon": "До дедлайна осталось меньше времени, чем задано для оповещения.",
    "past": "Выбранное время уже прошло.",
    "limit": "Можно включить до 10 ежедневных напоминаний.",
    "stale": "Запись уже изменилась. Обновите список.",
    "restore_conflict": "Следующее повторение уже изменено или по нему пришло оповещение.",
}


class FormError(ValueError):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LessonChange(Strict):
    kind: Literal["lesson"]
    operation: Literal["save", "delete"]
    id: str | None = None
    version: Version | None = None
    lesson: ScheduleLesson | None = None


class RecordChange(Strict):
    kind: Literal["t", "r", "d"]
    operation: Literal["save", "done", "move", "restore", "remove"]
    id: str | None = None
    version: Version | None = None
    text: Annotated[str, Field(max_length=500)] = ""
    when: Annotated[str, Field(max_length=16)] = ""
    repeat: Literal[1, 7] | None = None
    lead: Annotated[int, Field(ge=1, le=MAX_ALERT_MINUTES)] | None = None


def local_instant(value, timezone, current=None):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}", value):
        raise FormError("Выберите дату и время.")
    if current and current.astimezone(zone(timezone)).isoformat(timespec="minutes")[:16] == value:
        return current
    try:
        candidates = local_candidates(datetime.fromisoformat(value), timezone)
    except (ValueError, OverflowError):
        candidates = []
    if len(candidates) != 1:
        raise FormError("Это местное время недоступно или неоднозначно. Выберите другое.")
    return candidates[0].astimezone(UTC)


class MiniAppChanges:
    def __init__(self, schedule, planner, notifications, clock):
        self.schedule, self.store, self.notifications = schedule, planner, notifications
        self.clock = clock

    async def prepare(self, owner, request_id, raw, now):
        if not isinstance(raw, dict):
            raise FormError("Проверьте поля формы.")
        try:
            change = (LessonChange if raw.get("kind") == "lesson" else RecordChange).model_validate(
                raw
            )
        except ValidationError:
            raise FormError("Проверьте поля формы и время занятия.") from None
        if (change.id is None) != (change.version is None):
            raise FormError("Обновите список перед изменением записи.")
        key = f"miniapp:{uuid.UUID(request_id)}"
        if isinstance(change, LessonChange):
            return await self.lesson(owner, key, change, now)
        return await self.record(owner, key, change, now)

    async def lesson(self, owner, key, change, now):
        identity = int(change.id) if change.id and re.fullmatch(r"[0-9]{1,19}", change.id) else None
        if change.id is not None and not (identity and 0 < identity < 2**63):
            raise FormError("Обновите список занятий.")
        lesson = change.lesson
        if change.operation == "delete":
            if identity is None or lesson:
                raise FormError("Выберите занятие из личного расписания.")
            operation = "delete"
        else:
            if lesson is None or "\0" in lesson.title + lesson.location:
                raise FormError("Заполните название, аудиторию, дату и время.")
            operation = "edit" if identity else "add"
        try:
            row = await self.schedule.store.prepare(
                owner,
                key,
                operation,
                identity,
                lesson.model_dump() if lesson else None,
                now,
                expected_version=change.version,
            )
        except ActionError:
            raise FormError("Занятие изменилось или недоступно. Обновите расписание.") from None
        card = self.schedule.card(row)
        return "schedule", card.pending_id, card.content

    async def record(self, owner, key, change, now):
        profile = await self.notifications.profile(owner)
        if not profile or profile["destination_chat_id"] != owner:
            raise FormError("Отправьте /start в личном чате с ботом, затем откройте панель заново.")
        planner = Planner(self.store, None, self.schedule.default_timezone, clock=self.clock)
        identity = uuid.UUID(change.id) if change.id is not None else None
        kind, operation = change.kind, change.operation
        if kind != "t" and (change.repeat is not None or change.lead is not None):
            raise FormError("Повтор задачи и оповещение до дедлайна доступны только для задач.")
        item = None
        if identity:
            item = (
                await self.store.completed(owner, identity)
                if operation == "restore" and kind == "t"
                else await self.store.item(owner, kind, identity)
            )
            if not item or item["version"] != change.version:
                raise FormError("Запись изменилась или недоступна. Обновите список.")
        if operation != "save":
            if not item or (operation == "restore" and kind != "t"):
                raise FormError("Выберите запись из списка.")
            if operation == "remove":
                if kind == "t":
                    raise FormError("Удаление доступно для напоминаний.")
                pending = await self.store.proposal(
                    owner, key, now, kind, operation, target=identity, version=change.version
                )
                label = (
                    "Удалить разовое напоминание?"
                    if kind == "r"
                    else "Выключить ежедневное напоминание?"
                )
                message = f"{label}\n\n{item['text']}"
            elif operation == "restore":
                message, pending = await planner.task_operation(owner, key, item, operation)
            else:
                message, pending = await planner.change(
                    owner,
                    key,
                    kind,
                    item,
                    operation if operation == "done" else "custom",
                    custom=change.when.replace("T", " ") if operation == "move" else None,
                )
            return "planner", pending, message
        title = change.text.strip()
        if not title or "\0" in title:
            raise FormError("Введите название.")
        timezone = (
            item["timezone"] if item else profile["timezone"] or self.schedule.default_timezone
        )
        minute, due = change.lead, None
        if kind == "d":
            try:
                minute = parse_briefing_time(change.when)
            except ValueError:
                raise FormError("Укажите время ежедневного напоминания.") from None
            if not identity:
                pending = await self.notifications.daily_proposal(
                    owner,
                    key,
                    now,
                    text=title,
                    minute=minute,
                    timezone=timezone,
                )
                return "daily", pending, f"Напоминать каждый день в {change.when}?\n{title}"
        else:
            if change.when:
                timing = "due_at" if kind == "t" else "remind_at"
                current = item[timing] if item else None
                due = local_instant(change.when, timezone, current)
                if due <= now and due != current:
                    raise FormError("Выберите время в будущем.")
            elif kind == "r" or change.repeat or minute:
                raise FormError("Укажите дату и время.")
            if kind == "t" and minute and due <= now + timedelta(minutes=minute):
                if not item or (due, minute) != (item["due_at"], item["notify_before"]):
                    raise FormError(MESSAGES["too_soon"])
        operation = "edit" if identity else "add" if kind == "t" else "focus"
        pending = await self.store.proposal(
            owner,
            key,
            now,
            kind,
            operation,
            target=identity,
            version=change.version,
            text=title,
            timezone=timezone,
            due_at=due,
            minute=minute,
            repeat_days=change.repeat,
        )
        when = (
            f"{format_briefing_time(minute)} · {timezone}"
            if kind == "d"
            else when_text("t", {"due_at": due, "timezone": timezone})
        )
        return "planner", pending, f"Сохранить запись?\n{title}\n{when}"

    async def confirm(self, owner, domain, identity, now):
        if domain == "schedule":
            try:
                row = await self.schedule.store.confirm(owner, identity, now)
            except ActionError:
                return False, "Занятие недоступно или превышен лимит на день."
            status = row["state"]
            return status == "completed", self.schedule.result(row).content
        if domain == "daily":
            status = await self.notifications.confirm_daily(owner, identity, now)
        elif domain == "planner":
            status, _, _ = await self.store.confirm(owner, identity, now)
        else:
            raise FormError("Некорректное подтверждение.")
        return status in SUCCESS, MESSAGES.get(
            status,
            "Изменения сохранены."
            if status in SUCCESS
            else "Подтверждение уже использовано или устарело.",
        )
