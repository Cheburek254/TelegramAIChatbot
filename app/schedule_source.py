"""Свежий ИТМО для подключённого владельца, личная БД только без подключения."""

from datetime import date, timedelta

from app.mcp_service.contracts import Lesson, ScheduleOutput, WeekOutput
from app.timezones import zone

ITMO_SOURCE = "my.itmo.ru · актуальное расписание ИТМО · Europe/Moscow"


class ScheduleSource:
    def __init__(self, local, itmo):
        self.local, self.itmo = local, itmo

    @staticmethod
    def _lesson(entry):
        return Lesson.model_validate({key: value for key, value in entry.items() if key != "date"})

    async def day(self, owner, value):
        day = date.fromisoformat(value)
        entries, warning = await self.itmo.display(owner, day, day)
        if entries is None:
            result = await self.local.day(owner, value)
            return result.model_copy(update={"source": warning}) if warning else result
        return ScheduleOutput(
            date=value,
            lessons=[self._lesson(entry) for entry in entries],
            source=warning or ITMO_SOURCE,
        )

    async def week(self, owner, now, timezone):
        today = now.astimezone(zone("Europe/Moscow")).date()
        first = today - timedelta(days=today.weekday())
        last = first + timedelta(days=6)
        entries, warning = await self.itmo.display(owner, first, last)
        if entries is None:
            result = await self.local.week(owner, now, timezone)
            return result.model_copy(update={"source": warning}) if warning else result
        days = {(first + timedelta(days=n)).isoformat(): [] for n in range(7)}
        for entry in entries:
            days[entry["date"]].append(self._lesson(entry))
        return WeekOutput(
            start=first.isoformat(),
            end=last.isoformat(),
            timezone="Europe/Moscow",
            days=days,
            source=warning or ITMO_SOURCE,
        )
