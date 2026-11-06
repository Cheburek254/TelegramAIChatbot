"""Личная панель Telegram. Доступ только по проверенным данным запуска."""

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

from app.itmo import ITMOError
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
    def __init__(self, public_url, bot_token, schedule, planner, notifications, *, clock=None):
        self.public_url = public_url
        self.secret = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
        self.schedule, self.planner, self.notifications = schedule, planner, notifications
        self.clock = clock or (lambda: datetime.now(UTC))

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
        if not isinstance(data, dict) or set(data) != {"init_data", "view", "offset"}:
            raise web.HTTPBadRequest()
        view, offset = data["view"], data["offset"]
        if (
            view not in ("schedule", "tasks", "reminders")
            or type(offset) is not int
            or not 0 <= offset <= 6000
            or offset % PAGE_SIZE
            or (view == "schedule" and offset)
        ):
            raise web.HTTPBadRequest()
        now = self.clock()
        try:
            owner = telegram_owner(data["init_data"], self.secret, now)
        except (ValueError, RecursionError):
            raise web.HTTPUnauthorized() from None
        if view == "schedule":
            result = await self.lessons(owner, now)
        else:
            result = await self.records(owner, view, offset)
        return web.json_response(result)

    async def lessons(self, owner, now):
        itmo = self.schedule.itmo
        warning = None
        if itmo and itmo.connected(owner):
            timezone = "Europe/Moscow"
            first = now.astimezone(zone(timezone)).date()
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
                }
        timezone = await self.schedule.agent_store.timezone(owner) or self.schedule.default_timezone
        first = now.astimezone(zone(timezone)).date()
        rows = await self.schedule.store.entries(owner, first, first + timedelta(days=6))
        return {
            "items": [lesson_payload(row) for row in rows],
            "source": "Личное расписание",
            "timezone": timezone,
            "warning": warning,
            "more": False,
        }

    async def records(self, owner, view, offset):
        kind, timing = ("t", "due_at") if view == "tasks" else ("r", "remind_at")
        rows = await self.planner.active(owner, kind, offset)
        items = []
        if kind == "r" and offset == 0:
            for row in await self.notifications.daily_reminders(owner):
                minute = row["minute"]
                items.append(
                    {
                        "text": row["text"],
                        "when": f"{minute // 60:02d}:{minute % 60:02d}",
                        "timezone": row["timezone"],
                        "repeat": 1,
                    }
                )
        for row in rows[:PAGE_SIZE]:
            when = row[timing]
            local = when.astimezone(zone(row["timezone"])) if when else None
            items.append(
                {
                    "text": row["text"],
                    "when": local.isoformat() if local else None,
                    "timezone": row["timezone"],
                    "repeat": row.get("repeat_days"),
                }
            )
        return {"items": items, "more": len(rows) > PAGE_SIZE, "next_offset": offset + PAGE_SIZE}
