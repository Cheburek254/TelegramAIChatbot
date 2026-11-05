"""Постоянные подтверждения, атомарная дедупликация и безопасный аудит."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta


class ActionError(ValueError):
    pass


class AgentStore:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def request(self, user_id, request_key):
        async with self.pool.acquire() as conn, conn.transaction():
            # Отдельное пространство ключей от истории диалога.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"agent:{user_id}"
            )
            await conn.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            await conn.execute(
                "INSERT INTO chat_users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id
            )
            yield RequestUnit(conn, user_id, request_key)

    async def timezone(self, user_id, name=None):
        async with self.pool.acquire() as conn:
            if name is not None:
                await conn.execute(
                    (
                        "INSERT INTO agent_settings(user_id,timezone) VALUES($1,$2) ON "
                        "CONFLICT(user_id) DO UPDATE SET timezone=excluded.timezone"
                    ),
                    user_id,
                    name,
                )
            return await conn.fetchval(
                "SELECT timezone FROM agent_settings WHERE user_id=$1", user_id
            )

    async def last_audit(self, user_id):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM agent_audit WHERE user_id=$1 ORDER BY id DESC LIMIT 1", user_id
            )

    async def confirmation(self, user_id, action_id, now, *, cancel=False):
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM agent_pending WHERE id=$1 AND user_id=$2 FOR UPDATE",
                action_id,
                user_id,
            )
            if not row:
                raise ActionError("not_owned")
            if row["state"] == "completed":
                return dict(row)
            if row["state"] not in {"pending", "confirmed"}:
                raise ActionError(row["state"])
            if row["expires_at"] <= now:
                await conn.execute(
                    "UPDATE agent_pending SET state='expired' WHERE id=$1", action_id
                )
                return {**dict(row), "state": "expired"}
            state = "cancelled" if cancel else "confirmed"
            await conn.execute("UPDATE agent_pending SET state=$2 WHERE id=$1", action_id, state)
            return {**dict(row), "state": state}

    async def create_reminder(self, owner, action_id, arguments, now):
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM agent_pending WHERE id=$1 AND user_id=$2 FOR UPDATE",
                action_id,
                owner,
            )
            if not row or row["state"] not in {"confirmed", "completed"}:
                raise ActionError("confirmation_required")
            from datetime import datetime

            if row["text"] != arguments["text"] or row["remind_at"] != datetime.fromisoformat(
                arguments["remind_at"]
            ):
                raise ActionError("action_mismatch")
            old = await conn.fetchrow("SELECT * FROM agent_reminders WHERE action_id=$1", action_id)
            if old:
                return {
                    "id": str(old["id"]),
                    "remind_at": old["remind_at"].isoformat(),
                    "timezone": old["timezone"],
                    "created": False,
                }
            if row["expires_at"] <= now or row["remind_at"] <= now:
                raise ActionError("expired")
            from app.timezones import reminder_time

            reminder_time(arguments["remind_at"], row["timezone"], now)
            identity = uuid.uuid4()
            await conn.execute(
                (
                    "INSERT INTO "
                    "agent_reminders(id,action_id,user_id,text,remind_at,timezone) "
                    "VALUES($1,$2,$3,$4,$5,$6)"
                ),
                identity,
                action_id,
                owner,
                row["text"],
                row["remind_at"],
                row["timezone"],
            )
            await conn.execute("UPDATE agent_pending SET state='completed' WHERE id=$1", action_id)
            return {
                "id": str(identity),
                "remind_at": row["remind_at"].isoformat(),
                "timezone": row["timezone"],
                "created": True,
            }


class RequestUnit:
    def __init__(self, conn, user_id, key):
        self.conn, self.user_id, self.key = conn, user_id, key

    async def cached(self):
        return await self.conn.fetchrow(
            "SELECT response,pending_id FROM agent_requests WHERE user_id=$1 AND request_key=$2",
            self.user_id,
            self.key,
        )

    async def save(self, text, pending_id=None):
        await self.conn.execute(
            (
                "INSERT INTO agent_requests(user_id,request_key,response,pending_id) "
                "VALUES($1,$2,$3,$4)"
            ),
            self.user_id,
            self.key,
            text,
            pending_id,
        )

    async def prepare(self, arguments, timezone, moment, now):
        old = await self.conn.fetchrow(
            "SELECT * FROM agent_pending WHERE user_id=$1 AND request_key=$2",
            self.user_id,
            self.key,
        )
        if old:
            return old["id"]
        await self.conn.execute(
            "UPDATE agent_pending SET state='superseded' WHERE user_id=$1 AND state='pending'",
            self.user_id,
        )
        identity = uuid.uuid4()
        await self.conn.execute(
            (
                "INSERT INTO "
                "agent_pending(id,user_id,request_key,text,remind_at,timezone,expires_at,state)"
                " VALUES($1,$2,$3,$4,$5,$6,$7,'pending')"
            ),
            identity,
            self.user_id,
            self.key,
            arguments["text"],
            moment,
            timezone,
            now + timedelta(minutes=5),
        )
        return identity

    async def audit(self, now, action, tool, arguments, status, reason, duration_ms):
        await self.conn.execute(
            (
                "INSERT INTO "
                "agent_audit(user_id,created_at,action,tool,arguments,status,reason,duration_ms)"
                " VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8)"
            ),
            self.user_id,
            now,
            action,
            tool,
            json.dumps(arguments, ensure_ascii=False),
            status,
            reason,
            duration_ms,
        )
