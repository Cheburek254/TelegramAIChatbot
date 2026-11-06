"""Команды личного расписания: без LLM, с проверкой и подтверждением изменений."""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.agent_store import ActionError
from app.itmo import ITMOError
from app.schedule_contracts import ScheduleLesson, ScheduleRepeat, future_itmo_lessons
from app.schedule_store import lesson_payload
from app.timezones import zone

HELP = (
    "📅 Моё расписание\n\n"
    "Занятия видны только вам. Добавление, изменение и удаление — после подтверждения.\n\n"
    "Посмотреть: /schedule 2026-11-02\nНеделя: /schedule week\n\n"
    "Добавить:\n/schedule add 2026-11-02 10:00-11:30 | Математика | Аудитория 301\n\n"
    "Изменить занятие №7 (номер есть в списке):\n"
    "/schedule edit 7 2026-11-02 12:00-13:30 | Математика | Аудитория 305\n\n"
    "Удалить: /schedule delete 7\n\n"
    "Повторять (даты начала/конца, день 1=пн..7=вс, all/odd/even, дата первой учебной недели):\n"
    "/schedule repeat 2026-11-02 2026-12-31 1 odd 2026-09-01 10:00-11:30 | Математика | 301\n"
    "Удалить всю серию по номеру любой пары: /schedule delete_series 7\n\n"
    "ИТМО: /schedule itmo 2026-11-02 2026-11-08\n"
    "Сохранить ИТМО: /schedule import 2026-11-02 2026-11-08\n"
    "Кнопка сохранения берёт ближайшие 14 дней, только ещё не начавшиеся занятия.\n\n"
    "Для относительных дат задайте /timezone Europe/Moscow."
)
ERRORS = {
    "not_owned": "Занятие или подтверждение не найдено в вашем расписании.",
    "schedule_overlap": "Это занятие пересекается с другим вашим занятием. Измените время.",
    "schedule_full": "На одну дату можно сохранить максимум 12 занятий.",
    "not_a_series": "Это разовое занятие. Используйте /schedule delete с его номером.",
    "stale_series": "Серия уже изменилась. Подготовьте удаление заново.",
    "itmo_not_connected": (
        "ИТМО ещё не подключён. Нажмите «Подключить ИТМО» в меню расписания или отправьте /itmo. "
        "Пароль вводится на отдельной HTTPS-странице, не в переписке."
    ),
    "itmo_auth_required": "ИТМО отклонил авторизацию. Подключите аккаунт заново: /itmo.",
    "itmo_import_past": (
        "Импортировать можно только будущие занятия. Выберите даты от сегодняшней "
        "или нажмите «Сохранить ИТМО · 14 дней»."
    ),
    "itmo_unavailable": "API ИТМО сейчас недоступен. Личное расписание не изменено.",
    "itmo_invalid_response": "Ответ ИТМО не прошёл проверку. Личное расписание не изменено.",
    "itmo_invalid_range": "Для ИТМО укажите период не более 31 дня.",
}


@dataclass(frozen=True)
class ScheduleReply:
    content: str
    pending_id: uuid.UUID | None = None


def describe(payload):
    return (
        f"📅 {payload['date']} · {payload['start']}–{payload['end']}\n"
        f"📚 {payload['title']}\n📍 {payload['location']}"
    )


class ScheduleService:
    def __init__(self, store, agent_store, *, timezone="Europe/Moscow", clock=None, itmo=None):
        self.store, self.agent_store, self.default_timezone = store, agent_store, timezone
        self.clock = clock or (lambda: datetime.now(UTC))
        self.itmo = itmo

    async def button(self, owner, choice, request_key):
        if choice in {"itmo_get", "itmo_save"}:
            today = self.clock().astimezone(zone("Europe/Moscow")).date()
            first = today if choice == "itmo_save" else today - timedelta(days=today.weekday())
            last = first + timedelta(days=13 if choice == "itmo_save" else 6)
            operation = "itmo" if choice == "itmo_get" else "import"
            return await self.command(owner, f"{operation} {first} {last}", request_key)
        if choice == "week":
            return await self.command(owner, "week", request_key)
        timezone = await self.agent_store.timezone(owner) or self.default_timezone
        today = self.clock().astimezone(zone(timezone)).date()
        return await self.command(owner, today.isoformat(), request_key)

    async def command(self, owner, argument, request_key):
        try:
            tokens = argument.strip().split(maxsplit=1)
            operation = tokens[0].lower() if tokens else ""
            if operation == "account":
                return ScheduleReply(
                    f"Ваш Telegram ID для подключения ИТМО: {owner}.\n"
                    "Вход выполняется отдельно через закрытый файл сервера. "
                    "Не присылайте пароль или токен в чат."
                )
            if operation in {"itmo", "import"}:
                if self.itmo is None:
                    raise ITMOError("itmo_not_connected")
                bounds = tokens[1].split() if len(tokens) > 1 else []
                if len(bounds) != 2:
                    raise ValueError
                first, last = map(date.fromisoformat, bounds)
                if not 0 <= (last - first).days <= 30:
                    raise ITMOError("itmo_invalid_range")
                if operation == "import":
                    today = self.clock().astimezone(zone("Europe/Moscow")).date()
                    if last < today:
                        raise ITMOError("itmo_import_past")
                    first = max(first, today)
                warning = None
                if operation == "import":
                    entries = await self.itmo.fetch(owner, first, last)
                    entries = future_itmo_lessons(entries, self.clock())
                else:
                    entries, warning = await self.itmo.display(owner, first, last)
                    if entries is None:
                        if not warning:
                            raise ITMOError("itmo_not_connected")
                        entries = [
                            lesson_payload(row)
                            for row in await self.store.entries(owner, first, last)
                        ]
                if not entries:
                    return ScheduleReply(
                        (warning + "\n\n" if warning else "")
                        + (
                            "ИТМО не вернул будущих, ещё не начавшихся занятий на выбранные даты."
                            if operation == "import"
                            else "На выбранные даты в расписании нет занятий."
                        )
                    )
                if operation == "import":
                    row = await self.store.prepare(
                        owner, request_key, "import", None, entries, self.clock()
                    )
                    return self.card(row)
                return ScheduleReply(
                    (warning or "🎓 Расписание my.itmo.ru · Europe/Moscow")
                    + "\n\n"
                    + "\n\n".join(describe(entry) for entry in entries)
                    + (
                        ""
                        if warning
                        else "\n\nСохранить в своё: /schedule import " + " ".join(bounds)
                    )
                )
            if operation == "repeat":
                timing, title, location = tokens[1].split("|")
                fields = timing.split()
                if fields and fields[0] in {"all", "odd", "even"}:
                    parity, first, last, weekday, anchor, interval = fields
                else:
                    first, last, weekday, parity, anchor, interval = fields
                start, end = interval.split("-")
                repeat = ScheduleRepeat(
                    date=first,
                    until=last,
                    weekday=int(weekday),
                    parity=parity,
                    anchor=anchor,
                    start=start,
                    end=end,
                    title=title,
                    location=location,
                )
                row = await self.store.prepare(
                    owner, request_key, "repeat", None, repeat.model_dump(), self.clock()
                )
                return self.card(row)
            if operation in {"add", "edit", "delete", "delete_series"}:
                rest = tokens[1] if len(tokens) > 1 else ""
                identity = None
                if operation != "add":
                    parts = rest.split(maxsplit=1)
                    identity = int(parts[0])
                    if not 0 < identity < 2**63:
                        raise ValueError
                    rest = parts[1] if len(parts) > 1 else ""
                payload = None
                if operation in {"delete", "delete_series"}:
                    if rest:
                        raise ValueError
                else:
                    timing, title, location = rest.split("|")
                    day, interval = timing.split()
                    start, end = interval.split("-")
                    payload = ScheduleLesson(
                        date=day, start=start, end=end, title=title, location=location
                    ).model_dump()
                row = await self.store.prepare(
                    owner, request_key, operation, identity, payload, self.clock()
                )
                return self.card(row)
            if operation == "help":
                return ScheduleReply(HELP)
            timezone = await self.agent_store.timezone(owner) or self.default_timezone
            today = self.clock().astimezone(zone(timezone)).date()
            if operation == "week":
                if len(tokens) > 1:
                    raise ValueError
                first = today - timedelta(days=today.weekday())
                last = first + timedelta(days=6)
            else:
                first = date.fromisoformat(argument.strip()) if operation else today
                last = first
            rows = await self.store.entries(owner, first, last)
            lines = [
                f"📅 Моё расписание · {first:%d.%m.%Y}"
                + (f" — {last:%d.%m.%Y}" if last != first else ""),
                f"🕒 {timezone}",
            ]
            if not rows:
                lines.append("На эти даты вы ещё не добавили занятий.")
            for row in rows:
                series = " · серия" if row.get("series_id") else ""
                lines.append(f"№{row['id']}{series}\n{describe(lesson_payload(row))}")
            lines.append("Редактирование и примеры: /schedule help")
            if not operation:
                lines.append(HELP)
            return ScheduleReply("\n\n".join(lines))
        except (ActionError, ITMOError) as exc:
            return ScheduleReply(ERRORS.get(str(exc), "Изменение расписания недоступно."))
        except (ValueError, IndexError):
            return ScheduleReply(
                "Проверьте дату YYYY-MM-DD, время HH:MM-HH:MM (конец позже начала), "
                "название и место (1–200 символов).\n\n" + HELP
            )

    def card(self, row):
        if row["state"] != "pending":
            return self.result(row)
        if row["expires_at"] <= self.clock():
            return ScheduleReply("Срок подтверждения истёк. Отправьте команду заново.")
        content = json.loads(row["payload"])
        labels = {
            "add": "Добавить занятие",
            "edit": "Изменить занятие",
            "delete": "Удалить занятие",
            "repeat": "Добавить повторяющиеся занятия",
            "delete_series": "Удалить всю серию занятий",
            "import": "Сохранить расписание ИТМО",
        }
        lines = ["🔐 Подтвердите: " + labels[row["operation"]]]
        if content["before"]:
            lines.append("Сейчас:\n" + describe(content["before"]))
        if row["operation"] == "repeat":
            repeat = ScheduleRepeat.model_validate(content["after"])
            parity = {"all": "каждую", "odd": "нечётную", "even": "чётную"}[repeat.parity]
            lines.append(
                "Первая пара:\n"
                + describe({**content["after"], "date": repeat.dates()[0].isoformat()})
                + f"\nПериод {repeat.date} — {repeat.until}, день недели {repeat.weekday}, "
                f"{parity} учебную неделю.\nПервая неделя содержит {repeat.anchor}. "
                f"Всего занятий: {len(repeat.dates())}."
            )
        elif row["operation"] == "import":
            entries = content["after"]
            lines.append(
                f"Источник: my.itmo.ru. Занятий: {len(entries)}.\n"
                + "\n\n".join(describe(entry) for entry in entries)
                + "\nВремя — Europe/Moscow, часы не пересчитываются. "
                "Только ещё не начавшиеся занятия. "
                "Существующие занятия не удаляются; при пересечении отменяется весь импорт."
            )
        elif content["after"]:
            lines.append("Сохранить:\n" + describe(content["after"]))
        if row["operation"] == "delete_series":
            lines.append(f"Будет удалено занятий: {len(content['members'])}.")
        lines.append("Только в вашем расписании. Подтверждение действует 5 минут.")
        return ScheduleReply("\n\n".join(lines), row["id"])

    def result(self, row):
        messages = {
            "completed": f"✅ Изменение сохранено. Занятие №{row.get('result_id')}. /schedule",
            "cancelled": "Изменение отменено. Расписание сохранено без изменений.",
            "expired": "Срок подтверждения истёк. Отправьте команду заново.",
            "superseded": "Эта карточка заменена новым изменением. Используйте последнюю карточку.",
            "stale": "Занятие уже изменилось. Откройте /schedule и подготовьте изменение заново.",
        }
        return ScheduleReply(messages.get(row["state"], "Изменение не выполнено."))

    async def confirm(self, owner, identity, *, cancel=False):
        try:
            row = await self.store.confirm(
                owner, uuid.UUID(str(identity)), self.clock(), cancel=cancel
            )
            return self.result(row)
        except ActionError as exc:
            return ScheduleReply(ERRORS.get(str(exc), "Изменение не выполнено."))
        except ValueError:
            return ScheduleReply(ERRORS["not_owned"])
