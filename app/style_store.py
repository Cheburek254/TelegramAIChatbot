"""Owner-bound подтверждение декоратора; SQL не зависит от модели."""

import uuid
from datetime import timedelta

SCHEMA = """
ALTER TABLE chat_users ADD COLUMN IF NOT EXISTS speech_style TEXT NOT NULL DEFAULT 'default'
 CHECK(speech_style IN ('default','plain','yiddish'));
CREATE TABLE IF NOT EXISTS chat_style_actions (
 id UUID PRIMARY KEY, user_id BIGINT NOT NULL, request_key TEXT NOT NULL,
 style TEXT NOT NULL CHECK(style IN ('plain','yiddish')), expires_at TIMESTAMPTZ NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', UNIQUE(user_id,request_key)
);
"""


class SpeechStyleStore:
    def __init__(self, pool):
        self.pool = pool

    async def propose(self, owner, key, style, now):
        if style not in {"plain", "yiddish"}:
            raise ValueError("invalid_style")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", owner)
            old = await conn.fetchval(
                "SELECT id FROM chat_style_actions WHERE user_id=$1 AND request_key=$2", owner, key
            )
            if old:
                return old
            await conn.execute(
                "UPDATE chat_style_actions SET state='superseded' "
                "WHERE user_id=$1 AND state='pending'",
                owner,
            )
            identity = uuid.uuid4()
            await conn.execute(
                "INSERT INTO chat_style_actions(id,user_id,request_key,style,expires_at) "
                "VALUES($1,$2,$3,$4,$5)",
                identity,
                owner,
                key,
                style,
                now + timedelta(minutes=5),
            )
            return identity

    async def confirm(self, owner, identity, now, cancel=False):
        async with self.pool.acquire() as conn, conn.transaction():
            # Same lock as the dialogue, without deleting history or changing mode.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", owner)
            row = await conn.fetchrow(
                "SELECT * FROM chat_style_actions WHERE user_id=$1 AND id=$2 FOR UPDATE",
                owner,
                identity,
            )
            if not row or row["state"] != "pending":
                return "stale"
            state = (
                "cancelled" if cancel else "expired" if row["expires_at"] <= now else "completed"
            )
            if state == "completed":
                await conn.execute(
                    "INSERT INTO chat_users(user_id,speech_style) VALUES($1,$2) "
                    "ON CONFLICT(user_id) DO UPDATE SET speech_style=excluded.speech_style",
                    owner,
                    row["style"],
                )
            await conn.execute(
                "UPDATE chat_style_actions SET state=$2 WHERE id=$1", identity, state
            )
            return row["style"] if state == "completed" else state
