"""Единые строгие контракты сервера; клиент получает их через discovery."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.material_contracts import SearchInput, SearchOutput


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AnswerDecision(StrictModel):
    action: Literal["respond", "clarify"]
    answer: str = Field(min_length=1, max_length=20000)
    reason: str = Field(min_length=1, max_length=240)


class RoutingDecision(StrictModel):
    action: Literal["respond", "clarify", "call_tool"]
    tool_name: str | None
    answer: str = Field(max_length=20000)
    reason: str = Field(min_length=1, max_length=240)


class WeatherInput(StrictModel):
    city: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class ScheduleInput(StrictModel):
    date: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]

    @field_validator("date")
    @classmethod
    def real_date(cls, value):
        date.fromisoformat(value)
        return value


TaskScope = Literal["upcoming", "overdue", "today", "tomorrow", "week", "all"]


class TasksInput(StrictModel):
    scope: TaskScope


class TaskItem(StrictModel):
    text: str = Field(min_length=1, max_length=500)
    due_at: str | None

    @field_validator("due_at")
    @classmethod
    def aware(cls, value):
        if value is not None and datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("aware_time_required")
        return value


class TasksOutput(StrictModel):
    scope: TaskScope
    timezone: str
    tasks: list[TaskItem] = Field(max_length=12)
    more: bool


class ReminderInput(StrictModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    remind_at: str = Field(
        description="ISO 8601 datetime with explicit offset, e.g. 2026-11-01T18:30:00+03:00"
    )

    @field_validator("remind_at")
    @classmethod
    def aware(cls, value):
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("timezone_required")
        return moment.isoformat()


class Lesson(StrictModel):
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    title: str = Field(min_length=1, max_length=200)
    location: str = Field(min_length=1, max_length=200)


class Place(StrictModel):
    name: str
    country: str
    region: str
    query: str


class WeatherOutput(StrictModel):
    status: Literal["ok", "not_found", "ambiguous"]
    source: str
    place: Place | None = None
    options: list[Place] = Field(default_factory=list, max_length=100)
    temperature_c: float | None = None
    weather_code: int | None = None
    wind_speed: float | None = None
    wind_unit: str | None = None
    observed_at: str | None = None
    timezone: str | None = None


class ScheduleOutput(StrictModel):
    date: str
    lessons: list[Lesson]
    source: str


class ReminderOutput(StrictModel):
    id: str
    remind_at: str
    timezone: str
    created: bool


class WeekOutput(StrictModel):
    start: str
    end: str
    timezone: str
    days: dict[str, list[Lesson]]
    source: str


INPUTS = {
    "get_weather": WeatherInput,
    "get_schedule": ScheduleInput,
    "get_tasks": TasksInput,
    "add_reminder": ReminderInput,
    "search_materials": SearchInput,
}
OUTPUTS = {
    "get_weather": WeatherOutput,
    "get_schedule": ScheduleOutput,
    "get_tasks": TasksOutput,
    "add_reminder": ReminderOutput,
    "search_materials": SearchOutput,
}
DESCRIPTIONS = {
    "get_tasks": "Прочитать открытые личные задачи или дедлайны по выбранному периоду.",
    "search_materials": "Поиск по фиксированным учебным материалам. Возвращает выдержки и doc_id.",
    "get_weather": (
        "Текущая погода: температура, осадки/условия, ветер; также для "
        "вопроса нужен ли зонт. city = город, при необходимости страна и "
        "регион. Не угадывай актуальную погоду."
    ),
    "get_schedule": (
        "Актуальное расписание ИТМО подключённого пользователя; без подключения — "
        "сохранённые личные занятия. Импорт не нужен. На дату: сегодня, завтра или день недели. "
        "date "
        "строго YYYY-MM-DD, вычисляй относительно доверенного текущего "
        "времени."
    ),
    "add_reminder": (
        "Подготовить создание напоминания. text = только содержание без "
        "команды Напомни; remind_at = будущее ISO-время с offset из "
        "сохранённой IANA-зоны. При неполном времени уточни. Запись создаётся"
        " только после подтверждения пользователя."
    ),
}
