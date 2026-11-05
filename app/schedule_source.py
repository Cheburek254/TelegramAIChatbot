"""Свежий ИТМО для подключённого владельца, личная БД только без подключения."""

import asyncio
from datetime import date, timedelta

from app.itmo import ITMOError
from app.mcp_service.contracts import Lesson, ScheduleOutput, WeekOutput
from app.timezones import zone

ITMO_SOURCE = "my.itmo.ru · актуальное расписание ИТМО · Europe/Moscow"


class ScheduleSource:
    def __init__(self, local, itmo):
        self.local, self.itmo = local, itmo

    async def _fetch(self, owner, first, last):
        try:
            async with asyncio.timeout(45):
                return await self.itmo.fetch(owner, first, last)
        except ITMOError as exc:
            if str(exc) == "itmo_not_connected":
                return None
            raise
        except TimeoutError:
            raise ITMOError("itmo_unavailable") from None

    @staticmethod
    def _lesson(entry):
        return Lesson.model_validate({key: value for key, value in entry.items() if key != "date"})

    async def day(self, owner, value):
        day = date.fromisoformat(value)
        entries = await self._fetch(owner, day, day)
        if entries is None:
            return await self.local.day(owner, value)
        return ScheduleOutput(
            date=value, lessons=[self._lesson(entry) for entry in entries], source=ITMO_SOURCE
        )

    async def week(self, owner, now, timezone):
        today = now.astimezone(zone("Europe/Moscow")).date()
        first = today - timedelta(days=today.weekday())
        last = first + timedelta(days=6)
        entries = await self._fetch(owner, first, last)
        if entries is None:
            return await self.local.week(owner, now, timezone)
        days = {(first + timedelta(days=n)).isoformat(): [] for n in range(7)}
        for entry in entries:
            days[entry["date"]].append(self._lesson(entry))
        return WeekOutput(
            start=first.isoformat(),
            end=last.isoformat(),
            timezone="Europe/Moscow",
            days=days,
            source=ITMO_SOURCE,
        )
