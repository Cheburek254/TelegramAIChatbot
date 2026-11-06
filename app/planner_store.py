"""Личные задачи и подтверждённые изменения уведомлений."""

import uuid
from datetime import datetime, time, timedelta

from app.agent_store import insert_reminder
from app.timezones import next_task_due, zone

SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_tasks (
 id UUID PRIMARY KEY, user_id BIGINT NOT NULL,
 text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 500),
 due_at TIMESTAMPTZ, timezone TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS personal_tasks_active ON personal_tasks(user_id,due_at,id)
 WHERE completed_at IS NULL;
ALTER TABLE personal_tasks ADD COLUMN IF NOT EXISTS notify_before INTEGER
 CHECK(notify_before BETWEEN 1 AND 10080);
ALTER TABLE personal_tasks ADD COLUMN IF NOT EXISTS repeat_days INTEGER
 CHECK(repeat_days IN (1,7));
ALTER TABLE personal_tasks ADD COLUMN IF NOT EXISTS previous_id UUID UNIQUE;
CREATE INDEX IF NOT EXISTS personal_tasks_history ON personal_tasks(user_id,completed_at DESC,id)
 WHERE completed_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS planner_actions (
 id UUID PRIMARY KEY, user_id BIGINT NOT NULL, request_key TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('t','r','d')),
 operation TEXT NOT NULL,
 target UUID, version INTEGER, text TEXT, timezone TEXT, due_at TIMESTAMPTZ, minute INTEGER,
 local_date DATE,
 expires_at TIMESTAMPTZ NOT NULL, consumed BOOLEAN NOT NULL DEFAULT false,
 CHECK(operation<>'add' OR kind='t'),
 UNIQUE(user_id,request_key)
);
ALTER TABLE planner_actions ADD COLUMN IF NOT EXISTS repeat_days INTEGER
 CHECK(repeat_days IN (1,7));
ALTER TABLE planner_actions DROP CONSTRAINT IF EXISTS planner_actions_operation_check;
ALTER TABLE planner_actions ADD CONSTRAINT planner_actions_operation_check
 CHECK(operation IN ('add','done','move') OR
       (operation IN ('notify','restore','stop_repeat') AND kind='t') OR
       (operation='focus' AND kind='r'));
ALTER TABLE agent_reminders ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE agent_reminders ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE assistant_daily_reminders ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE assistant_daily_reminders ADD COLUMN IF NOT EXISTS skip_on DATE;
ALTER TABLE assistant_outbox ADD COLUMN IF NOT EXISTS reminder_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE assistant_outbox ADD COLUMN IF NOT EXISTS task_id UUID;
CREATE INDEX IF NOT EXISTS assistant_outbox_reminder ON assistant_outbox(reminder_id)
 WHERE reminder_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS assistant_outbox_task ON assistant_outbox(task_id)
 WHERE task_id IS NOT NULL;
"""

# Only application constants are interpolated. Values and owner always use parameters.
ITEMS = {
    "t": ("personal_tasks", "due_at", "completed_at IS NULL"),
    "r": ("agent_reminders", "remind_at", "completed_at IS NULL"),
    "d": ("assistant_daily_reminders", "minute", "true"),
}
PAGE_SIZE = 6


class PlannerStore:
    def __init__(self, pool):
        self.pool = pool

    async def active(self, owner, kind, offset):
        table, order, active = ITEMS[kind]
        return await self.pool.fetch(
            f"SELECT * FROM {table} WHERE user_id=$1 AND {active} "
            f"ORDER BY {order} NULLS LAST,id LIMIT $2 OFFSET $3",
            owner,
            PAGE_SIZE + 1,
            offset,
        )

    async def item(self, owner, kind, identity):
        table, _, active = ITEMS[kind]
        return await self.pool.fetchrow(
            f"SELECT * FROM {table} WHERE user_id=$1 AND id=$2 AND {active}", owner, identity
        )

    async def history(self, owner, offset=0, query=""):
        return await self.pool.fetch(
            "SELECT * FROM personal_tasks WHERE user_id=$1 AND completed_at IS NOT NULL "
            "AND ($2='' OR strpos(lower(text),lower($2))>0) "
            "ORDER BY completed_at DESC,id LIMIT $3 OFFSET $4",
            owner,
            query,
            PAGE_SIZE + 1,
            offset,
        )

    async def completed(self, owner, identity):
        return await self.pool.fetchrow(
            "SELECT * FROM personal_tasks WHERE user_id=$1 AND id=$2 AND completed_at IS NOT NULL",
            owner,
            identity,
        )

    async def deadline_for(self, owner, title):
        return await self.pool.fetchval(
            "SELECT min(due_at) FROM personal_tasks "
            "WHERE user_id=$1 AND completed_at IS NULL AND lower(text)=lower($2)",
            owner,
            title,
        )

    async def briefing_tasks(self, owner, end):
        return await self.pool.fetch(
            "SELECT text,due_at FROM personal_tasks WHERE user_id=$1 AND completed_at IS NULL "
            "AND (due_at<$2 OR due_at IS NULL) ORDER BY due_at NULLS LAST,id LIMIT 8",
            owner,
            end,
        )

    async def read_tasks(self, owner, scope, now, default_timezone):
        timezone = (
            await self.pool.fetchval("SELECT timezone FROM agent_settings WHERE user_id=$1", owner)
            or default_timezone
        )
        tz = zone(timezone)
        today = now.astimezone(tz).date()
        first, last = None, None
        if scope == "upcoming":
            first = now
        elif scope == "overdue":
            last = now
        elif scope in {"today", "tomorrow", "week"}:
            if scope == "week":
                start = today - timedelta(days=today.weekday())
                days = 7
            else:
                start = today + timedelta(days=scope == "tomorrow")
                days = 1
            first = datetime.combine(start, time(), tz)
            last = datetime.combine(start + timedelta(days=days), time(), tz)
        rows = await self.pool.fetch(
            "SELECT text,due_at FROM personal_tasks WHERE user_id=$1 AND completed_at IS NULL "
            "AND ($2::timestamptz IS NULL OR due_at>=$2) "
            "AND ($3::timestamptz IS NULL OR due_at<$3) "
            "ORDER BY due_at NULLS LAST,id LIMIT 13",
            owner,
            first,
            last,
        )
        return {
            "scope": scope,
            "timezone": timezone,
            "tasks": [
                {
                    "text": row["text"],
                    "due_at": row["due_at"].isoformat() if row["due_at"] else None,
                }
                for row in rows[:12]
            ],
            "more": len(rows) > 12,
        }

    async def proposal(
        self,
        owner,
        key,
        now,
        kind,
        operation,
        *,
        target=None,
        version=None,
        text=None,
        timezone=None,
        due_at=None,
        minute=None,
        local_date=None,
        repeat_days=None,
    ):
        return await self.pool.fetchval(
            "INSERT INTO planner_actions(id,user_id,request_key,kind,operation,target,version,"
            "text,timezone,due_at,minute,local_date,expires_at,repeat_days) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) "
            "ON CONFLICT(user_id,request_key) DO UPDATE SET request_key=excluded.request_key "
            "RETURNING id",
            uuid.uuid4(),
            owner,
            key,
            kind,
            operation,
            target,
            version,
            text,
            timezone,
            due_at,
            minute,
            local_date,
            now + timedelta(minutes=5),
            repeat_days,
        )

    async def _cancel_deliveries(self, conn, owner, kind, target):
        column = {"t": "task_id", "r": "reminder_id", "d": "daily_reminder_id"}[kind]
        await conn.execute(
            "UPDATE assistant_outbox SET state='cancelled',payload=NULL "
            f"WHERE user_id=$1 AND {column}=$2 AND state IN ('pending','working','retry')",
            owner,
            target,
        )

    async def _restore(self, conn, owner, target, version):
        completed = await conn.fetchval(
            "SELECT id FROM personal_tasks WHERE user_id=$1 AND id=$2 AND version=$3 "
            "AND completed_at IS NOT NULL FOR UPDATE",
            owner,
            target,
            version,
        )
        if not completed:
            return "stale"
        successor = await conn.fetchrow(
            "SELECT id,version,EXISTS(SELECT 1 FROM assistant_outbox o "
            "WHERE o.user_id=$1 AND o.task_id=t.id AND o.state='sent') AS notified "
            "FROM personal_tasks t WHERE t.user_id=$1 AND t.previous_id=$2 FOR UPDATE",
            owner,
            target,
        )
        if successor:
            if successor["version"] != 1 or successor["notified"]:
                return "restore_conflict"
            await conn.execute(
                "DELETE FROM personal_tasks WHERE user_id=$1 AND id=$2", owner, successor["id"]
            )
            await self._cancel_deliveries(conn, owner, "t", successor["id"])
        await conn.execute(
            "UPDATE personal_tasks SET completed_at=NULL,version=version+1 "
            "WHERE user_id=$1 AND id=$2",
            owner,
            target,
        )
        return "restored"

    async def confirm(self, owner, identity, now, *, cancel=False):
        async with self.pool.acquire() as conn, conn.transaction():
            destination = await conn.fetchval(
                "SELECT destination_chat_id FROM assistant_profiles WHERE user_id=$1 FOR UPDATE",
                owner,
            )
            if destination != owner:
                return "invalid", None, None
            action = await conn.fetchrow(
                "UPDATE planner_actions SET consumed=true WHERE user_id=$1 AND id=$2 "
                "AND NOT consumed AND expires_at>$3 RETURNING *",
                owner,
                identity,
                now,
            )
            if not action:
                return "invalid", None, None
            kind, operation, target = action["kind"], action["operation"], action["target"]
            if cancel:
                return "cancelled", kind, target
            if action["due_at"] and action["due_at"] <= now:
                return "past", kind, target
            if operation == "add":
                await conn.execute(
                    "INSERT INTO personal_tasks(id,user_id,text,due_at,timezone,repeat_days) "
                    "VALUES($1,$2,$3,$4,$5,$6)",
                    identity,
                    owner,
                    action["text"],
                    action["due_at"],
                    action["timezone"],
                    action["repeat_days"],
                )
                return "added", kind, identity
            if operation == "restore":
                status = await self._restore(conn, owner, target, action["version"])
                return status, kind, target
            if operation == "focus":
                await conn.execute(
                    "INSERT INTO agent_pending(id,user_id,request_key,text,remind_at,timezone,"
                    "expires_at,state) VALUES($1,$2,$3,$4,$5,$6,$7,'completed')",
                    identity,
                    owner,
                    f"planner:{identity}",
                    action["text"],
                    action["due_at"],
                    action["timezone"],
                    action["expires_at"],
                )
                await insert_reminder(
                    conn,
                    identity,
                    identity,
                    owner,
                    action["text"],
                    action["due_at"],
                    action["timezone"],
                )
                return "focus_added", kind, identity
            table, timing, active = ITEMS[kind]
            where = f"WHERE user_id=$1 AND id=$2 AND version=$3 AND {active} RETURNING id"
            if operation == "done":
                update = "skip_on=$4" if kind == "d" else "completed_at=$4"
                value = action["local_date"] if kind == "d" else now
            elif operation == "notify":
                update = "notify_before=$4"
                value = action["minute"]
            elif operation == "stop_repeat":
                update = "repeat_days=$4"
                value = None
            elif kind == "d":
                update = "minute=$4,starts_at=$5"
                value = action["minute"]
            else:
                update = f"{timing}=$4"
                value = action["due_at"]
                if kind == "t":
                    lead = await conn.fetchval(
                        "SELECT notify_before FROM personal_tasks "
                        "WHERE user_id=$1 AND id=$2 AND version=$3 AND completed_at IS NULL",
                        owner,
                        target,
                        action["version"],
                    )
                    if lead and value <= now + timedelta(minutes=lead):
                        return "too_soon", kind, target
            args = (owner, target, action["version"], value)
            if kind == "d" and operation == "move":
                args = (*args, now)
            if not await conn.fetchval(
                f"UPDATE {table} SET {update},version=version+1 {where}", *args
            ):
                return "stale", kind, target
            await self._cancel_deliveries(conn, owner, kind, target)
            if operation == "done":
                if kind == "t":
                    task = await conn.fetchrow(
                        "SELECT * FROM personal_tasks WHERE user_id=$1 AND id=$2", owner, target
                    )
                    if task["repeat_days"]:
                        due = next_task_due(
                            task["due_at"], task["timezone"], task["repeat_days"], now
                        )
                        await conn.execute(
                            "INSERT INTO personal_tasks(id,user_id,text,due_at,timezone,"
                            "notify_before,repeat_days,previous_id) "
                            "VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
                            uuid.uuid4(),
                            owner,
                            task["text"],
                            due,
                            task["timezone"],
                            task["notify_before"],
                            task["repeat_days"],
                            target,
                        )
                        return "repeated", kind, target
                return ("done_today" if kind == "d" else "done"), kind, target
            if operation == "notify":
                return ("alert_set" if value else "alert_off"), kind, target
            if operation == "stop_repeat":
                return "repeat_off", kind, target
            return "moved", kind, target
