"""Telegram-команды уведомлений и материалов."""

import asyncio
import io
import uuid
from pathlib import PurePosixPath

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.handlers.assistant import send_parts
from app.material_service import HELP, MAX_FILE_BYTES, format_briefing_time
from app.orchestration.notifications import DAILY_HELP
from app.presentation import confirmation_keyboard, planner_button, settings_keyboard


def notifications_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="☀️ Включить сводку", callback_data="features:on"),
                InlineKeyboardButton(text="🔕 Отключить", callback_data="features:off"),
            ],
            [InlineKeyboardButton(text="📍 Выбрать город", callback_data="features:city")],
            [InlineKeyboardButton(text="🕒 Время сводки", callback_data="features:time")],
            [InlineKeyboardButton(text="🔁 Ежедневные напоминания", callback_data="daily:menu")],
            [InlineKeyboardButton(text="← Настройки", callback_data="settings")],
        ]
    )


class ContactMiddleware(BaseMiddleware):
    def __init__(self, store):
        self.store = store

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        user = event.from_user
        if (
            isinstance(message, Message)
            and message.chat.type == "private"
            and user
            and not user.is_bot
        ):
            await self.store.contact(user.id, message.chat.id)
        return await handler(event, data)


def materials_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить материал", callback_data="material:add")],
            [InlineKeyboardButton(text="📚 Мои материалы", callback_data="material:list")],
            [InlineKeyboardButton(text="🗑 Удалить материал", callback_data="material:delete")],
            [InlineKeyboardButton(text="← Инструменты", callback_data="agent:tools")],
        ]
    )


class BoundedUpload(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_FILE_BYTES:
            raise ValueError("file_too_large")
        return super().write(data)


def create_feature_router(notifications, supervisor, materials=None):
    router = Router(name="features")
    router.message.filter(F.chat.type == "private", F.from_user.is_bot == False)  # noqa: E712
    router.callback_query.filter(F.message.chat.type == "private")

    async def daily_menu(message, owner, notice=None):
        reminders = await notifications.list_daily(owner)
        lines = ["🔁 Ежедневные напоминания"]
        buttons = []
        for index, reminder in enumerate(reminders, 1):
            when = format_briefing_time(reminder["minute"])
            preview = " ".join(reminder["text"].split())
            if len(preview) > 120:
                preview = preview[:119] + "…"
            lines.append(f"{index}. {when} · {reminder['timezone']}\n{preview}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🔕 Отключить №{index} · {when}",
                        callback_data=f"daily:stop:{reminder['id']}",
                    ),
                    planner_button("Открыть", "open", "d", reminder),
                ]
            )
        if not reminders:
            lines.append("У вас пока нет ежедневных напоминаний.")
        lines.append(DAILY_HELP)
        if notice:
            lines.insert(0, notice)
        buttons.append([InlineKeyboardButton(text="← Инструменты", callback_data="agent:tools")])
        await send_parts(
            message,
            "\n\n".join(lines),
            keyboard=InlineKeyboardMarkup(
                inline_keyboard=buttons,
            ),
        )

    @router.message(Command("daily_reminders"))
    async def daily_command(message: Message, command: CommandObject):
        owner = message.from_user.id
        if not command.args:
            await daily_menu(message, owner)
            return
        text, pending = await notifications.add_daily(
            owner, f"telegram:{message.chat.id}:{message.message_id}", command.args
        )
        if pending:
            await send_parts(message, text, keyboard=confirmation_keyboard(pending, prefix="daily"))
        elif text == DAILY_HELP:
            await daily_menu(message, owner)
        else:
            await daily_menu(message, owner, text)

    @router.callback_query(F.data.startswith("daily:"))
    async def daily_action(query: CallbackQuery):
        await query.answer()
        if not isinstance(query.message, Message) or query.message.chat.id != query.from_user.id:
            return
        owner = query.from_user.id
        _, action, *parts = query.data.split(":", 2)
        if action == "menu" and not parts:
            await daily_menu(query.message, owner)
            return
        if action not in {"stop", "confirm", "cancel"} or len(parts) != 1:
            return
        try:
            identity = uuid.UUID(parts[0])
        except ValueError:
            return
        if action == "stop":
            text, pending = await notifications.stop_daily(owner, "callback:" + query.id, identity)
            if pending:
                await send_parts(
                    query.message, text, keyboard=confirmation_keyboard(pending, prefix="daily")
                )
                return
        else:
            text = await notifications.confirm_daily(owner, identity, cancel=action == "cancel")
        await daily_menu(query.message, owner, text)

    async def material_response(message, text, pending=None):
        await send_parts(
            message,
            text,
            keyboard=(
                confirmation_keyboard(pending, prefix="content")
                if pending
                else materials_keyboard()
            ),
        )

    @router.message(Command("briefing_time"))
    async def briefing_time(message: Message, command: CommandObject):
        text, pending = await notifications.time(
            message.from_user.id,
            f"telegram:{message.chat.id}:{message.message_id}",
            command.args or "",
        )
        await send_parts(
            message,
            text,
            keyboard=(
                confirmation_keyboard(pending, prefix="content")
                if pending
                else notifications_keyboard()
            ),
        )

    @router.message(Command("materials"))
    async def personal_materials(message: Message, command: CommandObject):
        if not materials:
            await send_parts(message, "Личные материалы временно недоступны.")
            return
        text, pending = await materials.command(
            message.from_user.id,
            f"telegram:{message.chat.id}:{message.message_id}",
            command.args or "",
        )
        await material_response(message, text, pending)

    @router.message(F.document)
    async def upload_material(message: Message):
        if not materials:
            return
        document = message.document
        filename = PurePosixPath((document.file_name or "").replace("\\", "/"))
        if filename.suffix.lower() not in {".txt", ".md", ".markdown"}:
            await material_response(
                message, "Поддерживаются файлы TXT и Markdown в UTF-8.\n\n" + HELP
            )
            return
        if document.file_size and document.file_size > MAX_FILE_BYTES:
            await material_response(message, "Файл слишком большой: максимум 200 КБ.")
            return
        try:
            stream = BoundedUpload()
            async with asyncio.timeout(25):
                await message.bot.download(document, destination=stream)
            content = stream.getvalue().decode("utf-8-sig")
        except (ValueError, UnicodeError):
            await material_response(message, "Нужен текстовый файл UTF-8 размером до 200 КБ.")
            return
        except Exception:
            await material_response(message, "Не удалось загрузить файл. Попробуйте позже.")
            return
        title = (message.caption or filename.stem).strip()
        text, pending = await materials.add(
            message.from_user.id, f"telegram:{message.chat.id}:{message.message_id}", title, content
        )
        await material_response(message, text, pending)

    @router.callback_query(F.data.startswith("content:"))
    async def content_confirm(query: CallbackQuery):
        await query.answer()
        if (
            not materials
            or not isinstance(query.message, Message)
            or query.message.chat.id != query.from_user.id
        ):
            return
        try:
            _, operation, identity = query.data.split(":", 2)
            if operation not in {"confirm", "cancel"}:
                return
            text = await materials.confirm(
                query.from_user.id, uuid.UUID(identity), operation == "cancel"
            )
        except ValueError:
            text = "Карточка недействительна."
        await send_parts(query.message, text, keyboard=settings_keyboard())

    @router.callback_query(F.data.startswith("material:"))
    async def material_menu(query: CallbackQuery):
        await query.answer()
        if (
            not materials
            or not isinstance(query.message, Message)
            or query.message.chat.id != query.from_user.id
        ):
            return
        choice = query.data.split(":", 1)[1]
        if choice == "list":
            text, _ = await materials.command(query.from_user.id, "", "")
        elif choice == "delete":
            text, _ = await materials.command(query.from_user.id, "", "")
            text += "\n\nДля удаления: /materials delete ID (ID в квадратных скобках)."
        else:
            text = HELP
        await material_response(query.message, text)

    @router.message(Command("city"))
    async def city(message: Message, command: CommandObject):
        try:
            text, pending = await notifications.city(message.from_user.id, command.args or "")
            await message.answer(
                text,
                parse_mode=None,
                reply_markup=(
                    confirmation_keyboard(pending, prefix="city")
                    if pending
                    else notifications_keyboard()
                ),
            )
        except Exception:
            await send_parts(message, "Не удалось найти город. Повторите позже.")

    @router.message(Command("briefing_on", "briefing_off", "notifications", "trace"))
    async def command_handler(message: Message, command: CommandObject):
        owner = message.from_user.id
        if command.command == "trace":
            text = await supervisor.trace(owner, (command.args or "last").strip())
        elif command.command == "notifications":
            text = await notifications.settings(owner)
        else:
            text = await notifications.consent(owner, command.command == "briefing_on")
        await send_parts(
            message,
            text,
            keyboard=(
                notifications_keyboard()
                if command.command.startswith("briefing") or command.command == "notifications"
                else None
            ),
        )

    @router.callback_query(F.data.startswith("city:"))
    async def city_confirm(query: CallbackQuery):
        await query.answer()
        if not isinstance(query.message, Message) or query.message.chat.id != query.from_user.id:
            return
        try:
            _, operation, identity = query.data.split(":", 2)
            if operation not in {"confirm", "cancel"}:
                return
            text = await notifications.confirm(
                query.from_user.id, uuid.UUID(identity), cancel=operation == "cancel"
            )
        except ValueError:
            text = "Подтверждение города недействительно."
        await query.message.answer(text, parse_mode=None, reply_markup=notifications_keyboard())

    @router.callback_query(F.data.startswith("features:"))
    async def menu(query: CallbackQuery):
        await query.answer()
        if not isinstance(query.message, Message) or query.message.chat.id != query.from_user.id:
            return
        choice, owner = query.data.split(":")[1], query.from_user.id
        if choice == "city":
            text = (
                "📍 Напишите /city Санкт-Петербург, Россия. Город сохранится после подтверждения."
            )
        elif choice == "time":
            await query.message.answer(
                "🕒 Выберите время сводки в вашей зоне или напишите /briefing_time 09:30.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text=t, callback_data="features:at:" + t)
                            for t in ("07:00", "08:00", "09:00")
                        ],
                        [
                            InlineKeyboardButton(
                                text="← Уведомления", callback_data="features:settings"
                            )
                        ],
                    ]
                ),
                parse_mode=None,
            )
            return
        elif choice == "at":
            text, pending = await notifications.time(
                owner, "callback:" + query.id, query.data.split(":", 2)[2]
            )
            await send_parts(
                query.message,
                text,
                keyboard=(
                    confirmation_keyboard(pending, prefix="content")
                    if pending
                    else notifications_keyboard()
                ),
            )
            return
        elif choice in {"on", "off"}:
            text = await notifications.consent(owner, choice == "on")
        elif choice == "trace":
            text = await supervisor.trace(owner)
        elif choice == "materials":
            await material_response(query.message, HELP)
            return
        else:
            text = await notifications.settings(owner)
        await query.message.answer(
            text,
            parse_mode=None,
            reply_markup=settings_keyboard() if choice == "trace" else notifications_keyboard(),
        )

    return router
