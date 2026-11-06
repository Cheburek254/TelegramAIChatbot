"""Личная панель Telegram. Доступ только по проверенным данным запуска."""

import asyncio
import hashlib
import hmac
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from app.itmo import ITMOError
from app.itmo_auth_web_messages import login_message
from app.itmo_login import LoginError
from app.mcp_service.weather import ToolFailure
from app.miniapp_changes import FormError, MiniAppChanges
from app.planner_store import PAGE_SIZE
from app.schedule_store import lesson_payload
from app.timezones import zone

ASSETS = Path("assets/miniapp")
ASSET_TYPES = {"index.html": "text/html", "app.js": "text/javascript", "app.css": "text/css"}
MINIAPP_CSP = (
    "default-src 'none'; script-src 'self' https://telegram.org/js/telegram-web-app.js; "
    "style-src 'self'; connect-src 'self'; frame-ancestors https://web.telegram.org; "
    "form-action 'none'; base-uri 'none'"
)


def telegram_owner(raw, secret, now):
    """Check Telegram's HMAC and freshness before looking at user data."""
    if not isinstance(raw, str) or not 1 <= len(raw) <= 4096:
        raise ValueError
    pairs = parse_qsl(
        raw,
        keep_blank_values=True,
        strict_parsing=True,
        max_num_fields=24,
        encoding="utf-8",
        errors="strict",
    )
    fields = dict(pairs)
    digest = fields.pop("hash", "")
    if len(fields) + 1 != len(pairs) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError
    if any(not re.fullmatch(r"[a-z_]+", key) or "\n" in value for key, value in fields.items()):
        raise ValueError
    data = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    if not hmac.compare_digest(hmac.new(secret, data.encode(), hashlib.sha256).hexdigest(), digest):
        raise ValueError
    auth_date = fields.get("auth_date", "")
    if (
        not re.fullmatch(r"[0-9]{1,12}", auth_date)
        or not -30 <= now.timestamp() - int(auth_date) <= 1800
    ):
        raise ValueError
    user = json.loads(fields.get("user", "null"))
    owner = user.get("id") if isinstance(user, dict) else None
    if type(owner) is not int or not 0 < owner < 2**52:
        raise ValueError
    return owner


class MiniApp:
    def __init__(
        self,
        public_url,
        bot_token,
        schedule,
        planner,
        notifications,
        *,
        geography=None,
        itmo_auth=None,
        clock=None,
    ):
        self.public_url = public_url
        self.secret = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
        self.schedule, self.planner, self.notifications = schedule, planner, notifications
        self.clock = clock or (lambda: datetime.now(UTC))
        self.changes = MiniAppChanges(schedule, planner, notifications, self.clock)
        self.geography, self.itmo_auth = geography, itmo_auth

    def register(self, app):
        app.router.add_get("/miniapp/", self.asset)
        app.router.add_get("/miniapp/{name:app[.](?:js|css)}", self.asset)
        app.router.add_post("/miniapp/api", self.api)

    async def asset(self, request):
        name = request.match_info.get("name", "index.html")
        return web.Response(
            body=await asyncio.to_thread(ASSETS.joinpath(name).read_bytes),
            content_type=ASSET_TYPES[name],
            charset="utf-8",
        )

    async def api(self, request):
        if request.headers.get("Origin") != self.public_url:
            raise web.HTTPForbidden()
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        try:
            data = await request.json()
        except (ValueError, RecursionError):
            raise web.HTTPBadRequest() from None
        if not isinstance(data, dict) or "init_data" not in data:
            raise web.HTTPBadRequest()
        now = self.clock()
        try:
            owner = telegram_owner(data["init_data"], self.secret, now)
        except (ValueError, RecursionError):
            raise web.HTTPUnauthorized() from None
        if "action" in data:
            return await self.action(owner, data, now)
        if set(data) != {"init_data", "view", "offset"}:
            raise web.HTTPBadRequest()
        view, offset = data["view"], data["offset"]
        if (
            view not in ("schedule", "personal_schedule", "tasks", "reminders", "weather")
            or type(offset) is not int
            or not (
                0 <= offset <= 364 and offset % 7 == 0
                if view in ("schedule", "personal_schedule")
                else 0 <= offset <= 6000 and offset % PAGE_SIZE == 0
            )
            or (view == "weather" and offset)
        ):
            raise web.HTTPBadRequest()
        if view in ("schedule", "personal_schedule"):
            result = await self.lessons(owner, now, offset, personal=view == "personal_schedule")
        elif view == "weather":
            result = await self.weather(owner)
        else:
            result = await self.records(owner, view, offset)
        return web.json_response(result)

    async def action(self, owner, data, now):
        fields = set(data) - {"init_data", "action"}
        action = data["action"]
        try:
            if action == "prepare" and fields == {"request_id", "change"}:
                domain, pending, message = await self.changes.prepare(
                    owner, data["request_id"], data["change"], now
                )
                if not pending:
                    raise FormError(message)
                return web.json_response(
                    {"domain": domain, "pending": str(pending), "message": message}
                )
            if action == "confirm" and fields == {"domain", "pending"}:
                success, message = await self.changes.confirm(
                    owner, data["domain"], uuid.UUID(data["pending"]), now
                )
                return web.json_response({"message": message}, status=200 if success else 409)
            if self.itmo_auth:
                if action == "itmo_issue" and not fields:
                    identity, ticket = await self.itmo_auth.issue_ticket(owner)
                    return web.json_response({"id": str(identity), "ticket": ticket})
                if action == "itmo_sign_in" and fields == {"ticket", "username", "password"}:
                    async with asyncio.timeout(20):
                        account = await self.itmo_auth.sign_in(
                            data["ticket"], data["username"], data["password"], owner=owner
                        )
                    return web.json_response({"account": account})
                if action == "itmo_confirm" and fields == {"id"}:
                    await self.itmo_auth.confirm(owner, uuid.UUID(data["id"]))
                    return web.json_response({"message": "Аккаунт ИТМО подключён."})
        except FormError as exc:
            return web.json_response({"message": str(exc)}, status=400)
        except LoginError as exc:
            return web.json_response({"message": login_message(str(exc))}, status=400)
        except TimeoutError:
            return web.json_response({"message": "ИТМО не ответил. Повторите попытку."}, status=503)
        except (AttributeError, TypeError, ValueError):
            raise web.HTTPBadRequest() from None
        raise web.HTTPBadRequest()

    async def lessons(self, owner, now, offset=0, *, personal=False):
        itmo = self.schedule.itmo
        connected = bool(itmo and itmo.connected(owner))
        warning = None
        if connected and not personal:
            timezone = "Europe/Moscow"
            first = now.astimezone(zone(timezone)).date() + timedelta(days=offset)
            try:
                async with asyncio.timeout(15):
                    entries = await itmo.fetch(owner, first, first + timedelta(days=6))
            except (ITMOError, OSError, TimeoutError):
                warning = "ИТМО сейчас недоступен. Показано сохранённое личное расписание."
            else:
                return {
                    "items": sorted(entries, key=lambda item: (item["date"], item["start"])),
                    "source": "Актуальное расписание ИТМО",
                    "timezone": timezone,
                    "more": False,
                    "first": first.isoformat(),
                    "connected": connected,
                }
        timezone = await self.schedule.agent_store.timezone(owner) or self.schedule.default_timezone
        first = now.astimezone(zone(timezone)).date() + timedelta(days=offset)
        rows = await self.schedule.store.entries(owner, first, first + timedelta(days=6))
        return {
            "items": [
                {**lesson_payload(row), "id": str(row["id"]), "version": row["version"]}
                for row in rows
            ],
            "source": "Личное расписание",
            "timezone": timezone,
            "warning": warning,
            "more": False,
            "first": first.isoformat(),
            "connected": connected,
        }

    @staticmethod
    def record_payload(row, kind):
        if kind == "d":
            minute = row["minute"]
            when = f"{minute // 60:02d}:{minute % 60:02d}"
        else:
            instant = row["due_at" if kind == "t" else "remind_at"]
            when = instant.astimezone(zone(row["timezone"])).isoformat() if instant else None
        return {
            "id": str(row["id"]),
            "version": row["version"],
            "kind": kind,
            "text": row["text"],
            "when": when,
            "timezone": row["timezone"],
            "repeat": 1 if kind == "d" else row.get("repeat_days"),
            "lead": row.get("notify_before"),
        }

    async def records(self, owner, view, offset):
        kind = "t" if view == "tasks" else "r"
        rows = await self.planner.active(owner, kind, offset)
        items = [self.record_payload(row, kind) for row in rows[:PAGE_SIZE]]
        result = {"items": items, "more": len(rows) > PAGE_SIZE, "next_offset": offset + PAGE_SIZE}
        if kind == "r" and offset == 0:
            daily = await self.notifications.daily_reminders(owner)
            items[:0] = [self.record_payload(row, "d") for row in daily]
        if kind == "t" and offset == 0:
            done = await self.planner.history(owner)
            result["completed"] = [self.record_payload(row, kind) for row in done[:PAGE_SIZE]]
        return result

    async def weather(self, owner):
        profile = await self.notifications.profile(owner)
        city = profile and profile.get("city")
        default_city = not city
        if not city:
            city = {
                "name": "Санкт-Петербург",
                "country": "Россия",
                "region": "Санкт-Петербург",
                "latitude": 59.9386,
                "longitude": 30.3141,
            }
        elif isinstance(city, str):
            city = json.loads(city)
        try:
            async with asyncio.timeout(12):
                current = await self.geography.current(city)
        except (ToolFailure, TimeoutError, OSError):
            return {"items": [], "warning": "Погода временно недоступна."}
        return {
            "items": [],
            "weather": current.model_dump(mode="json"),
            "default_city": default_city,
        }
