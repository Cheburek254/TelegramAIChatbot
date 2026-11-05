"""Личные материалы и подтверждаемые настройки; только параметризованный SQL."""

import json
import uuid
from datetime import timedelta

from app.orchestration.store import decode

SCHEMA = """
ALTER TABLE assistant_profiles ADD COLUMN IF NOT EXISTS briefing_minute INTEGER NOT NULL DEFAULT 480
 CHECK(briefing_minute BETWEEN 0 AND 1439);
CREATE TABLE IF NOT EXISTS user_materials (
 doc_id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, title TEXT NOT NULL
 CHECK(length(title) BETWEEN 1 AND 140), content TEXT NOT NULL
 CHECK(length(content) BETWEEN 1 AND 50000), created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS user_materials_owner ON user_materials(user_id,created_at,doc_id);
CREATE TABLE IF NOT EXISTS user_content_pending (
 id UUID PRIMARY KEY, user_id BIGINT NOT NULL, request_key TEXT NOT NULL,
 operation TEXT NOT NULL CHECK(operation IN ('material_add','material_delete','briefing_time')),
 payload JSONB NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', UNIQUE(user_id,request_key)
);
"""


class MaterialStore:
    def __init__(self, pool):
        self.pool = pool

    async def documents(self, owner):
        return await self.pool.fetch(
            "SELECT doc_id,title,content FROM user_materials WHERE user_id=$1 "
            "ORDER BY created_at,doc_id",
            owner,
        )

    async def proposal(self, owner, key, operation, payload, now):
        identity = uuid.uuid4()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"content:{owner}"
            )
            old = await conn.fetchval(
                "SELECT id FROM user_content_pending WHERE user_id=$1 AND request_key=$2",
                owner,
                key,
            )
            if old:
                return old
            await conn.execute(
                "UPDATE user_content_pending SET state='superseded',payload='{}'::jsonb "
                "WHERE user_id=$1 AND operation=$2 AND state='pending'",
                owner,
                operation,
            )
            await conn.execute(
                "INSERT INTO user_content_pending"
                "(id,user_id,request_key,operation,payload,expires_at) "
                "VALUES($1,$2,$3,$4,$5,$6)",
                identity,
                owner,
                key,
                operation,
                json.dumps(payload),
                now + timedelta(minutes=5),
            )
        return identity

    async def confirm(self, owner, identity, now, cancel=False):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"content:{owner}"
            )
            row = await conn.fetchrow(
                "SELECT * FROM user_content_pending WHERE user_id=$1 AND id=$2 FOR UPDATE",
                owner,
                identity,
            )
            if not row or row["state"] != "pending":
                return "stale"
            state = (
                "cancelled" if cancel else "expired" if row["expires_at"] <= now else "completed"
            )
            payload = decode(row["payload"])
            if state == "completed":
                if row["operation"] == "material_add":
                    count = await conn.fetchval(
                        "SELECT count(*) FROM user_materials WHERE user_id=$1", owner
                    )
                    if count >= 20:
                        state = "quota"
                    else:
                        await conn.execute(
                            "INSERT INTO user_materials(doc_id,user_id,title,content,created_at) "
                            "VALUES($1,$2,$3,$4,$5)",
                            "u-" + identity.hex,
                            owner,
                            payload["title"],
                            payload["content"],
                            now,
                        )
                elif row["operation"] == "material_delete":
                    await conn.execute(
                        "DELETE FROM user_materials WHERE user_id=$1 AND doc_id=$2",
                        owner,
                        payload["doc_id"],
                    )
                else:
                    # Same profile lock as delivery: an old worker cannot send
                    # after the changed time has been committed.
                    await conn.execute(
                        "UPDATE assistant_profiles SET briefing_minute=$2,"
                        "consent_version=consent_version+1 WHERE user_id=$1",
                        owner,
                        payload["minute"],
                    )
                    await conn.execute(
                        "UPDATE assistant_outbox SET state='cancelled',payload=NULL "
                        "WHERE user_id=$1 AND kind='daily_briefing' "
                        "AND state IN ('pending','retry','working')",
                        owner,
                    )
            await conn.execute(
                "UPDATE user_content_pending SET state=$2,payload='{}'::jsonb WHERE id=$1",
                identity,
                state,
            )
            return row["operation"] if state == "completed" else state

    async def cleanup(self, now):
        # Keep IDs for replay protection; remove unpublished document bodies.
        await self.pool.execute(
            "UPDATE user_content_pending SET state='expired',payload='{}'::jsonb "
            "WHERE state='pending' AND expires_at<=$1",
            now,
        )
