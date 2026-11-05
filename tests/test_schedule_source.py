from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from app.itmo import ITMOClient, ITMOError
from app.mcp_service.contracts import ScheduleOutput
from app.schedule_source import ITMO_SOURCE, ScheduleSource

ENTRY = {
    "date": "2026-11-02",
    "start": "10:00",
    "end": "11:30",
    "title": "Синтетический предмет",
    "location": "Аудитория A",
}


async def test_connected_owner_gets_fresh_itmo_without_local_read_or_import():
    local = Mock(day=AsyncMock(), week=AsyncMock())
    itmo = Mock(fetch=AsyncMock(side_effect=[[ENTRY], [{**ENTRY, "location": "Новая аудитория"}]]))
    source = ScheduleSource(local, itmo)
    first = await source.day(101, ENTRY["date"])
    second = await source.day(101, ENTRY["date"])
    assert first.source == ITMO_SOURCE
    assert first.lessons[0].location != second.lessons[0].location
    assert itmo.fetch.await_count == 2
    itmo.fetch.assert_awaited_with(101, date(2026, 11, 2), date(2026, 11, 2))
    local.day.assert_not_called()


async def test_unconnected_owner_only_falls_back_to_own_local_data(tmp_path):
    (tmp_path / "101").mkdir()
    (tmp_path / "101" / "state.json").write_text("{}")
    itmo = ITMOClient(tmp_path)
    itmo._exchange = AsyncMock()
    local = Mock(
        day=AsyncMock(return_value=ScheduleOutput(date=ENTRY["date"], lessons=[], source="local"))
    )
    result = await ScheduleSource(local, itmo).day(202, ENTRY["date"])
    local.day.assert_awaited_once_with(202, ENTRY["date"])
    itmo._exchange.assert_not_called()
    assert result.source == "local"


@pytest.mark.parametrize(
    "code", ["itmo_auth_required", "itmo_unavailable", "itmo_invalid_response"]
)
async def test_itmo_failure_never_silently_returns_stale_local_schedule(code):
    local = Mock(day=AsyncMock(), week=AsyncMock())
    itmo = Mock(fetch=AsyncMock(side_effect=ITMOError(code)))
    source = ScheduleSource(local, itmo)
    with pytest.raises(ITMOError, match=code):
        await source.day(101, ENTRY["date"])
    with pytest.raises(ITMOError, match=code):
        await source.week(101, datetime(2026, 11, 2, tzinfo=UTC), "UTC")
    local.day.assert_not_called()
    local.week.assert_not_called()


async def test_empty_itmo_is_authoritative_and_week_uses_moscow():
    local = Mock(day=AsyncMock(), week=AsyncMock())
    itmo = Mock(fetch=AsyncMock(return_value=[]))
    source = ScheduleSource(local, itmo)
    assert not (await source.day(101, ENTRY["date"])).lessons
    # Sunday UTC is already Monday in Moscow.
    result = await source.week(101, datetime(2026, 11, 1, 22, tzinfo=UTC), "UTC")
    assert result.start == "2026-11-02" and result.end == "2026-11-08"
    assert result.timezone == "Europe/Moscow" and result.source == ITMO_SOURCE
    assert len(result.days) == 7 and not any(result.days.values())
    local.day.assert_not_called()
    local.week.assert_not_called()


async def test_week_without_connection_keeps_local_timezone():
    local = Mock(week=AsyncMock())
    source = ScheduleSource(
        local, Mock(fetch=AsyncMock(side_effect=ITMOError("itmo_not_connected")))
    )
    now = datetime(2026, 11, 1, 22, tzinfo=UTC)
    await source.week(202, now, "UTC")
    local.week.assert_awaited_once_with(202, now, "UTC")


async def test_real_mcp_roundtrip_for_connected_owner_and_errors_keep_bridge_alive(
    tmp_path, monkeypatch
):
    from mcp.client.stdio import stdio_client

    from app.config import Settings
    from app.mcp_client import MCPBridge, MCPFailure

    directory = tmp_path / "101"
    directory.mkdir()
    (directory / "state.json").write_text("{}")
    code = """
import asyncio
from app.itmo import ITMOClient, ITMOError
from app.mcp_service.server import main
async def exchange(self, directory, first, last):
    if first != last: raise ITMOError('itmo_unavailable')
    if first.isoformat() == '2026-11-03': raise ITMOError('itmo_auth_required')
    return {'range': {'from': first.isoformat(), 'to': last.isoformat()},
            'days': [{'date': first.isoformat(), 'lessons': [
                {'time': '10:00-11:30', 'subject': 'Synthetic ITMO', 'room': 'A'}]}]}
ITMOClient._exchange = exchange
asyncio.run(main())
"""

    def fixture_process(params, *args, **kwargs):
        params = params.model_copy(update={"args": ["-c", code]})
        return stdio_client(params, *args, **kwargs)

    monkeypatch.setattr("app.mcp_client.stdio_client", fixture_process)
    bridge = MCPBridge(
        Settings(bot_token="unused", postgres_password="unused", itmo_state_dir=str(tmp_path)),
        "fixture-context-secret",
    )
    try:
        assert await bridge.start()
        result = await bridge.call("get_schedule", {"date": "2026-11-02"}, owner=101)
        assert result["source"] == ITMO_SOURCE and result["lessons"][0]["title"] == "Synthetic ITMO"
        with pytest.raises(MCPFailure, match="trusted_context_required"):
            await bridge.call("get_schedule", {"date": "2026-11-02"})
        with pytest.raises(MCPFailure, match="itmo_auth_required"):
            await bridge.call("get_schedule", {"date": "2026-11-03"}, owner=101)
        with pytest.raises(MCPFailure, match="itmo_unavailable"):
            await bridge.read_week(owner=101)
        assert bridge.available
        assert (await bridge.call("get_schedule", {"date": "2026-11-02"}, owner=101))["lessons"]
    finally:
        await bridge.close()
