"""Меню задач и напоминаний в личном чате."""

import uuid

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.handlers.assistant import send_parts
from app.planner import DAILY_PATTERN, MOVE_CHOICES, task_intent, when_text
from app.planner_store import PAGE_SIZE
from app.presentation import confirmation_keyboard, planner_button, planner_item_rows

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
    "cancelled": "Действие отменено.",
    "past": "Это время уже прошло. Создайте новое подтверждение.",
    "invalid": "Карточка уже использована, устарела или недействительна.",
    "stale": "Запись уже изменилась. Откройте список заново.",
}


def text_intent(message: Message):
    if not message.text:
        return False
    daily = DAILY_PATTERN.fullmatch(message.text.strip())
    if daily:
        return {"intent": ("d", f"add {daily[1]} {daily[2]}")}
    task = task_intent(message.text)
    return {"intent": ("t", task)} if task else False


def menu_button(label, kind, offset=0):
    return InlineKeyboardButton(text=label, callback_data=f"plan:list:{kind}:{offset}")


def create_planner_router(planner):
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
        if kind == "r":
            rows.append(
                [InlineKeyboardButton(text="🔁 Ежедневные напоминания", callback_data="daily:menu")]
            )
        rows.append([InlineKeyboardButton(text="← Инструменты", callback_data="agent:tools")])
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
        title = "Выберите перенос" if moving else notice
        text = f"{item['text']}\n\n{when_text(kind, item)}"
        if title:
            text = f"{title}\n\n{text}"
        await send_parts(
            message, text, keyboard=InlineKeyboardMarkup(inline_keyboard=[*buttons, [back]])
        )

    @router.message(Command("tasks", "reminders"))
    async def menu(message: Message, command: CommandObject):
        await listing(message, message.from_user.id, "t" if command.command == "tasks" else "r")

    @router.message(text_intent)
    async def create(message: Message, intent):
        owner = message.from_user.id
        key = f"telegram:{message.chat.id}:{message.message_id}"
        kind, value = intent
        if kind == "d":
            text, pending = await planner.notifications.add_daily(owner, key, value)
            await reply(message, text, pending, prefix="daily")
        else:
            text, pending = await planner.add_task(owner, key, value)
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
        item = await planner.store.item(owner, kind, identity)
        if not item or item["version"] != version:
            await reply(message, RESULT["stale"])
        elif operation in {"open", "move"}:
            await card(message, kind, item, moving=operation == "move")
        elif operation == "done" or any(option == operation for _, option in MOVE_CHOICES[kind]):
            text, pending = await planner.change(
                owner, "callback:" + query.id, kind, item, operation
            )
            await reply(message, text, pending)

    return router
