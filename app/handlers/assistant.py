import logging
import time
import uuid
from html import escape

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from app.agent import SCHEDULE_HEADING, Agent
from app.assistant import Assistant
from app.context import ContextTooLarge, split_telegram
from app.itmo_auth import ITMOAuth
from app.itmo_login import LoginError
from app.llm import LLMError
from app.orchestration.supervisor import TASKS_HEADING
from app.presentation import (
    confirmation_keyboard,
    draft_button,
    formatted_parts,
    main_keyboard,
    section_keyboard,
    settings_keyboard,
)
from app.schedule_service import ScheduleService
from app.speech_style import SpeechStyles

logger = logging.getLogger(__name__)
START_TEXT = (
    "<b>Telegram AI Chatbot</b>\n"
    "Ваш помощник в учёбе — от первого вопроса до понятного решения.\n\n"
    "📚 <b>Учёба</b> · /study\nОбъяснения, небольшие примеры и самопроверка.\n\n"
    "🌐 <b>Перевод</b> · /translate\nПеревод с сохранением смысла. Укажите целевой язык.\n\n"
    "🔎 <b>Ревью кода</b> · /review\nОшибки, минимальное исправление и способ проверки.\n\n"
    "<b>Управление</b>\n/settings — режим, модель и temperature\n"
    "/settings 0.3 — изменить temperature\n/reset — очистить свою историю\n\n"
    "Выберите режим кнопкой или просто напишите вопрос: по умолчанию включена учёба.\n"
    "\n🧰 /tools — погода, расписание и напоминания\n"
    "📅 /schedule — ваше личное расписание и редактирование\n"
    "🔐 /itmo — подключить аккаунт ИТМО\n"
    "🕒 /timezone Europe/Moscow — часовой пояс\n"
    "🔎 /why — последнее решение агента\n\n"
    "📚 /materials — учебные источники\n☀️ /notifications — сводка и город\n"
    "🔁 /daily_reminders — ежедневные напоминания\n"
    "📌 /tasks — задачи и дедлайны\n🔔 /reminders — разовые напоминания\n"
    "🧭 /trace last — путь обработки запроса\n\n"
    "<i>Личный чат · история сохраняется · при смене режима контекст очищается.</i>\n\n"
    "Не присылайте пароли и ключи: текст передаётся внешнему сервису модели."
)
ERROR_TEXT = "Сервис модели сейчас недоступен. Попробуйте позже."
TIMEOUT_TEXT = "Модель не успела ответить. Попробуйте позже."
STORAGE_ERROR = "Не удалось обработать запрос. Попробуйте позже."
CONTEXT_ERROR = "Сообщение слишком большое для контекста. Сократите его и отправьте снова."


def create_router(*, bot_username=None) -> Router:
    router = Router(name="assistant")
    router.message.filter(F.chat.type == "private", F.from_user.is_bot == False)  # noqa: E712
    router.callback_query.filter(F.message.chat.type == "private")

    @router.message(Command("jew"))
    async def speech_style(
        message: Message, command: CommandObject, speech_styles: SpeechStyles | None = None
    ):
        if speech_styles is None:
            await send_parts(message, "Настройка стиля временно недоступна.")
            return
        try:
            text, pending = await speech_styles.command(
                message.from_user.id,
                (command.args or "").strip().lower(),
                f"telegram:{message.chat.id}:{message.message_id}",
            )
            await send_parts(
                message,
                text,
                keyboard=(
                    confirmation_keyboard(pending, prefix="speech") if pending else main_keyboard()
                ),
            )
        except (asyncpg.PostgresError, OSError, TimeoutError):
            await send_parts(message, STORAGE_ERROR)

    @router.callback_query(F.data.startswith("speech:"))
    async def speech_confirm(query: CallbackQuery, speech_styles: SpeechStyles | None = None):
        await query.answer()
        if (
            speech_styles is None
            or not isinstance(query.message, Message)
            or query.message.chat.id != query.from_user.id
        ):
            return
        try:
            _, operation, identity = query.data.split(":", 2)
            if operation not in {"confirm", "cancel"}:
                return
            text = await speech_styles.confirm(
                query.from_user.id, uuid.UUID(identity), cancel=operation == "cancel"
            )
        except ValueError:
            text = "Карточка стиля недействительна."
        except (asyncpg.PostgresError, OSError, TimeoutError):
            text = STORAGE_ERROR
        await send_parts(query.message, text)

    @router.message(Command("start"))
    async def start(message: Message):
        await message.answer(START_TEXT, parse_mode="HTML", reply_markup=main_keyboard())

    @router.message(Command("study", "translate", "review"))
    async def mode(message: Message, command: CommandObject, assistant: Assistant):
        await perform(message, assistant.mode(message.from_user.id, command.command))

    @router.message(Command("reset"))
    async def reset(message: Message, assistant: Assistant):
        await perform(message, assistant.reset(message.from_user.id))

    @router.message(Command("settings"))
    async def settings(message: Message, command: CommandObject, assistant: Assistant):
        await perform(
            message,
            assistant.preferences(message.from_user.id, (command.args or "").strip()),
            settings=True,
            bot_username=bot_username,
        )

    @router.message(Command("tools", "timezone", "why"))
    async def agent_command(message: Message, command: CommandObject, agent: Agent | None = None):
        if agent is None:
            await send_parts(message, "Инструменты сейчас недоступны.")
            return
        try:
            if command.command == "tools":
                answer = await agent.tools()
            elif command.command == "timezone":
                answer = await agent.timezone(message.from_user.id, (command.args or "").strip())
            else:
                answer = await agent.why(message.from_user.id)
        except (asyncpg.PostgresError, OSError, TimeoutError):
            answer = STORAGE_ERROR
        await send_parts(
            message,
            answer,
            keyboard=(
                settings_keyboard(bot_username)
                if command.command == "timezone"
                else section_keyboard("tools", bot_username)
            ),
        )

    @router.message(Command("schedule"))
    async def schedule(message: Message, command: CommandObject, schedule_service: ScheduleService):
        try:
            if not (command.args or "").strip():
                await message.answer(
                    "📅 Ваше личное расписание",
                    parse_mode=None,
                    reply_markup=section_keyboard("schedule"),
                )
                return
            result = await schedule_service.command(
                message.from_user.id, command.args or "", f"message:{message.message_id}"
            )
            await send_schedule_reply(message, result, bot_username)
        except (asyncpg.PostgresError, OSError, TimeoutError):
            await send_parts(message, STORAGE_ERROR)

    @router.callback_query(F.data.startswith("schedule:"))
    async def schedule_button(query: CallbackQuery, schedule_service: ScheduleService):
        await query.answer()
        if not isinstance(query.message, Message) or query.message.chat.id != query.from_user.id:
            return
        try:
            if query.data == "schedule:menu":
                await query.message.answer(
                    "📅 Ваше личное расписание",
                    parse_mode=None,
                    reply_markup=section_keyboard("schedule"),
                )
                return
            elif query.data in {"schedule:put", "schedule:get", "schedule:repeat"}:
                section = query.data.split(":")[1]
                hints = {
                    "put": "Для разового занятия допишите дату, время, название и место.\n\n"
                    "Пример: 2026-11-20 10:00-11:30 | Математика | 301",
                    "get": "Для выбранной даты допишите её в формате ГГГГ-ММ-ДД.",
                    "repeat": "Выберите чётность, затем допишите начало, конец, день недели "
                    "(1=пн…7=вс), дату первой недели, время, название и место.\n\n"
                    "Пример: 2026-11-16 2026-12-28 1 2026-09-07 "
                    "10:00-11:30 | Математика | 301",
                }
                await query.message.answer(
                    hints[section],
                    parse_mode=None,
                    reply_markup=section_keyboard(section, bot_username),
                )
                return
            elif query.data in {
                "schedule:single",
                "schedule:edit_help",
                "schedule:all",
                "schedule:odd",
                "schedule:even",
            }:
                choice = query.data.split(":")[1]
                if choice in {"all", "odd", "even"}:
                    content = (
                        "🔁 Повторяющиеся занятия\n\n"
                        "Начало, конец, день (1=пн..7=вс), чётность, "
                        "дата первой учебной недели, время:\n"
                        f"/schedule repeat 2026-11-02 2026-12-31 1 {choice} 2026-09-01 "
                        "10:00-11:30 | Математика | 301\n\n"
                        "Неделя с указанной датой считается первой (нечётной). "
                        "Укажите свои даты, время и предмет."
                    )
                elif choice == "single":
                    content = (
                        "➕ Добавить пару:\n/schedule add 2026-11-02 10:00-11:30 | Математика | 301"
                    )
                else:
                    content = (
                        "✏️ Номер пары показан при просмотре дня/недели.\n"
                        "/schedule edit 7 2026-11-02 12:00-13:30 | Математика | 305\n"
                        "/schedule delete 7 — одна пара\n/schedule delete_series 7 — вся серия"
                    )
                if choice == "edit_help":
                    keyboard = section_keyboard("edit_schedule", bot_username)
                else:
                    draft = (
                        "/schedule add " if choice == "single" else f"/schedule repeat {choice} "
                    )
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [draft_button("✏️ Ввести команду", draft, bot_username)],
                            [InlineKeyboardButton(text="← Назад", callback_data="schedule:put")],
                        ]
                    )
                await query.message.answer(content, parse_mode=None, reply_markup=keyboard)
                return
            elif query.data in {"schedule:today", "schedule:week"}:
                result = await schedule_service.button(
                    query.from_user.id, query.data.split(":")[1], "callback:" + query.id
                )
            elif query.data in {"schedule:itmo_get", "schedule:itmo_save"}:
                result = await schedule_service.button(
                    query.from_user.id, query.data.split(":")[1], "callback:" + query.id
                )
            else:
                _, operation, identity = query.data.split(":", 2)
                if operation not in {"confirm", "cancel"}:
                    return
                result = await schedule_service.confirm(
                    query.from_user.id, identity, cancel=operation == "cancel"
                )
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            await send_schedule_reply(query.message, result, bot_username)
        except (ValueError, asyncpg.PostgresError, OSError, TimeoutError):
            await send_parts(query.message, STORAGE_ERROR)

    async def issue_login(message, owner, itmo_auth):
        if itmo_auth is None:
            await send_parts(
                message, "Вход через страницу пока не настроен. Инструкция: /schedule account."
            )
            return
        try:
            identity, url = await itmo_auth.issue(owner)
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔐 Открыть вход", url=url)],
                    [
                        InlineKeyboardButton(
                            text="Проверить вход", callback_data=f"itmo:status:{identity}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="Отменить", callback_data=f"itmo:cancel:{identity}"
                        )
                    ],
                ]
            )
            await message.answer(
                "🔐 Подключить ИТМО\n\nОткройте страницу, войдите в ITMO.ID, "
                "затем нажмите «Проверить вход» здесь. Подключение завершится "
                "только после вашего подтверждения аккаунта. Ссылка действует 10 минут. "
                "Не пересылайте её. Пароль в чат не отправляйте.",
                parse_mode=None,
                reply_markup=keyboard,
            )
        except (LoginError, ValueError):
            await send_parts(message, "Слишком много попыток входа. Повторите через 10 минут.")

    @router.message(Command("itmo"))
    async def itmo_command(message: Message, itmo_auth: ITMOAuth | None = None):
        try:
            await issue_login(message, message.from_user.id, itmo_auth)
        except (asyncpg.PostgresError, OSError, TimeoutError):
            await send_parts(message, STORAGE_ERROR)

    @router.callback_query(F.data.startswith("itmo:"))
    async def itmo_button(query: CallbackQuery, itmo_auth: ITMOAuth | None = None):
        await query.answer()
        if not isinstance(query.message, Message) or query.message.chat.id != query.from_user.id:
            return
        try:
            if query.data == "itmo:connect":
                await issue_login(query.message, query.from_user.id, itmo_auth)
                return
            if itmo_auth is None:
                return
            _, operation, identity = query.data.split(":", 2)
            if operation == "status":
                row = await itmo_auth.status(query.from_user.id, identity)
                if row["state"] == "verified":
                    from app.presentation import confirmation_keyboard

                    await query.message.answer(
                        f"Подключить аккаунт ИТМО {row['account']} к вашему личному расписанию?\n"
                        "Текущее подключение, если есть, будет заменено. Пароль не сохраняется.",
                        parse_mode=None,
                        reply_markup=confirmation_keyboard(identity, prefix="itmo"),
                    )
                else:
                    await send_parts(
                        query.message,
                        "Вход ещё не завершён или ссылка уже использована. "
                        "Откройте страницу либо создайте новую ссылку: /itmo.",
                    )
                return
            if operation not in {"confirm", "cancel"}:
                return
            account = await itmo_auth.confirm(
                query.from_user.id, identity, cancel=operation == "cancel"
            )
            await send_parts(
                query.message,
                f"✅ Аккаунт ИТМО {account} подключён. Расписание → Достать → Из ИТМО."
                if account
                else "Подключение ИТМО отменено.",
            )
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        except (LoginError, ValueError):
            await send_parts(
                query.message,
                "Ссылка или подтверждение недействительно. Создайте новую ссылку: /itmo.",
            )
        except (asyncpg.PostgresError, OSError, TimeoutError):
            await send_parts(query.message, STORAGE_ERROR)

    async def respond_text(message, prompt, assistant, agent):
        if not prompt.strip():
            await message.answer("Пришлите непустое текстовое сообщение.", parse_mode=None)
            return
        try:
            async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
                result = (
                    await agent.respond(
                        message.from_user.id, prompt, f"message:{message.message_id}"
                    )
                    if agent
                    else await assistant.respond(message.from_user.id, prompt)
                )
            answer = result.content
            if getattr(result, "pending_id", None):
                from app.presentation import confirmation_keyboard

                await send_with_trace(
                    message,
                    message.answer(
                        answer,
                        parse_mode=None,
                        reply_markup=confirmation_keyboard(result.pending_id),
                    ),
                    agent,
                    result,
                )
                return
        except ContextTooLarge:
            answer = CONTEXT_ERROR
        except LLMError as exc:
            answer = TIMEOUT_TEXT if exc.code == "timeout" else ERROR_TEXT
        except (asyncpg.PostgresError, OSError, TimeoutError):
            logger.warning("storage_failure request=%s", uuid.uuid4())
            answer = STORAGE_ERROR
        await send_with_trace(
            message,
            send_parts(
                message,
                answer,
                format_model=not answer.startswith((SCHEDULE_HEADING, TASKS_HEADING)),
            ),
            agent,
            locals().get("result"),
        )

    @router.message(Command("remind", "weather"))
    async def tool_command(
        message: Message, command: CommandObject, assistant: Assistant, agent: Agent | None = None
    ):
        args = (command.args or "").strip()
        reminder = command.command == "remind"
        if agent is None:
            await send_parts(message, "Инструменты сейчас недоступны.")
            return
        if not args:
            hint = (
                "Допишите время и текст. Пример: /remind завтра в 18:00 подготовить отчёт."
                if reminder
                else "Допишите город. Пример: /weather Санкт-Петербург, Россия."
            )
            await send_parts(message, hint)
            return
        prompt = f"Напомни {args}" if reminder else f"Какая погода в {args}?"
        await respond_text(message, prompt, assistant, agent)

    @router.message(F.text.startswith("/"))
    async def unknown(message: Message):
        await message.answer("Неизвестная команда. Список команд: /start.", parse_mode=None)

    @router.message(F.text)
    async def text(message: Message, assistant: Assistant, agent: Agent | None = None):
        await respond_text(message, message.text, assistant, agent)

    @router.callback_query(F.data.startswith("reminder:"))
    async def reminder(query: CallbackQuery, agent: Agent | None = None):
        await query.answer()
        if (
            agent is None
            or not isinstance(query.message, Message)
            or query.message.chat.id != query.from_user.id
        ):
            return
        try:
            _, operation, identity = query.data.split(":", 2)
            if operation not in {"confirm", "cancel"}:
                return
            result = await agent.confirm(query.from_user.id, identity, cancel=operation == "cancel")
            await send_with_trace(
                query.message, send_parts(query.message, result.content), agent, result
            )
            if result.pending_id is None:
                try:
                    await query.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        except (ValueError, asyncpg.PostgresError, OSError, TimeoutError):
            await send_parts(query.message, STORAGE_ERROR)

    @router.callback_query(F.data.in_({"agent:tools", "agent:why", "agent:timezone"}))
    async def agent_menu(query: CallbackQuery, agent: Agent | None = None):
        await query.answer()
        if (
            agent is None
            or not isinstance(query.message, Message)
            or query.message.chat.id != query.from_user.id
        ):
            return
        try:
            if query.data == "agent:tools":
                content = await agent.tools()
            elif query.data == "agent:why":
                content = await agent.why(query.from_user.id)
            else:
                content = await agent.timezone(query.from_user.id)
                content += "\n\nИзменить: /timezone Europe/Moscow"
            await query.message.answer(
                content,
                parse_mode=None,
                reply_markup=(
                    settings_keyboard(bot_username)
                    if query.data == "agent:timezone"
                    else section_keyboard("tools", bot_username)
                ),
            )
        except (asyncpg.PostgresError, OSError, TimeoutError):
            await send_parts(query.message, STORAGE_ERROR)

    @router.callback_query(F.data.in_({"tool:weather", "tool:reminder"}))
    async def tool_help(query: CallbackQuery):
        await query.answer()
        if not isinstance(query.message, Message) or query.message.chat.id != query.from_user.id:
            return
        content = (
            "🌦 Напишите: Какая погода в Санкт-Петербурге, Россия?"
            if query.data == "tool:weather"
            else "🔔 Напишите: Напомни завтра в 18:00 подготовить отчёт.\n"
            "Сначала задайте зону в настройках. Создание потребует подтверждения. "
            "Подтверждённое напоминание будет отправлено в этот личный чат в назначенное время."
        )
        command = "/weather " if query.data == "tool:weather" else "/remind "
        await query.message.answer(
            content,
            parse_mode=None,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [draft_button("✏️ Ввести команду", command, bot_username)],
                    [InlineKeyboardButton(text="← Инструменты", callback_data="agent:tools")],
                ]
            ),
        )

    @router.callback_query(
        F.data.in_({"menu", "settings", "reset"})
        | F.data.startswith("mode:")
        | F.data.startswith("temperature:")
    )
    async def button(query: CallbackQuery, assistant: Assistant):
        await query.answer()
        if not isinstance(query.message, Message) or query.message.chat.id != query.from_user.id:
            return
        if query.data == "menu":
            await query.message.answer(START_TEXT, parse_mode="HTML", reply_markup=main_keyboard())
        elif query.data == "settings":
            await perform(
                query.message,
                assistant.preferences(query.from_user.id),
                settings=True,
                bot_username=bot_username,
            )
        elif query.data == "reset":
            await perform(
                query.message,
                assistant.reset(query.from_user.id),
                settings=True,
                bot_username=bot_username,
            )
        elif query.data.startswith("temperature:"):
            await perform(
                query.message,
                assistant.preferences(query.from_user.id, query.data.split(":", 1)[1]),
                settings=True,
                bot_username=bot_username,
            )
        elif query.data.split(":", 1)[1] in {"study", "translate", "review"}:
            await perform(
                query.message, assistant.mode(query.from_user.id, query.data.split(":", 1)[1])
            )

    return router


async def send_schedule_reply(message, result, bot_username=None):
    if result.pending_id:
        from app.presentation import confirmation_keyboard

        parts = split_telegram(result.content)
        for index, part in enumerate(parts):
            await message.answer(
                part,
                parse_mode=None,
                reply_markup=(
                    confirmation_keyboard(result.pending_id, prefix="schedule")
                    if index == len(parts) - 1
                    else None
                ),
            )
    else:
        for part in split_telegram(result.content):
            await message.answer(
                part, parse_mode=None, reply_markup=section_keyboard("schedule", bot_username)
            )


async def send_with_trace(message, operation, agent, result):
    started = time.monotonic()
    status = "success"
    try:
        return await operation
    except BaseException:
        status = "failed"
        raise
    finally:
        if getattr(result, "trace_id", None):
            await agent.delivered(
                message.chat.id, result.trace_id, int((time.monotonic() - started) * 1000), status
            )


async def send_parts(
    message: Message, text: str, *, format_model: bool = False, keyboard=None
) -> None:
    if format_model:
        parts = formatted_parts(text)
        for index, part in enumerate(parts):
            await message.answer(
                part.text,
                entities=part.entities,
                parse_mode=None,
                reply_markup=main_keyboard() if index == len(parts) - 1 else None,
            )
    else:
        for part in split_telegram(text):
            await message.answer(part, parse_mode=None, reply_markup=keyboard or main_keyboard())


async def perform(
    message: Message, operation, *, settings: bool = False, bot_username=None
) -> None:
    try:
        answer = await operation
    except (asyncpg.PostgresError, OSError, TimeoutError):
        logger.warning("storage_failure request=%s", uuid.uuid4())
        answer = STORAGE_ERROR
    title = "⚙️ Настройки" if settings else "✓ Готово"
    await message.answer(
        f"<b>{title}</b>\n\n{escape(answer)}",
        parse_mode="HTML",
        reply_markup=settings_keyboard(bot_username) if settings else main_keyboard(),
    )
