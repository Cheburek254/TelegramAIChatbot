import argparse
import asyncio
import logging
import secrets

import aiohttp
from aiogram import Dispatcher
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, MenuButtonWebApp, WebAppInfo

from app.agent import Agent
from app.agent_store import AgentStore
from app.assistant import Assistant
from app.config import ConfigError, Settings
from app.db import OwnerMiddleware, create_pool
from app.handlers.assistant import create_router
from app.handlers.features import ContactMiddleware, create_feature_router
from app.handlers.itmo_watch import create_itmo_watch_router
from app.handlers.planner import create_planner_router
from app.health import HealthState, start_health_server
from app.itmo import ITMOClient
from app.itmo_auth import ITMOAuth
from app.itmo_auth_store import AuthStore
from app.itmo_auth_web import start_auth_server
from app.itmo_watch import ItmoWatch
from app.itmo_watch_store import ItmoWatchStore
from app.llm import LLMClient
from app.logging_setup import configure_logging
from app.material_service import PersonalMaterials
from app.material_store import MaterialStore
from app.mcp_client import MCPBridge
from app.mcp_service.weather import Weather
from app.migrations import migrate
from app.miniapp import MiniApp
from app.orchestration.geography import Geography
from app.orchestration.notifications import Notifications
from app.orchestration.scheduler import Scheduler
from app.orchestration.store import AssistantStore
from app.orchestration.supervisor import Supervisor
from app.planner import Planner
from app.planner_store import PlannerStore
from app.repository import PostgresRepository
from app.schedule_service import ScheduleService
from app.schedule_store import ScheduleStore
from app.speech_style import SpeechStyles
from app.study_planning import StudyPlanning
from app.style_store import SpeechStyleStore
from app.system_speech import SystemSpeechMiddleware
from app.telegram import create_bot

logger = logging.getLogger("app")


async def run(settings: Settings) -> None:
    state = HealthState()
    bot = create_bot(settings)
    runner = None
    auth_runner = None
    itmo_auth = None
    llm_session = None
    bridge = None
    agent = None
    scheduler_task = None
    scheduler = None
    notifications = None
    itmo_task = None
    miniapp_url = None
    try:
        state.pool = await create_pool(settings)
        logger.info("PostgreSQL подключён: SELECT 1 выполнен.")
        # Начальная проверка токена и маршрута через прокси ограничена по времени.
        async with asyncio.timeout(30):
            me = await bot.get_me()
            webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ConfigError(
                "У бота установлен webhook. Удалите его перед запуском polling "
                "или используйте отдельного учебного бота."
            )
        logger.info("Telegram доступен. Бот @%s запускает polling.", me.username)
        if not settings.postgres_rls:
            await migrate(state.pool)
        style_store = SpeechStyleStore(state.pool)
        bot.session.middleware(SystemSpeechMiddleware(style_store))
        llm_session = aiohttp.ClientSession(trust_env=False)
        itmo = ITMOClient(settings.itmo_state_dir, settings.itmo_vault_key_file)
        await itmo.migrate_legacy()
        itmo_task = asyncio.create_task(itmo.maintenance())
        schedule_service = ScheduleService(
            ScheduleStore(state.pool),
            AgentStore(state.pool),
            timezone=settings.mcp_resource_timezone,
            itmo=itmo,
        )
        store = AssistantStore(state.pool)
        planner_store = PlannerStore(state.pool)
        geography = Geography(Weather(llm_session))
        if settings.itmo_auth_public_url:
            from urllib.parse import urlsplit

            url = urlsplit(settings.itmo_auth_public_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path
            ):
                raise ConfigError("ITMO_AUTH_PUBLIC_URL: требуется HTTPS origin без пути.")
            itmo_auth = ITMOAuth(
                AuthStore(state.pool), schedule_service.itmo, settings.itmo_auth_public_url
            )
            miniapp_url = settings.itmo_auth_public_url + "/miniapp/"
            miniapp = MiniApp(
                settings.itmo_auth_public_url,
                settings.bot_token,
                schedule_service,
                planner_store,
                store,
                geography=geography,
                itmo_auth=itmo_auth,
            )
            auth_runner = await start_auth_server(
                itmo_auth, settings.itmo_auth_port, miniapp=miniapp
            )
        assistant = Assistant(
            settings, PostgresRepository(state.pool), LLMClient(settings, llm_session)
        )
        if settings.mcp_enabled:
            bridge = MCPBridge(settings, secrets.token_urlsafe(32))
            await bridge.start()
            agent = Agent(settings, AgentStore(state.pool), assistant.llm, bridge)
            if settings.supervisor_enabled:
                agent = Supervisor(settings, store, assistant, bridge)
                notifications = Notifications(store, geography)
                watch_store = ItmoWatchStore(state.pool)
                scheduler = Scheduler(
                    store,
                    bridge,
                    bot,
                    interval=settings.scheduler_interval,
                    briefing_timeout=settings.briefing_timeout,
                    itmo_watch=ItmoWatch(watch_store, schedule_service.itmo),
                )
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command=name, description=description)
                    for name, description in [
                        ("start", "Возможности и команды"),
                        *([("app", "Открыть учебный план")] if miniapp_url else []),
                        ("study", "Учебный помощник"),
                        ("translate", "Переводчик"),
                        ("review", "Ревью кода"),
                        ("settings", "Режим, модель и temperature"),
                        ("reset", "Очистить историю"),
                        ("tools", "Инструменты агента"),
                        ("timezone", "Часовой пояс напоминаний"),
                        ("why", "Последнее решение агента"),
                        ("schedule", "Моё расписание: просмотр и изменение"),
                        ("itmo", "Подключить аккаунт ИТМО"),
                        ("materials", "Каталог учебных материалов"),
                        ("city", "Город для утренней погоды"),
                        ("briefing_on", "Включить ежедневную сводку"),
                        ("briefing_off", "Выключить ежедневную сводку"),
                        ("briefing_time", "Время ежедневной сводки"),
                        ("notifications", "Настройки уведомлений"),
                        ("daily_reminders", "Мои ежедневные напоминания"),
                        ("reminders", "Мои разовые напоминания"),
                        ("remind", "Создать разовое напоминание"),
                        ("weather", "Погода в выбранном городе"),
                        ("tasks", "Мои задачи и дедлайны"),
                        ("free", "Найти свободное время между парами"),
                        ("itmo_alerts", "Уведомления об изменениях расписания ИТМО"),
                        ("trace", "Путь обработки последнего запроса"),
                    ]
                ],
                scope=BotCommandScopeAllPrivateChats(),
            )
        except Exception:
            logger.warning("Не удалось обновить меню команд Telegram.")
        if miniapp_url:
            try:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="Мой план", web_app=WebAppInfo(url=miniapp_url)
                    )
                )
            except Exception:
                logger.warning("Не удалось установить кнопку Mini App.")
        dispatcher = Dispatcher()
        dispatcher.message.outer_middleware(OwnerMiddleware())
        dispatcher.callback_query.outer_middleware(OwnerMiddleware())
        if notifications:
            dispatcher.message.outer_middleware(ContactMiddleware(store))
            dispatcher.callback_query.outer_middleware(ContactMiddleware(store))
            planner = Planner(planner_store, notifications, settings.mcp_resource_timezone)
            dispatcher.include_router(
                create_planner_router(
                    planner,
                    study=StudyPlanning(
                        planner,
                        schedule_service.store,
                        schedule_service.itmo,
                        assistant.llm,
                        settings.history_max_tokens,
                    ),
                    bot_username=me.username,
                )
            )
            dispatcher.include_router(create_itmo_watch_router(watch_store, schedule_service.itmo))
            dispatcher.include_router(
                create_feature_router(
                    notifications,
                    agent,
                    PersonalMaterials(MaterialStore(state.pool)),
                    bot_username=me.username,
                )
            )
        dispatcher.include_router(create_router(bot_username=me.username, miniapp_url=miniapp_url))
        runner = await start_health_server(state, settings.health_port)
        state.polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                db=state.pool,
                assistant=assistant,
                agent=agent,
                schedule_service=schedule_service,
                itmo_auth=itmo_auth,
                speech_styles=SpeechStyles(style_store, assistant.repository),
                allowed_updates=dispatcher.resolve_used_update_types(),
                close_bot_session=False,
            )
        )
        state.initialized = True
        if scheduler:
            scheduler_task = asyncio.create_task(scheduler.run())
            state.background_task = scheduler_task
        await state.polling_task
    finally:
        state.initialized = False
        if itmo_task:
            itmo_task.cancel()
            await asyncio.gather(itmo_task, return_exceptions=True)
        if scheduler_task:
            scheduler_task.cancel()
            await asyncio.gather(scheduler_task, return_exceptions=True)
        if state.polling_task and not state.polling_task.done():
            state.polling_task.cancel()
            await asyncio.gather(state.polling_task, return_exceptions=True)
        if runner:
            await runner.cleanup()
        if auth_runner:
            await auth_runner.cleanup()
        if bridge:
            await bridge.close()
        await bot.session.close()
        if llm_session:
            await llm_session.close()
        if state.pool:
            try:
                async with asyncio.timeout(10):
                    await state.pool.close()
            except TimeoutError:
                state.pool.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram AI Chatbot")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    try:
        settings = Settings.load(args.env_file)
        settings.validate_llm()
    except ConfigError as exc:
        print(str(exc))
        return 1
    configure_logging(settings)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    except Exception:
        logger.exception("Не удалось запустить бот. Проверьте БД, токен и прокси.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
