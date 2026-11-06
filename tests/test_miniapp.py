import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User
from aiohttp.test_utils import TestClient, TestServer
from cryptography.fernet import Fernet

from app.handlers.assistant import create_router
from app.itmo_auth_web import create_auth_app
from app.miniapp import MiniApp

ORIGIN = "https://study.example"
TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"
NOW = datetime(2026, 11, 6, 10, tzinfo=UTC)


def signed(owner, date=NOW):
    data = {
        "auth_date": str(int(date.timestamp())),
        "query_id": "test_query",
        "signature": "test-signature",
        "user": json.dumps({"id": owner, "first_name": "Тест"}, ensure_ascii=False),
    }
    message = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    return urlencode({**data, "hash": hmac.digest(secret, message.encode(), "sha256").hex()})


async def test_miniapp_only_reads_for_verified_owner_and_serves_private_routes():
    schedule = Mock(
        itmo=Mock(
            status=lambda owner: {
                "connected": owner == 101,
                "status": "active" if owner == 101 else "never",
                "days": 30,
            },
            display=AsyncMock(side_effect=lambda owner, *_: ([] if owner == 101 else None, None)),
        ),
        agent_store=Mock(timezone=AsyncMock(return_value=None)),
        store=Mock(entries=AsyncMock(return_value=[])),
        default_timezone="Europe/Moscow",
    )
    planner = Mock(
        active=AsyncMock(
            return_value=[
                {
                    "id": uuid.uuid4(),
                    "version": 1,
                    "text": "Report",
                    "due_at": NOW,
                    "timezone": "Europe/Moscow",
                    "repeat_days": 7,
                }
            ]
        ),
        history=AsyncMock(return_value=[]),
    )
    notifications = Mock(
        daily_reminders=AsyncMock(
            return_value=[
                {
                    "id": uuid.uuid4(),
                    "version": 1,
                    "text": "Read",
                    "minute": 1140,
                    "timezone": "Europe/Moscow",
                }
            ]
        )
    )
    miniapp = MiniApp(ORIGIN, TOKEN, schedule, planner, notifications, clock=lambda: NOW)
    auth = Mock(public_url=ORIGIN, cipher=Fernet(Fernet.generate_key()))
    async with TestClient(TestServer(create_auth_app(auth, miniapp=miniapp))) as client:
        for path in ("", "app.js", "app.css"):
            page = await client.get("/miniapp/" + path)
            assert page.status == 200
            assert page.headers["Cache-Control"] == "no-store"
            assert "https://web.telegram.org" in page.headers["Content-Security-Policy"]
            assert "X-Frame-Options" not in page.headers
        assert (await client.get("/itmo/login")).headers["X-Frame-Options"] == "DENY"

        async def request(raw, view="tasks", *, origin=ORIGIN, **extra):
            return await client.post(
                "/miniapp/api",
                headers={"Origin": origin},
                json={"init_data": raw, "view": view, "offset": 0, **extra},
            )

        original = signed(101)
        assert (await request(original, origin="https://evil.example")).status == 403
        assert (await request(original, owner=102)).status == 400
        assert (await request(original.replace("101", "102", 1))).status == 401
        assert (await request(original + "&auth_date=1")).status == 401
        assert (await request(signed(101, NOW - timedelta(minutes=31)))).status == 401
        assert (await request(signed(101, NOW + timedelta(minutes=1)))).status == 401
        planner.active.assert_not_called()

        tasks = await request(original)
        assert tasks.status == 200
        assert (await tasks.json())["items"][0]["when"] == "2026-11-06T13:00:00+03:00"
        planner.active.assert_awaited_once_with(101, "t", 0)
        official = await request(original, "schedule")
        assert "ИТМО" in (await official.json())["source"]
        schedule.itmo.display.assert_awaited_once_with(
            101, NOW.date(), NOW.date() + timedelta(days=6)
        )
        local = await request(signed(102), "schedule")
        assert (await local.json())["source"] == "Личное расписание"
        schedule.store.entries.assert_awaited_once_with(
            102, NOW.date(), NOW.date() + timedelta(days=6)
        )
        planner.active.return_value = []
        reminders = await request(signed(102), "reminders")
        assert (await reminders.json())["items"][0]["when"] == "19:00"
        notifications.daily_reminders.assert_awaited_once_with(102)


async def test_miniapp_itmo_login_and_weather_use_signed_owner():
    ticket_id = uuid.uuid4()
    auth = Mock(
        public_url=ORIGIN,
        cipher=Fernet(Fernet.generate_key()),
        issue_ticket=AsyncMock(return_value=(ticket_id, "synthetic-ticket")),
        sign_in=AsyncMock(return_value="fixture-account"),
        confirm=AsyncMock(),
        propose_control=AsyncMock(return_value="synthetic-confirmation"),
        apply_control=AsyncMock(return_value="saved"),
    )
    profiles = Mock(profile=AsyncMock(return_value={"city": None}))
    geography = Mock(current=AsyncMock(return_value=Mock(model_dump=lambda **_: {"status": "ok"})))
    miniapp = MiniApp(
        ORIGIN,
        TOKEN,
        Mock(),
        Mock(),
        profiles,
        geography=geography,
        itmo_auth=auth,
        clock=lambda: NOW,
    )
    async with TestClient(TestServer(create_auth_app(auth, miniapp=miniapp))) as client:

        async def request(init_data, **payload):
            return await client.post(
                "/miniapp/api", headers={"Origin": ORIGIN}, json={"init_data": init_data, **payload}
            )

        user = signed(101)
        assert (await request("invalid", action="itmo_issue")).status == 401
        auth.issue_ticket.assert_not_called()
        issue = await request(user, action="itmo_issue")
        assert await issue.json() == {"id": str(ticket_id), "ticket": "synthetic-ticket"}
        result = await request(
            user,
            action="itmo_sign_in",
            ticket="synthetic-ticket",
            username="fixture-login",
            password="fixture-password",
        )
        assert await result.json() == {"account": "fixture-account"}
        auth.sign_in.assert_awaited_once_with(
            "synthetic-ticket", "fixture-login", "fixture-password", owner=101
        )
        auth.confirm.assert_not_called()
        assert (await request(user, action="itmo_confirm", id=str(ticket_id))).status == 200
        auth.confirm.assert_awaited_once_with(101, ticket_id)
        confirmation = await request(user, action="itmo_prepare", value="off")
        assert "украденный токен" in (await confirmation.json())["message"]
        auth.propose_control.assert_awaited_once_with(101, "off")
        auth.apply_control.assert_not_called()
        result = await request(user, action="itmo_apply", pending="synthetic-confirmation")
        assert result.status == 200
        auth.apply_control.assert_awaited_once_with(101, "synthetic-confirmation")
        weather = await request(user, view="weather", offset=0)
        assert (await weather.json())["default_city"] is True
        assert geography.current.await_args.args[0]["name"] == "Санкт-Петербург"


async def test_app_command_and_start_use_telegram_web_app_button():
    dispatcher = Dispatcher()
    url = ORIGIN + "/miniapp/"
    dispatcher.include_router(create_router(miniapp_url=url))
    bot = Bot(TOKEN)
    bot.session = AsyncMock()
    for index, command in enumerate(("/app", "/start"), 1):
        message = Message(
            message_id=index,
            date=NOW,
            chat=Chat(id=101, type="private"),
            from_user=User(id=101, is_bot=False, first_name="Test"),
            text=command,
        )
        await dispatcher.feed_update(bot, Update(update_id=index, message=message))
        markup = bot.session.call_args.args[1].reply_markup
        assert markup.inline_keyboard[0][0].web_app.url == url
