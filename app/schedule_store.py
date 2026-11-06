"""Личное расписание и атомарные подтверждения; SQL всегда ограничен владельцем."""

import json
import uuid
from datetime import timedelta

from app.agent_store import ActionError
from app.mcp_service.contracts import Lesson, ScheduleOutput, WeekOutput
from app.schedule_contracts import ScheduleLesson, ScheduleRepeat, future_itmo_lessons
from app.timezones import zone

SOURCE = "Личное расписание пользователя"


def lesson_payload(row):
    return {
        "date": row["date"].isoformat(),
        "start": row["start"].strftime("%H:%M"),
        "end": row["end"].strftime("%H:%M"),
        "title": row["title"],
        "location": row["location"],
    }


class ScheduleStore:
    def __init__(self, pool):
        self.pool = pool

    async def _lock(self, conn, owner):
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"schedule:{owner}"
        )

    async def entries(self, owner, first, last):
        return await self.pool.fetch(
            "SELECT * FROM personal_schedule_lessons WHERE user_id=$1 AND date BETWEEN $2 AND $3 "
            'ORDER BY date,"start",id',
            owner,
            first,
            last,
        )

    async def day(self, owner, value):
        from datetime import date

        day = date.fromisoformat(value)
        rows = await self.entries(owner, day, day)
        return ScheduleOutput(
            date=value,
            lessons=[
                Lesson.model_validate({k: v for k, v in lesson_payload(row).items() if k != "date"})
                for row in rows
            ],
            source=SOURCE,
        )

    async def week(self, owner, now, default_timezone):
        timezone = (
            await self.pool.fetchval("SELECT timezone FROM agent_settings WHERE user_id=$1", owner)
            or default_timezone
        )
        today = now.astimezone(zone(timezone)).date()
        first = today - timedelta(days=today.weekday())
        last = first + timedelta(days=6)
        rows = await self.entries(owner, first, last)
        days = {(first + timedelta(days=n)).isoformat(): [] for n in range(7)}
        for row in rows:
            payload = lesson_payload(row)
            days[payload.pop("date")].append(Lesson.model_validate(payload))
        return WeekOutput(
            start=first.isoformat(),
            end=last.isoformat(),
            timezone=timezone,
            days=days,
            source=SOURCE,
        )

    async def prepare(
        self, owner, request_key, operation, identity, payload, now, *, expected_version=None
    ):
        async with self.pool.acquire() as conn, conn.transaction():
            await self._lock(conn, owner)
            cached = await conn.fetchrow(
                "SELECT * FROM personal_schedule_actions WHERE user_id=$1 AND request_key=$2",
                owner,
                request_key,
            )
            if cached:
                return dict(cached)
            original = None
            if operation in {"edit", "delete", "delete_series"}:
                original = await conn.fetchrow(
                    "SELECT * FROM personal_schedule_lessons WHERE user_id=$1 AND id=$2 FOR UPDATE",
                    owner,
                    identity,
                )
                if not original:
                    raise ActionError("not_owned")
                if expected_version is not None and original["version"] != expected_version:
                    raise ActionError("stale_schedule")
            content = {"after": payload, "before": lesson_payload(original) if original else None}
            if operation == "delete_series":
                if not original["series_id"]:
                    raise ActionError("not_a_series")
                content["series_id"] = str(original["series_id"])
                members = await conn.fetch(
                    "SELECT id,version FROM personal_schedule_lessons "
                    "WHERE user_id=$1 AND series_id=$2 ORDER BY id",
                    owner,
                    original["series_id"],
                )
                content["members"] = [[r["id"], r["version"]] for r in members]
            await conn.execute(
                "UPDATE personal_schedule_actions SET state='superseded' "
                "WHERE user_id=$1 AND state='pending'",
                owner,
            )
            row = await conn.fetchrow(
                "INSERT INTO personal_schedule_actions"
                "(id,user_id,request_key,operation,lesson_id,expected_version,"
                "payload,expires_at,state) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,'pending') RETURNING *",
                uuid.uuid4(),
                owner,
                request_key,
                operation,
                identity,
                original["version"] if original else None,
                json.dumps(content, ensure_ascii=False),
                now + timedelta(minutes=5),
            )
            return dict(row)

    async def confirm(self, owner, identity, now, *, cancel=False):
        async with self.pool.acquire() as conn, conn.transaction():
            await self._lock(conn, owner)
            row = await conn.fetchrow(
                "SELECT * FROM personal_schedule_actions WHERE user_id=$1 AND id=$2 FOR UPDATE",
                owner,
                identity,
            )
            if not row:
                raise ActionError("not_owned")
            row = dict(row)
            if row["state"] != "pending":
                return row
            state = "expired" if row["expires_at"] <= now else "cancelled" if cancel else None
            if state:
                await conn.execute(
                    "UPDATE personal_schedule_actions SET state=$3 WHERE user_id=$1 AND id=$2",
                    owner,
                    identity,
                    state,
                )
                return {**row, "state": state}
            if row["operation"] in {"edit", "delete", "delete_series"}:
                version = await conn.fetchval(
                    "SELECT version FROM personal_schedule_lessons WHERE user_id=$1 AND id=$2 "
                    "FOR UPDATE",
                    owner,
                    row["lesson_id"],
                )
                if version != row["expected_version"]:
                    await conn.execute(
                        "UPDATE personal_schedule_actions SET state='stale' "
                        "WHERE user_id=$1 AND id=$2",
                        owner,
                        identity,
                    )
                    return {**row, "state": "stale"}
            content = json.loads(row["payload"])
            result_id = row["lesson_id"]
            if row["operation"] == "delete_series":
                series = uuid.UUID(content["series_id"])
                members = await conn.fetch(
                    "SELECT id,version FROM personal_schedule_lessons "
                    "WHERE user_id=$1 AND series_id=$2 ORDER BY id",
                    owner,
                    series,
                )
                if [[r["id"], r["version"]] for r in members] != content["members"]:
                    raise ActionError("stale_series")
                await conn.execute(
                    "DELETE FROM personal_schedule_lessons WHERE user_id=$1 AND series_id=$2",
                    owner,
                    series,
                )
            elif row["operation"] in {"repeat", "import"}:
                entries = (
                    ScheduleRepeat.model_validate(content["after"]).lessons()
                    if row["operation"] == "repeat"
                    else content["after"]
                )
                if not isinstance(entries, list) or not 1 <= len(entries) <= 372:
                    raise ActionError("invalid_schedule_batch")
                if row["operation"] == "import" and len(future_itmo_lessons(entries, now)) != len(
                    entries
                ):
                    raise ActionError("itmo_import_past")
                for payload in entries:
                    result_id = await self._write_lesson(
                        conn, owner, ScheduleLesson.model_validate(payload), series=identity
                    )
            elif row["operation"] == "delete":
                await conn.execute(
                    "DELETE FROM personal_schedule_lessons WHERE user_id=$1 AND id=$2",
                    owner,
                    row["lesson_id"],
                )
            else:
                entry = ScheduleLesson.model_validate(content["after"])
                result_id = await self._write_lesson(conn, owner, entry, row["lesson_id"])
            await conn.execute(
                "UPDATE personal_schedule_actions SET state='completed',result_id=$3 "
                "WHERE user_id=$1 AND id=$2",
                owner,
                identity,
                result_id,
            )
            return {**row, "state": "completed", "result_id": result_id}

    async def _write_lesson(self, conn, owner, entry, identity=None, *, series=None):
        count = await conn.fetchval(
            "SELECT count(*) FROM personal_schedule_lessons WHERE user_id=$1 AND date=$2 "
            "AND ($3::bigint IS NULL OR id<>$3)",
            owner,
            entry.date_value,
            identity,
        )
        if count >= 12:
            raise ActionError("schedule_full")
        if await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM personal_schedule_lessons WHERE user_id=$1 "
            'AND date=$2 AND "start"<$4 AND "end">$3 AND ($5::bigint IS NULL OR id<>$5))',
            owner,
            entry.date_value,
            entry.start_value,
            entry.end_value,
            identity,
        ):
            raise ActionError("schedule_overlap")
        values = (
            owner,
            entry.date_value,
            entry.start_value,
            entry.end_value,
            entry.title,
            entry.location,
        )
        if identity is None:
            return await conn.fetchval(
                "INSERT INTO personal_schedule_lessons"
                '(user_id,date,"start","end",title,location,series_id) '
                "VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id",
                *values,
                series,
            )
        await conn.execute(
            'UPDATE personal_schedule_lessons SET date=$2,"start"=$3,"end"=$4,title=$5,'
            "location=$6,version=version+1 WHERE user_id=$1 AND id=$7",
            *values,
            identity,
        )
        return identity
