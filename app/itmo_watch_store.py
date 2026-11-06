"""Подписки ИТМО, личные снимки и атомарная постановка уведомлений."""

import json
import uuid
from datetime import timedelta

from app.orchestration.store import decode

SCHEMA = """
CREATE TABLE IF NOT EXISTS itmo_watch_profiles (
 user_id BIGINT PRIMARY KEY, enabled BOOLEAN NOT NULL, version INTEGER NOT NULL,
 next_check TIMESTAMPTZ NOT NULL, snapshot JSONB
);
CREATE INDEX IF NOT EXISTS itmo_watch_due ON itmo_watch_profiles(next_check) WHERE enabled;
CREATE TABLE IF NOT EXISTS itmo_watch_actions (
 id UUID PRIMARY KEY, user_id BIGINT NOT NULL, request_key TEXT NOT NULL,
 enabled BOOLEAN NOT NULL, expected_version INTEGER NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 consumed BOOLEAN NOT NULL DEFAULT false, UNIQUE(user_id,request_key)
);
"""


class ItmoWatchStore:
    def __init__(self, pool):
        self.pool = pool

    async def enabled(self, owner):
        return bool(
            await self.pool.fetchval(
                "SELECT enabled FROM itmo_watch_profiles WHERE user_id=$1", owner
            )
        )

    async def propose(self, owner, key, enabled, now):
        return await self.pool.fetchval(
            "INSERT INTO itmo_watch_actions(id,user_id,request_key,enabled,"
            "expected_version,expires_at) VALUES($1,$2,$3,$4,COALESCE("
            "(SELECT version FROM itmo_watch_profiles WHERE user_id=$2),0),$5) "
            "ON CONFLICT(user_id,request_key) DO UPDATE SET request_key=EXCLUDED.request_key "
            "RETURNING id",
            uuid.uuid4(),
            owner,
            key,
            enabled,
            now + timedelta(minutes=5),
        )

    async def confirm(self, owner, identity, now, *, cancel=False):
        async with self.pool.acquire() as conn, conn.transaction():
            if (
                await conn.fetchval(
                    "SELECT destination_chat_id FROM assistant_profiles "
                    "WHERE user_id=$1 FOR UPDATE",
                    owner,
                )
                != owner
            ):
                return None
            action = await conn.fetchrow(
                "UPDATE itmo_watch_actions SET consumed=true WHERE user_id=$1 AND id=$2 "
                "AND NOT consumed AND expires_at>$3 RETURNING enabled,expected_version",
                owner,
                identity,
                now,
            )
            if not action or cancel:
                return None
            current = await conn.fetchrow(
                "SELECT enabled,version FROM itmo_watch_profiles WHERE user_id=$1", owner
            )
            version = current["version"] if current else 0
            if version != action["expected_version"]:
                return None
            enabled = action["enabled"]
            if current and current["enabled"] == enabled:
                return enabled
            await conn.execute(
                "INSERT INTO itmo_watch_profiles(user_id,enabled,version,next_check) "
                "VALUES($1,$2,$3,$4) ON CONFLICT(user_id) DO UPDATE SET enabled=EXCLUDED.enabled,"
                "version=EXCLUDED.version,next_check=EXCLUDED.next_check,snapshot=NULL",
                owner,
                enabled,
                version + 1,
                now,
            )
            if not enabled:
                await conn.execute(
                    "UPDATE assistant_outbox SET state='cancelled',payload=NULL WHERE user_id=$1 "
                    "AND kind='itmo_change' AND state IN ('pending','retry','working')",
                    owner,
                )
            return enabled

    async def claim(self, now):
        row = await self.pool.fetchrow(
            "UPDATE itmo_watch_profiles SET next_check=$2 WHERE user_id=("
            "SELECT user_id FROM itmo_watch_profiles WHERE enabled AND next_check<=$1 "
            "ORDER BY next_check,user_id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *",
            now,
            now + timedelta(minutes=30),
        )
        return dict(row, snapshot=decode(row["snapshot"])) if row else None

    async def save(self, claim, snapshot, message, now):
        owner, version = claim["user_id"], claim["version"]
        async with self.pool.acquire() as conn, conn.transaction():
            if (
                await conn.fetchval(
                    "SELECT destination_chat_id FROM assistant_profiles "
                    "WHERE user_id=$1 FOR UPDATE",
                    owner,
                )
                != owner
            ):
                return
            updated = await conn.fetchval(
                "UPDATE itmo_watch_profiles SET snapshot=$4::jsonb "
                "WHERE user_id=$1 AND enabled AND version=$2 AND next_check=$3 RETURNING true",
                owner,
                version,
                claim["next_check"],
                json.dumps(snapshot, ensure_ascii=False),
            )
            if updated and message:
                identity = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO assistant_outbox(id,event_key,user_id,kind,due_at,deadline,"
                    "available_at,consent_version,payload) "
                    "VALUES($1,$2,$3,'itmo_change',$4,$5,$4,$6,$7)",
                    identity,
                    f"itmo:{identity}",
                    owner,
                    now,
                    now + timedelta(hours=2),
                    version,
                    message,
                )
