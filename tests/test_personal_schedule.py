import json
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.schedule_service import ScheduleLesson, ScheduleService

NOW = datetime(2026, 11, 1, 22, tzinfo=UTC)
ENTRY = {
    "date": "2026-11-02",
    "start": "10:00",
    "end": "11:30",
    "title": "Алгоритмы",
    "location": "Аудитория A",
}


@pytest.mark.parametrize(
    "patch",
    [
        {"date": "2026-02-30"},
        {"date": "02.11.2026"},
        {"start": "25:00"},
        {"start": "10:60"},
        {"start": "9:00"},
        {"end": "10:00"},
        {"end": "09:00"},
        {"title": " "},
        {"location": ""},
        {"title": "x" * 201},
        {"user_id": 99},
    ],
)
def test_lesson_validation(patch):
    with pytest.raises(ValidationError):
        ScheduleLesson.model_validate({**ENTRY, **patch})


@pytest.fixture
def service():
    return ScheduleService(
        Mock(prepare=AsyncMock(), entries=AsyncMock(return_value=[]), confirm=AsyncMock()),
        Mock(timezone=AsyncMock(return_value="Europe/Moscow")),
        clock=lambda: NOW,
    )


async def test_prepare_has_exact_payload_owner_key_and_no_write(service):
    identity = uuid.uuid4()
    service.store.prepare.return_value = {
        "id": identity,
        "operation": "add",
        "state": "pending",
        "payload": json.dumps({"before": None, "after": ENTRY}),
        "expires_at": NOW + timedelta(minutes=5),
    }
    result = await service.command(
        42, "add 2026-11-02 10:00-11:30 | Алгоритмы | Аудитория A", "message:7"
    )
    service.store.prepare.assert_awaited_once_with(42, "message:7", "add", None, ENTRY, NOW)
    service.store.confirm.assert_not_called()
    assert result.pending_id == identity and "5 минут" in result.content


@pytest.mark.parametrize(
    "text",
    [
        "delete -1",
        "delete 1 anything",
        "edit 7",
        "add wrong",
        "delete 9223372036854775808",
        "add 2026-11-02 11:30-10:00 | X | A",
        "add 2026-11-02 10:00-11:30 | X | A | B",
    ],
)
async def test_invalid_command_cannot_prepare(service, text):
    result = await service.command(42, text, "message:1")
    assert result.pending_id is None and "Проверьте" in result.content
    service.store.prepare.assert_not_called()


async def test_default_day_uses_saved_timezone(service):
    result = await service.command(42, "", "menu")
    service.store.entries.assert_awaited_once_with(42, date(2026, 11, 2), date(2026, 11, 2))
    assert "не добавили" in result.content and "/schedule add" in result.content


async def test_week_is_monday_to_sunday_for_owner(service):
    await service.command(73, "week", "menu")
    service.store.entries.assert_awaited_once_with(73, date(2026, 11, 2), date(2026, 11, 8))


async def test_confirm_passes_owner_and_cancel(service):
    identity = uuid.uuid4()
    service.store.confirm.return_value = {"state": "cancelled"}
    result = await service.confirm(42, str(identity), cancel=True)
    service.store.confirm.assert_awaited_once_with(42, identity, NOW, cancel=True)
    assert "отменено" in result.content


async def test_itmo_import_button_uses_next_14_days_not_previous_calendar_week(service):
    service.clock = lambda: datetime(2026, 11, 1, 18, tzinfo=UTC)
    service.itmo = Mock(fetch=AsyncMock(return_value=[]))
    await service.button(42, "itmo_save", "click")
    service.itmo.fetch.assert_awaited_once_with(42, date(2026, 11, 1), date(2026, 11, 14))


async def test_itmo_import_clamps_past_dates_and_skips_started_lessons(service):
    now = datetime(2026, 11, 2, 7, tzinfo=UTC)  # 10:00 Moscow
    service.clock = lambda: now
    later = {**ENTRY, "start": "12:00", "end": "13:30"}
    service.itmo = Mock(fetch=AsyncMock(return_value=[ENTRY, later]))
    service.store.prepare.return_value = {
        "id": uuid.uuid4(),
        "state": "pending",
        "operation": "import",
        "payload": json.dumps({"before": None, "after": [later]}),
        "expires_at": now + timedelta(minutes=5),
    }
    reply = await service.command(42, "import 2026-11-01 2026-11-08", "msg")
    service.itmo.fetch.assert_awaited_once_with(42, date(2026, 11, 2), date(2026, 11, 8))
    service.store.prepare.assert_awaited_once_with(42, "msg", "import", None, [later], now)
    assert reply.pending_id


async def test_past_import_rejected_before_api_and_empty_future_not_prepared(service):
    service.itmo = Mock(fetch=AsyncMock(return_value=[ENTRY]))
    result = await service.command(42, "import 2026-10-26 2026-11-01", "past")
    assert "будущие" in result.content
    service.itmo.fetch.assert_not_called()
    service.clock = lambda: datetime(2026, 11, 2, 7, tzinfo=UTC)
    result = await service.command(42, "import 2026-11-02 2026-11-02", "started")
    assert "не начавшихся" in result.content
    service.store.prepare.assert_not_called()


async def test_itmo_view_can_still_request_explicit_history(service):
    service.itmo = Mock(fetch=AsyncMock(return_value=[]))
    await service.command(42, "itmo 2026-10-26 2026-11-01", "view")
    service.itmo.fetch.assert_awaited_once_with(42, date(2026, 10, 26), date(2026, 11, 1))
