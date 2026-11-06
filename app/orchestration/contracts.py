"""Проверяемые внутренние контракты; A2A-сервер не реализован."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.material_contracts import SearchOutput
from app.mcp_service.contracts import ScheduleOutput, TasksOutput, WeatherOutput, WeekOutput


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RouteDecision(Contract):
    target: Literal["schedule", "tasks", "knowledge", "lifestyle", "clarify", "legacy"]
    intent: Literal["read", "reminder", "answer", "clarify", "legacy"]
    scope: Literal["day", "week", "default", "today", "tomorrow", "upcoming", "overdue", "all"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=240)


class AgentRequest(Contract):
    request_id: uuid.UUID
    trace_id: uuid.UUID
    actor_id: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=20000)
    intent: Literal["user_query", "daily_briefing"]
    occurred_at: datetime
    timezone: str

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("aware_time_required")
        return value

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value):
        from app.timezones import zone

        zone(value)
        return value


class EmptyFacts(Contract):
    kind: Literal["empty"] = "empty"


class ScheduleFacts(Contract):
    kind: Literal["schedule"] = "schedule"
    data: ScheduleOutput | WeekOutput


class WeatherFacts(Contract):
    kind: Literal["weather"] = "weather"
    data: WeatherOutput


class KnowledgeFacts(Contract):
    kind: Literal["knowledge"] = "knowledge"
    data: SearchOutput


class TaskFacts(Contract):
    kind: Literal["tasks"] = "tasks"
    data: TasksOutput


Facts = Annotated[
    EmptyFacts | ScheduleFacts | WeatherFacts | KnowledgeFacts | TaskFacts,
    Field(discriminator="kind"),
]


class AgentResult(Contract):
    status: Literal["success", "needs_clarification", "not_found", "failed"]
    message: str = Field(min_length=1, max_length=30000)
    confidence: float = Field(ge=0, le=1)
    facts: Facts
    used_tools: list[str]
    error_code: str | None


class City(Contract):
    name: str = Field(min_length=1, max_length=100)
    country: str = Field(min_length=1, max_length=100)
    region: str = Field(max_length=140)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
