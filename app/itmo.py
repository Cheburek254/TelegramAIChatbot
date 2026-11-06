"""Изолированная сессия itmo-mcp для каждого Telegram-владельца, только чтение расписания."""

import asyncio
import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import aiohttp
from cryptography.fernet import InvalidToken
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.itmo_login import CLIENT, ISSUER
from app.itmo_vault import (
    NO_CACHE_WARNING,
    PARTIAL_WARNING,
    STALE_WARNING,
    SessionVault,
    atomic_write,
)
from app.schedule_contracts import ScheduleLesson
from app.timezones import zone

CLI = "/opt/itmo/node_modules/itmo-mcp/dist/cli.js"
REVOKE = ISSUER + "/protocol/openid-connect/revoke"


class ITMOError(ValueError):
    pass


class ITMOClient:
    def __init__(self, state_dir, key_file=None, *, clock=None, revoke=None):
        self.state_dir = Path(state_dir).resolve()
        self.vault = SessionVault(self.state_dir, key_file)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.revoke = revoke or self._revoke
        self.locks = {}

    def expired(self, state):
        days = state["days"]
        return days is not None and self.clock().timestamp() >= state["started"] + days * 86400

    def status(self, owner):
        try:
            state = self.vault.load(owner)
        except (InvalidToken, OSError, ValueError):
            return {"connected": False, "status": "unavailable", "days": None}
        status = state["status"]
        if status == "active" and self.expired(state):
            status = "expired"
        return {"connected": status == "active", "status": status, "days": state["days"]}

    def connected(self, owner):
        return self.status(owner)["connected"]

    @asynccontextmanager
    async def session_lock(self, owner):
        directory = self.vault.directory(owner)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        async with self.locks.setdefault(owner, asyncio.Lock()):
            descriptor = None
            try:
                if os.name == "posix":
                    import fcntl

                    descriptor = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
                    async with asyncio.timeout(45):
                        while True:
                            try:
                                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                await asyncio.sleep(0.1)
                yield
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def state(self, owner):
        state = self.vault.load(owner)
        if (self.vault.directory(owner) / "state.json").exists():
            self.vault.save(owner, state)
        return state

    async def install(self, owner, refresh, identity, *, replace=True, bot_issued=True):
        if (
            not isinstance(refresh, str)
            or not 20 <= len(refresh) <= 16000
            or any(char.isspace() for char in refresh)
        ):
            raise ITMOError("invalid_token")
        state = self.state(owner)
        if self.installed(state, identity):
            return
        if state["sdk"]:
            if not replace:
                raise ITMOError("already_connected_use_replace")
            if not await self.disconnect(owner, state):
                raise ITMOError("revoke_pending")
        state.update(
            sdk={"refreshToken": refresh},
            started=self.clock().timestamp(),
            status="active",
            login=str(identity),
            primed_on=None,
            bot_issued=bot_issued,
        )
        state.pop("prime_retry", None)
        (self.vault.directory(owner) / "schedule-cache.enc").unlink(missing_ok=True)
        self.vault.save(owner, state)

    @staticmethod
    def installed(state, identity):
        return (state.get("login") or (state["sdk"] or {}).get("loginTicket")) == str(identity)

    @staticmethod
    async def _revoke(refresh):
        try:
            async with (
                aiohttp.ClientSession(
                    trust_env=False, timeout=aiohttp.ClientTimeout(total=8)
                ) as session,
                session.post(
                    REVOKE,
                    data={
                        "token": refresh,
                        "token_type_hint": "refresh_token",
                        "client_id": CLIENT,
                    },
                    allow_redirects=False,
                ) as response,
            ):
                return response.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return False

    async def disconnect(self, owner, state):
        state["status"] = "revoking"
        state["retry_at"] = self.clock().timestamp() + 60
        self.vault.save(owner, state)
        refresh = (state["sdk"] or {}).get("refreshToken")
        if refresh and state.get("bot_issued") and not await self.revoke(refresh):
            return False
        state.update(sdk=None, status="disconnected")
        self.vault.save(owner, state)
        return True

    async def apply_control(self, owner, value):
        async with self.session_lock(owner):
            state = self.state(owner)
            if value == "disconnect":
                return await self.apply_disconnect(owner, state)
            state["days"] = None if value == "off" else value
            self.vault.save(owner, state)
            if state["status"] == "active" and self.expired(state):
                return await self.apply_disconnect(owner, state)
            return "saved"

    async def apply_disconnect(self, owner, state):
        if state["sdk"] is None:
            return "already_disconnected"
        copied = bool(state["sdk"]) and not state.get("bot_issued")
        if not await self.disconnect(owner, state):
            return "revoking"
        return "local" if copied else "disconnected"

    async def migrate_legacy(self):
        for owner in self.owners():
            if (self.vault.directory(owner) / "state.json").is_file():
                async with self.session_lock(owner):
                    self.state(owner)

    def owners(self):
        if not self.state_dir.is_dir():
            return
        for child in self.state_dir.iterdir():
            if child.name.isascii() and child.name.isdecimal() and not child.is_symlink():
                if child.is_dir() and 0 < int(child.name) < 2**63:
                    yield int(child.name)

    async def maintenance(self):
        while True:
            for owner in self.owners():
                try:
                    async with self.session_lock(owner):
                        state = self.state(owner)
                        active = state["status"] == "active"
                        if (active and self.expired(state)) or (
                            state["status"] == "revoking"
                            and state.get("retry_at", 0) <= self.clock().timestamp()
                        ):
                            await self.disconnect(owner, state)
                            continue
                        today = self.clock().astimezone(zone("Europe/Moscow")).date()
                        prime = active and state.get("primed_on") != today.isoformat()
                        if prime:
                            last_try = state.get("prime_retry", 0)
                            current = self.clock().timestamp()
                            if current < last_try:
                                continue
                            state["prime_retry"] = current + 900
                            self.vault.save(owner, state)
                    if prime:
                        await self.fetch(owner, today, today + timedelta(days=13), prime=True)
                except (InvalidToken, OSError, ValueError, TimeoutError):
                    continue
            await asyncio.sleep(60)

    def cached(self, owner, first, last):
        try:
            stored = self.vault.read(owner, "schedule-cache.enc")
            covered = [
                (first + timedelta(days=offset)).isoformat()
                for offset in range((last - first).days + 1)
            ]
            if not any(day in stored for day in covered):
                return None, NO_CACHE_WARNING
            warning = STALE_WARNING if all(day in stored for day in covered) else PARTIAL_WARNING
            return [entry for day in covered for entry in stored.get(day, [])], warning
        except (InvalidToken, OSError, ValueError, TypeError):
            return None, NO_CACHE_WARNING

    def cache(self, owner, first, last, entries):
        try:
            stored = self.vault.read(owner, "schedule-cache.enc")
        except (InvalidToken, OSError, ValueError):
            stored = {}
        for offset in range((last - first).days + 1):
            stored[(first + timedelta(days=offset)).isoformat()] = []
        for entry in entries:
            stored[entry["date"]].append(entry)
        today = self.clock().astimezone(zone("Europe/Moscow")).date()
        low, high = (
            (today - timedelta(days=30)).isoformat(),
            (today + timedelta(days=365)).isoformat(),
        )
        self.vault.write(
            owner, "schedule-cache.enc", {day: stored[day] for day in stored if low <= day <= high}
        )

    async def display(self, owner, first, last):
        status = self.status(owner)["status"]
        if status == "never":
            return None, None
        try:
            async with asyncio.timeout(42):
                return await self.fetch(owner, first, last), None
        except (ITMOError, OSError, TimeoutError):
            return self.cached(owner, first, last)

    async def fetch(self, owner, first, last, *, prime=False):
        if self.status(owner)["status"] == "never":
            raise ITMOError("itmo_not_connected")
        if not 0 <= (last - first).days <= 30:
            raise ITMOError("itmo_invalid_range")
        async with self.session_lock(owner):
            try:
                state = self.state(owner)
                if state["status"] == "active" and self.expired(state):
                    await self.disconnect(owner, state)
                if state["status"] != "active":
                    raise ITMOError("itmo_not_connected")
                async with asyncio.timeout(45):
                    body = await self.exchange_encrypted(owner, state, first, last)
                if not isinstance(body, dict) or body.get("range") != {
                    "from": first.isoformat(),
                    "to": last.isoformat(),
                }:
                    raise ITMOError("itmo_invalid_response")
                days = body.get("days")
                if not isinstance(days, list):
                    raise ITMOError("itmo_invalid_response")
                raw = {"code": 0, "data": []}
                for day in days:
                    lessons = []
                    for lesson in day["lessons"]:
                        start, end = lesson["time"].split("-")
                        lessons.append(
                            {
                                "time_start": start,
                                "time_end": end,
                                "subject": lesson.get("subject"),
                                "room": lesson.get("room"),
                                "building": lesson.get("building"),
                            }
                        )
                    raw["data"].append({"date": day["date"], "lessons": lessons})
                entries = self.parse(raw, first, last)
                self.cache(owner, first, last, entries)
                if prime:
                    state["primed_on"] = first.isoformat()
                    self.vault.save(owner, state)
                return entries
            except ITMOError:
                raise
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ITMOError("itmo_invalid_response") from None
            except Exception:
                raise ITMOError("itmo_unavailable") from None

    async def exchange_encrypted(self, owner, state, first, last):
        # Node writes rotated credentials back. On Linux the plaintext is confined to tmpfs.
        with tempfile.TemporaryDirectory(
            dir="/dev/shm" if sys.platform == "linux" else None
        ) as path:
            directory = Path(path)
            target = directory / "state.json"
            refresh = state["sdk"].get("refreshToken")
            atomic_write(target, json.dumps({"refreshToken": refresh}).encode())
            try:
                return await self._exchange(directory, first, last)
            finally:
                if target.is_file():
                    try:
                        updated = json.loads(target.read_bytes())
                    except (OSError, ValueError):
                        updated = None
                    if isinstance(updated, dict) and updated.get("refreshToken"):
                        state["sdk"] = updated
                        self.vault.save(owner, state)

    async def _exchange(self, directory, first, last):
        env = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
            if key in os.environ
        }
        env.update({"ITMO_MCP_STATE_DIR": str(directory), "ITMO_MCP_ENABLE_WRITES": "false"})
        params = StdioServerParameters(command="node", args=[CLI, "--stdio"], env=env)
        # /dev/null is an immediate sink, not a disk file. Never log upstream diagnostics.
        with open(os.devnull, "w") as errors:  # noqa: ASYNC230
            async with (
                stdio_client(params, errlog=errors) as streams,
                ClientSession(*streams, read_timeout_seconds=timedelta(seconds=25)) as client,
            ):
                await client.initialize()
                result = await client.call_tool(
                    "itmo_get_schedule",
                    {"date_from": first.isoformat(), "date_to": last.isoformat()},
                )
        # Close SDK task groups before raising a domain error, so it is not wrapped
        # in an ExceptionGroup and misreported as an unavailable API.
        if result.isError:
            raise ITMOError("itmo_auth_required")
        if (
            len(result.content) != 1
            or result.content[0].type != "text"
            or len(result.content[0].text) > 1_000_000
        ):
            raise ITMOError("itmo_invalid_response")
        return json.loads(result.content[0].text)

    @staticmethod
    def parse(raw, first, last):
        try:
            if not isinstance(raw, dict) or type(raw.get("code")) is not int or raw["code"] != 0:
                raise ValueError
            days = raw["data"]
            if not isinstance(days, list) or len(days) > 31:
                raise ValueError
            entries = []
            seen = set()
            for day in days:
                when = date.fromisoformat(day["date"])
                if not first <= when <= last or not isinstance(day["lessons"], list):
                    raise ValueError
                for lesson in day["lessons"]:
                    location = (
                        ", ".join(
                            value
                            for value in [lesson.get("building"), lesson.get("room")]
                            if isinstance(value, str) and value.strip()
                        )
                        or "Место не указано ИТМО"
                    )
                    entry = ScheduleLesson(
                        date=when.isoformat(),
                        start=lesson["time_start"],
                        end=lesson["time_end"],
                        title=lesson.get("subject") or "Занятие без названия (ИТМО)",
                        location=location,
                    ).model_dump()
                    key = tuple(entry.values())
                    if key not in seen:
                        entries.append(entry)
                        seen.add(key)
                    if len(entries) > 372:
                        raise ValueError
            return entries
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ITMOError("itmo_invalid_response") from None
