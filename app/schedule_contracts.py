"""Проверенные поля занятия, общие для команды и границы записи в БД."""

from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ScheduleLesson(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    start: Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]
    end: Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]
    title: Text
    location: Text

    @property
    def date_value(self):
        return date.fromisoformat(self.date)

    @property
    def start_value(self):
        return time.fromisoformat(self.start)

    @property
    def end_value(self):
        return time.fromisoformat(self.end)

    @model_validator(mode="after")
    def valid(self):
        date.fromisoformat(self.date)
        if self.start_value >= self.end_value:
            raise ValueError("start_before_end_required")
        return self


class ScheduleRepeat(ScheduleLesson):
    until: str
    weekday: int
    parity: Literal["all", "odd", "even"]
    anchor: str

    @model_validator(mode="after")
    def valid_repeat(self):
        last = date.fromisoformat(self.until)
        anchor = date.fromisoformat(self.anchor)
        first_week = anchor - timedelta(days=anchor.weekday())
        if not 1 <= self.weekday <= 7 or not 0 <= (last - self.date_value).days <= 365:
            raise ValueError("invalid_repeat_range")
        if self.date_value < first_week or not self.dates():
            raise ValueError("invalid_first_week_or_empty_series")
        return self

    def dates(self):
        first = self.date_value
        last = date.fromisoformat(self.until)
        anchor = date.fromisoformat(self.anchor)
        anchor -= timedelta(days=anchor.weekday())
        result = []
        for n in range(max(0, (last - first).days + 1)):
            day = first + timedelta(days=n)
            week = (day - anchor).days // 7 + 1
            if day.isoweekday() == self.weekday and (
                self.parity == "all" or week % 2 == (1 if self.parity == "odd" else 0)
            ):
                result.append(day)
        return result

    def lessons(self):
        common = self.model_dump(include={"start", "end", "title", "location"})
        return [{"date": day.isoformat(), **common} for day in self.dates()]


def future_itmo_lessons(entries, now):
    """ИТМО задаёт московские часы; прошедшие/начавшиеся занятия не импортируются."""
    result = []
    for payload in entries:
        entry = ScheduleLesson.model_validate(payload)
        start = datetime.combine(entry.date_value, entry.start_value, ZoneInfo("Europe/Moscow"))
        if start > now:
            result.append(entry.model_dump())
    return result
