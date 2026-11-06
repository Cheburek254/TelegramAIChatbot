"""Меню задач и напоминаний в личном чате."""

import uuid

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.handlers.assistant import send_parts
from app.itmo import ITMOError
from app.planner import DAILY_PATTERN, MOVE_CHOICES, repeat_task_intent, task_intent, when_text
from app.planner_store import PAGE_SIZE
from app.presentation import confirmation_keyboard, draft_button, planner_button, planner_item_rows
from app.study_planning import FREE_HELP, FREE_PREFIX, free_query
from app.timezones import zone

TITLES = {"t": "📌 Задачи и дедлайны", "r": "🔔 Разовые напоминания"}
HELP = {
    "t": "Напишите «Сдать отчёт в пятницу до 18:00» или «Задача: разобрать конспект».",
    "r": "Напишите «Напомни завтра в 18:00 проверить расписание» в режиме «Учёба».",
}
RESULT = {
    "added": "✅ Задача добавлена.",
    "done": "✅ Отмечено готовым.",
    "done_today": "✅ Отмечено за сегодня. Серия продолжится завтра.",
    "moved": "🕒 Время изменено.",
    "alert_set": "🔔 Оповещение настроено.",
    "alert_off": "🔕 Оповещение отключено.",
    "too_soon": "Для оповещения слишком поздно. Выберите срок позже или измените оповещение.",
    "cancelled": "Действие отменено.",
    "past": "Это время уже прошло. Создайте новое подтверждение.",
    "invalid": "Карточка уже использована, устарела или недействительна.",
    "stale": "Запись уже изменилась. Откройте список заново.",
    "restored": "↩️ Задача снова в открытых.",
    "restore_conflict": "Следующее повторение уже изменено или по нему пришло оповещение.",
    "repeated": "✅ Готово. Следующее повторение добавлено в задачи.",
    "repeat_off": "🔕 Повторение отключено.",
    "focus_added": "🔔 Напоминание на выбранное окно поставлено.",
}


def text_intent(message: Message):
    if not message.text or getattr(message, "forward_origin", None):
        return False
    daily = DAILY_PATTERN.fullmatch(message.text.strip())
    if daily:
        return {"intent": ("d", f"add {daily[1]} {daily[2]}")}
    repeat = repeat_task_intent(message.text)
    if repeat:
        return {"intent": ("repeat", repeat)}
    free = FREE_PREFIX.match(message.text.strip())
    if free and (args := free_query(message.text.strip()[free.end() :])):
        return {"intent": ("free", args)}
    task = task_intent(message.text)
    return {"intent": ("t", task)} if task else False


def menu_button(label, kind, offset=0):
    return InlineKeyboardButton(text=label, callback_data=f"plan:list:{kind}:{offset}")


def create_planner_router(planner, *, study=None, bot_username=None):
    router = Router(name="planner")
    router.message.filter(F.chat.type == "private", F.from_user.is_bot == False)  # noqa: E712
    router.callback_query.filter(F.message.chat.type == "private")

    async def reply(message, text, pending=None, *, prefix="plan"):
        await send_parts(
            message,
            text,
            keyboard=confirmation_keyboard(pending, prefix=prefix) if pending else None,
        )

    async def listing(message, owner, kind, offset=0):
        items = await planner.store.active(owner, kind, offset)
        lines, rows = [TITLES[kind]], []
        for number, item in enumerate(items[:PAGE_SIZE], start=offset + 1):
            preview = " ".join(item["text"].split())
            if len(preview) > 140:
                preview = preview[:139] + "…"
            lines.append(f"{number}. {preview}\n{when_text(kind, item)}")
            rows.append([planner_button(f"Открыть №{number}", "open", kind, item)])
        if not items:
            lines.append("Открытых записей нет.")
        lines.append(HELP[kind])
        pages = []
        if offset:
            pages.append(menu_button("← Предыдущие", kind, offset - PAGE_SIZE))
        if len(items) > PAGE_SIZE:
            pages.append(menu_button("Следующие →", kind, offset + PAGE_SIZE))
        if pages:
            rows.append(pages)
        label, command = (
            ("➕ Добавить задачу", "/tasks add ")
            if kind == "t"
            else ("➕ Добавить напоминание", "/remind ")
        )
        rows.append([draft_button(label, command, bot_username)])
        if kind == "t":
            rows.extend(
                [
                    [draft_button("🔁 Повторяющаяся задача", "/tasks repeat ", bot_username)],
                    [draft_button("🔎 Найти свободное окно", "/free ", bot_username)],
                    [InlineKeyboardButton(text="✅ История задач", callback_data="plan:history:0")],
                ]
            )
        if kind == "r":
            rows.append(
                [InlineKeyboardButton(text="🔁 Ежедневные напоминания", callback_data="daily:menu")]
            )
        rows.append([InlineKeyboardButton(text="← Инструменты", callback_data="agent:tools")])
        await send_parts(
            message, "\n\n".join(lines), keyboard=InlineKeyboardMarkup(inline_keyboard=rows)
        )

    async def history(message, owner, offset=0, query=""):
        tasks = await planner.store.history(owner, offset, query)
        lines, rows = ["✅ Завершённые задачи"], []
        for number, item in enumerate(tasks[:PAGE_SIZE], offset + 1):
            completed = item["completed_at"].astimezone(zone(item["timezone"]))
            preview = " ".join(item["text"].split())[:140]
            lines.append(f"{number}. {completed:%d.%m.%Y %H:%M} · {preview}")
            rows.append([planner_button(f"↩️ Вернуть №{number}", "restore", "t", item)])
        if not tasks:
            lines.append("Подходящих завершённых задач нет.")
        pages = []
        if not query:
            if offset:
                pages.append(
                    InlineKeyboardButton(
                        text="← Предыдущие", callback_data=f"plan:history:{offset - PAGE_SIZE}"
                    )
                )
            if len(tasks) > PAGE_SIZE:
                pages.append(
                    InlineKeyboardButton(
                        text="Следующие →", callback_data=f"plan:history:{offset + PAGE_SIZE}"
                    )
                )
        elif len(tasks) > PAGE_SIZE:
            lines.append("Показаны первые 6 совпадений. Уточните поисковый запрос.")
        if pages:
            rows.append(pages)
        rows.extend(
            [
                [draft_button("🔎 Искать по названию", "/tasks done ", bot_username)],
                [menu_button("← К открытым задачам", "t")],
            ]
        )
        await send_parts(
            message, "\n\n".join(lines), keyboard=InlineKeyboardMarkup(inline_keyboard=rows)
        )

    async def card(message, kind, item, notice=None, *, moving=False):
        back = (
            InlineKeyboardButton(text="← Ежедневные", callback_data="daily:menu")
            if kind == "d"
            else menu_button("← К списку", kind)
        )
        buttons = (
            [[planner_button(label, option, kind, item)] for label, option in MOVE_CHOICES[kind]]
            if moving
            else planner_item_rows(kind, item)
        )
        title = (
            "Выберите перенос"
            if moving
            else notice
            or {"t": "📌 Задача", "r": "🔔 Разовое напоминание", "d": "🔁 Ежедневное напоминание"}[
                kind
            ]
        )
        text = f"{item['text']}\n\n{when_text(kind, item)}"
        reference = f"{kind}-{item['id'].hex}-{item['version']}"
        if moving:
            label = "✏️ Своё время" if kind == "d" else "📅 Своя дата и время"
            buttons.insert(0, [draft_button(label, f"/move {reference} ", bot_username)])
            text += (
                "\n\nВведите новое время, например 19:30."
                if kind == "d"
                else "\n\nВведите дату и время, например 2026-11-20 18:30."
            )
        elif kind == "t" and item["due_at"]:
            lead = item.get("notify_before")
            text += f"\n🔔 За {lead} мин." if lead else "\n🔕 Оповещение выключено."
            buttons.append(
                [
                    draft_button(
                        "🔔 За сколько минут оповестить?", f"/task_alert {reference} ", bot_username
                    )
                ]
            )
            if lead:
                buttons.append(
                    [
                        draft_button(
                            "🔕 Отключить оповещение", f"/task_alert {reference} off", bot_username
                        )
                    ]
                )
        if kind == "t" and item.get("repeat_days") and not moving:
            label = "каждый день" if item["repeat_days"] == 1 else "каждую неделю"
            text += f"\n🔁 Повторяется {label}."
            buttons.append([planner_button("Отключить повторение", "stop_repeat", kind, item)])
        if title:
            text = f"{title}\n\n{text}"
        await send_parts(
            message, text, keyboard=InlineKeyboardMarkup(inline_keyboard=[*buttons, [back]])
        )

    @router.message(Command("tasks", "reminders"))
    async def menu(message: Message, command: CommandObject):
        owner = message.from_user.id
        args = (command.args or "").split(maxsplit=1)
        key = f"telegram:{message.chat.id}:{message.message_id}"
        if command.command == "tasks" and args and args[0] == "done":
            await history(message, owner, query=args[1].strip()[:140] if len(args) == 2 else "")
            return
        if command.command == "tasks" and args and args[0] == "repeat":
            parsed = repeat_task_intent(args[1]) if len(args) == 2 else None
            if parsed:
                intent, days = parsed
                text, pending = await planner.add_task(owner, key, intent, repeat_days=days)
                await reply(message, text, pending)
            else:
                await reply(message, "Пример: /tasks repeat каждую пятницу в 18:00 сдавать отчёт.")
            return
        if command.command == "tasks" and len(args) == 2 and args[0] == "add":
            intent = task_intent("Задача: " + args[1])
            if intent:
                text, pending = await planner.add_task(
                    owner, f"telegram:{message.chat.id}:{message.message_id}", intent
                )
                await reply(message, text, pending)
                return
        await listing(message, owner, "t" if command.command == "tasks" else "r")

    @router.message(Command("move", "task_alert"))
    async def custom(message: Message, command: CommandObject):
        owner = message.from_user.id
        args = (command.args or "").split(maxsplit=1)
        alert = command.command == "task_alert"
        try:
            reference, value = args
            kind, raw_id, raw_version = reference.split("-")
            identity, version = uuid.UUID(hex=raw_id), int(raw_version)
            if kind not in MOVE_CHOICES or (alert and kind != "t"):
                raise ValueError
        except ValueError:
            await reply(
                message, "Откройте карточку и допишите значение после подставленной команды."
            )
            return
        item = await planner.store.item(owner, kind, identity)
        if not item or item["version"] != version:
            await reply(message, RESULT["stale"])
            return
        key = f"telegram:{message.chat.id}:{message.message_id}"
        text, pending = (
            await planner.task_alert(owner, key, item, value.strip().lower())
            if alert
            else await planner.change(owner, key, kind, item, "custom", custom=value.strip())
        )
        await reply(message, text, pending)

    @router.message(text_intent)
    async def create(message: Message, intent):
        owner = message.from_user.id
        key = f"telegram:{message.chat.id}:{message.message_id}"
        kind, value = intent
        if kind == "d":
            text, pending = await planner.notifications.add_daily(owner, key, value)
            await reply(message, text, pending, prefix="daily")
        elif kind == "repeat":
            item, days = value
            text, pending = await planner.add_task(owner, key, item, repeat_days=days)
            await reply(message, text, pending)
        elif kind == "free":
            await show_windows(message, value)
        else:
            text, pending = await planner.add_task(owner, key, value)
            await reply(message, text, pending)

    async def show_windows(message, args):
        if not study or not args:
            await reply(message, FREE_HELP)
            return
        try:
            text, proposals = await study.windows(
                message.from_user.id, f"free:{message.message_id}", args
            )
        except (ITMOError, OSError, TimeoutError):
            await reply(
                message, "Не удалось проверить актуальное расписание ИТМО. Попробуйте позже."
            )
            return
        rows = [
            [InlineKeyboardButton(text=label, callback_data=f"plan:confirm:{identity}")]
            for label, identity in proposals
        ]
        rows.append([menu_button("← К задачам", "t")])
        await send_parts(message, text, keyboard=InlineKeyboardMarkup(inline_keyboard=rows))

    @router.message(Command("free"))
    async def free(message: Message, command: CommandObject):
        await show_windows(message, free_query(command.args or ""))

    @router.message(F.forward_origin, F.text | F.caption)
    async def forwarded(message: Message):
        if not study:
            return
        proposals = await study.extract(
            message.from_user.id, f"forward:{message.message_id}", message.text or message.caption
        )
        if proposals is None:
            await reply(message, "Не удалось разобрать объявление. Попробуйте позже.")
        elif not proposals:
            await reply(message, "В объявлении не нашлось задач, которые можно предложить.")
        else:
            for text, pending in proposals:
                await reply(message, text, pending)

    @router.callback_query(F.data.startswith("plan:"))
    async def action(query: CallbackQuery):
        await query.answer()
        message, owner = query.message, query.from_user.id
        if not isinstance(message, Message) or message.chat.id != owner:
            return
        _, operation, *parts = query.data.split(":")
        if operation in {"confirm", "cancel"} and len(parts) == 1:
            try:
                identity = uuid.UUID(parts[0])
            except ValueError:
                return
            status, kind, target = await planner.store.confirm(
                owner, identity, planner.clock(), cancel=operation == "cancel"
            )
            item = await planner.store.item(owner, kind, target) if target else None
            if item:
                await card(message, kind, item, RESULT[status])
            else:
                await reply(message, RESULT[status])
            return
        if operation == "history" and len(parts) == 1:
            try:
                offset = int(parts[0])
            except ValueError:
                return
            if 0 <= offset <= 2**31 - PAGE_SIZE and offset % PAGE_SIZE == 0:
                await history(message, owner, offset)
            return
        if operation == "list" and len(parts) == 2 and parts[0] in TITLES:
            try:
                offset = int(parts[1])
            except ValueError:
                return
            if 0 <= offset <= 2**31 - PAGE_SIZE and offset % PAGE_SIZE == 0:
                await listing(message, owner, parts[0], offset)
            return
        if len(parts) != 3 or parts[0] not in MOVE_CHOICES:
            return
        kind = parts[0]
        try:
            identity, version = uuid.UUID(parts[1]), int(parts[2])
        except ValueError:
            return
        item = (
            await planner.store.completed(owner, identity)
            if operation == "restore" and kind == "t"
            else await planner.store.item(owner, kind, identity)
        )
        if not item or item["version"] != version:
            await reply(message, RESULT["stale"])
        elif operation in {"open", "move"}:
            await card(message, kind, item, moving=operation == "move")
        elif kind == "t" and operation in {"restore", "stop_repeat"}:
            text, pending = await planner.task_operation(
                owner, "callback:" + query.id, item, operation
            )
            await reply(message, text, pending)
        elif operation == "done" or any(option == operation for _, option in MOVE_CHOICES[kind]):
            text, pending = await planner.change(
                owner, "callback:" + query.id, kind, item, operation
            )
            await reply(message, text, pending)

    return router
