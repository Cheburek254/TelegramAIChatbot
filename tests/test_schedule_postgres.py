import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.agent_store import ActionError
from app.db import create_pool
from app.mcp_client import MCPBridge, MCPFailure
from app.migrations import migrate
from app.schedule_contracts import ScheduleRepeat
from app.schedule_store import ScheduleStore
from app.timezones import zone
from tests.test_integration import database as database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1", reason="Отдельная PostgreSQL в Docker"
    ),
]
ENTRY = {
    "date": "2026-11-02",
    "start": "10:00",
    "end": "11:30",
    "title": "Предмет A",
    "location": "A",
}


async def test_personal_schedule_isolation_confirmation_races_mcp_and_restart(database):
    settings, docker, ready, name = database
    pool = await create_pool(settings)
    bridge = MCPBridge(settings, "schedule-integration-" + uuid.uuid4().hex)
    now = datetime.now(UTC)
    store = ScheduleStore(pool)
    try:
        await migrate(pool)
        await migrate(pool)
        # No write before consent; concurrent duplicate update prepares one card.
        a, b = await asyncio.gather(
            *[store.prepare(101, "same-update", "add", None, ENTRY, now) for _ in range(2)]
        )
        assert a["id"] == b["id"]
        assert not await store.entries(101, date(2026, 11, 2), date(2026, 11, 2))
        with pytest.raises(ActionError, match="not_owned"):
            await store.confirm(202, a["id"], now)
        first, second = await asyncio.gather(*[store.confirm(101, a["id"], now) for _ in range(2)])
        identity = first["result_id"]
        assert identity == second["result_id"]
        assert await pool.fetchval("SELECT count(*) FROM personal_schedule_lessons") == 1
        assert (await store.day(101, ENTRY["date"])).lessons[0].title == "Предмет A"
        assert not (await store.day(202, ENTRY["date"])).lessons
        with pytest.raises(ActionError, match="not_owned"):
            await store.prepare(202, "steal-edit", "edit", identity, ENTRY, now)
        with pytest.raises(ActionError, match="not_owned"):
            await store.prepare(202, "steal-delete", "delete", identity, None, now)
        own = await store.prepare(202, "own", "add", None, {**ENTRY, "title": "Предмет B"}, now)
        await store.confirm(202, own["id"], now)
        assert await bridge.start()
        with pytest.raises(MCPFailure, match="trusted_context_required"):
            await bridge.call("get_schedule", {"date": ENTRY["date"]})
        for owner, expected in [(101, "Предмет A"), (202, "Предмет B")]:
            data = await bridge.call("get_schedule", {"date": ENTRY["date"]}, owner=owner)
            assert [row["title"] for row in data["lessons"]] == [expected]
            week = await bridge.read_week(owner)
            assert len(week["days"]) == 7 and week["source"] == "Личное расписание пользователя"
        # Resource follows the owner's saved zone and contains no other owner's rows.
        today = now.astimezone(zone(settings.mcp_resource_timezone)).date()
        weekly = await store.prepare(
            101,
            "week-row",
            "add",
            None,
            {
                **ENTRY,
                "date": today.isoformat(),
                "title": "Только 101",
                "start": "22:00",
                "end": "23:00",
            },
            now,
        )
        await store.confirm(101, weekly["id"], now)
        week = await bridge.read_week(101)
        assert any(row["title"] == "Только 101" for rows in week["days"].values() for row in rows)
        other = await bridge.read_week(202)
        assert not any(
            row["title"] == "Только 101" for rows in other["days"].values() for row in rows
        )
        await pool.execute(
            "DELETE FROM personal_schedule_lessons WHERE user_id=101 AND date=$1 AND title=$2",
            today,
            "Только 101",
        )
        # Edit and stale version protection.
        edit = await store.prepare(101, "edit", "edit", identity, {**ENTRY, "location": "B"}, now)
        await store.confirm(101, edit["id"], now)
        assert (await store.day(101, ENTRY["date"])).lessons[0].location == "B"
        stale = await store.prepare(101, "stale", "edit", identity, ENTRY, now)
        await pool.execute(
            "UPDATE personal_schedule_lessons SET version=version+1 WHERE id=$1", identity
        )
        assert (await store.confirm(101, stale["id"], now))["state"] == "stale"
        # Overlap never creates a second lesson; adjacent intervals are allowed.
        overlap = await store.prepare(101, "overlap", "add", None, ENTRY, now)
        with pytest.raises(ActionError, match="schedule_overlap"):
            await store.confirm(101, overlap["id"], now)
        adjacent = await store.prepare(
            101, "adjacent", "add", None, {**ENTRY, "start": "11:30", "end": "12:00"}, now
        )
        assert (await store.confirm(101, adjacent["id"], now))["state"] == "completed"
        cancelled = await store.prepare(101, "cancel", "delete", identity, None, now)
        assert (await store.confirm(101, cancelled["id"], now, cancel=True))["state"] == "cancelled"
        expired = await store.prepare(101, "expired", "delete", identity, None, now)
        assert (await store.confirm(101, expired["id"], now + timedelta(minutes=5)))[
            "state"
        ] == "expired"
        old = await store.prepare(101, "old", "delete", identity, None, now)
        newer = await store.prepare(101, "new", "delete", identity, None, now)
        assert (await store.confirm(101, old["id"], now))["state"] == "superseded"
        await bridge.close()
        await pool.close()
        await asyncio.to_thread(docker, "restart", name)
        await asyncio.to_thread(ready)
        pool = await create_pool(settings)
        store = ScheduleStore(pool)
        assert len((await store.day(101, ENTRY["date"])).lessons) == 2
        assert (await store.confirm(101, newer["id"], now))["state"] == "completed"
        assert len((await store.day(101, ENTRY["date"])).lessons) == 1
        # Academic odd weeks remain stable across New Year; one confirmation for the whole series.
        rule = ScheduleRepeat(
            **{
                **ENTRY,
                "date": "2026-12-28",
                "until": "2027-01-18",
                "anchor": "2026-12-30",
                "weekday": 1,
                "parity": "odd",
            }
        )
        series = await store.prepare(404, "repeat", "repeat", None, rule.model_dump(), now)
        assert not (await store.day(404, "2026-12-28")).lessons
        await asyncio.gather(
            store.confirm(404, series["id"], now), store.confirm(404, series["id"], now)
        )
        members = await store.entries(404, date(2026, 12, 28), date(2027, 1, 18))
        assert [r["date"].isoformat() for r in members] == ["2026-12-28", "2027-01-11"]
        assert all(r["series_id"] == series["id"] for r in members)
        assert not (await store.day(404, "2027-01-04")).lessons
        with pytest.raises(ActionError, match="not_owned"):
            await store.prepare(505, "foreign-series", "delete_series", members[0]["id"], None, now)
        removal = await store.prepare(
            404, "remove-series", "delete_series", members[0]["id"], None, now
        )
        await store.confirm(404, removal["id"], now)
        assert not await store.entries(404, date(2026, 12, 28), date(2027, 1, 18))
        # An import that conflicts on its second lesson rolls back the first too.
        saved = await store.prepare(
            505, "occupied", "add", None, {**ENTRY, "date": "2027-01-11"}, now
        )
        await store.confirm(505, saved["id"], now)
        batch = await store.prepare(505, "import", "import", None, rule.lessons(), now)
        with pytest.raises(ActionError, match="schedule_overlap"):
            await store.confirm(505, batch["id"], now)
        assert not (await store.day(505, "2026-12-28")).lessons
        assert len((await store.day(505, "2027-01-11")).lessons) == 1
        # A lesson can start while its confirmation card is still valid.
        start = datetime(2027, 1, 2, 7, tzinfo=UTC)
        batch = await store.prepare(
            606,
            "future-import",
            "import",
            None,
            [{**ENTRY, "date": "2027-01-02"}],
            start - timedelta(seconds=30),
        )
        with pytest.raises(ActionError, match="itmo_import_past"):
            await store.confirm(606, batch["id"], start)
        assert not (await store.day(606, "2027-01-02")).lessons
        for n in range(12):
            entry = {**ENTRY, "start": f"{n:02}:00", "end": f"{n:02}:30"}
            prepared = await store.prepare(303, f"capacity-{n}", "add", None, entry, now)
            await store.confirm(303, prepared["id"], now)
        full = await store.prepare(
            303, "full", "add", None, {**ENTRY, "start": "13:00", "end": "13:30"}, now
        )
        with pytest.raises(ActionError, match="schedule_full"):
            await store.confirm(303, full["id"], now)
        assert len((await store.day(303, ENTRY["date"])).lessons) == 12
        assert len((await store.day(202, ENTRY["date"])).lessons) == 1
        # Failed transaction rolls back both action and lesson.
        invalid = await store.prepare(101, "invalid", "add", None, {**ENTRY, "end": "08:00"}, now)
        with pytest.raises(ValueError):
            await store.confirm(101, invalid["id"], now)
        assert len((await store.day(101, ENTRY["date"])).lessons) == 1
    finally:
        await bridge.close()
        await pool.close()
