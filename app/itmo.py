"""Изолированная сессия itmo-mcp для каждого Telegram-владельца, только чтение расписания."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.schedule_contracts import ScheduleLesson

CLI = "/opt/itmo/node_modules/itmo-mcp/dist/cli.js"


class ITMOError(ValueError):
    pass


class ITMOClient:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir).resolve()
        self.locks = {}

    def connected(self, owner):
        if type(owner) is not int or owner <= 0:
            return False
        directory = self.state_dir / str(owner)
        return not directory.is_symlink() and (directory / "state.json").is_file()

    @asynccontextmanager
    async def session_lock(self, owner):
        async with self.locks.setdefault(owner, asyncio.Lock()):
            descriptor = None
            try:
                if os.name == "posix":
                    import fcntl

                    descriptor = os.open(
                        self.state_dir / str(owner) / ".lock", os.O_CREAT | os.O_RDWR, 0o600
                    )
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

    async def fetch(self, owner, first, last):
        if not self.connected(owner):
            raise ITMOError("itmo_not_connected")
        if not 0 <= (last - first).days <= 30:
            raise ITMOError("itmo_invalid_range")
        directory = self.state_dir / str(owner)
        async with self.session_lock(owner):
            try:
                async with asyncio.timeout(45):
                    body = await self._exchange(directory, first, last)
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
                return self.parse(raw, first, last)
            except ITMOError:
                raise
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ITMOError("itmo_invalid_response") from None
            except Exception:
                raise ITMOError("itmo_unavailable") from None

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
