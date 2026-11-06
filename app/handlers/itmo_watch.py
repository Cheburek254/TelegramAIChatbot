"""Подтверждение подписки на изменения расписания ИТМО."""

import uuid
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.handlers.assistant import send_parts
from app.presentation import confirmation_keyboard


def create_itmo_watch_router(store, client):
    router = Router(name="itmo_watch")
    router.message.filter(F.chat.type == "private", F.from_user.is_bot == False)  # noqa: E712
    router.callback_query.filter(F.message.chat.type == "private")

    async def menu(message, owner):
        enabled = await store.enabled(owner)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔕 Выключить" if enabled else "🔔 Включить",
                        callback_data="iw:off" if enabled else "iw:on",
                    )
                ],
                [InlineKeyboardButton(text="← Назад", callback_data="schedule:get")],
            ]
        )
        status = "включены" if enabled else "выключены"
        await send_parts(
            message,
            f"🔔 Уведомления ИТМО {status}.\n\n"
            "Проверяю ближайшие 7 дней примерно раз в 30 минут. "
            "Для работы нужен подключённый аккаунт /itmo.",
            keyboard=keyboard,
        )

    async def propose(message, owner, key, choice):
        enabled = choice == "on"
        if enabled and not client.connected(owner):
            await send_parts(message, "Сначала подключите аккаунт ИТМО командой /itmo.")
            return
        identity = await store.propose(owner, key, enabled, datetime.now(UTC))
        action = "включить" if enabled else "выключить"
        await send_parts(
            message,
            f"Подтвердите, что хотите {action} уведомления об изменениях расписания ИТМО.",
            keyboard=confirmation_keyboard(identity, prefix="iw"),
        )

    @router.message(Command("itmo_alerts"))
    async def alerts(message: Message, command: CommandObject):
        owner = message.from_user.id
        choice = (command.args or "").strip().lower()
        if choice in {"on", "off"}:
            await propose(message, owner, f"message:{message.message_id}", choice)
        else:
            await menu(message, owner)

    @router.callback_query(F.data.startswith("iw:"))
    async def button(query: CallbackQuery):
        await query.answer()
        message, owner = query.message, query.from_user.id
        if not isinstance(message, Message) or message.chat.id != owner:
            return
        _, action, *parts = query.data.split(":")
        if action == "menu" and not parts:
            await menu(message, owner)
        elif action in {"on", "off"} and not parts:
            await propose(message, owner, f"callback:{query.id}", action)
        elif action in {"confirm", "cancel"} and len(parts) == 1:
            try:
                identity = uuid.UUID(parts[0])
            except ValueError:
                return
            enabled = await store.confirm(
                owner, identity, datetime.now(UTC), cancel=action == "cancel"
            )
            if action == "cancel":
                await send_parts(message, "Действие отменено.")
            elif enabled is None:
                await send_parts(message, "Подтверждение уже использовано или устарело.")
            else:
                await menu(message, owner)

    return router
