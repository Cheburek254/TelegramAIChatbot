"""Одноразовые подключения с hash ссылкой и зашифрованным refresh, всегда с владельцем."""

import uuid
from datetime import timedelta

from app.itmo_login import LoginError


class AuthStore:
    def __init__(self, pool):
        self.pool = pool

    async def cleanup(self, now):
        await self.pool.execute(
            "UPDATE itmo_login_tickets SET state='cancelled',encrypted_token=NULL "
            "WHERE expires_at<=$1 AND state<>'completed'",
            now,
        )
        await self.pool.execute(
            "DELETE FROM itmo_login_tickets WHERE created_at<$1", now - timedelta(days=1)
        )

    async def create(self, owner, digest, now):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"itmo-login:{owner}"
            )
            count = await conn.fetchval(
                "SELECT count(*) FROM itmo_login_tickets WHERE user_id=$1 AND created_at>$2",
                owner,
                now - timedelta(minutes=10),
            )
            if count >= 5:
                raise LoginError("rate_limited")
            await conn.execute(
                "UPDATE itmo_login_tickets SET state='cancelled',encrypted_token=NULL "
                "WHERE user_id=$1 AND state IN ('issued','busy','verified')",
                owner,
            )
            return await conn.fetchrow(
                "INSERT INTO itmo_login_tickets(id,user_id,token_hash,created_at,expires_at,state) "
                "VALUES($1,$2,$3,$4,$5,'issued') RETURNING *",
                uuid.uuid4(),
                owner,
                digest,
                now,
                now + timedelta(minutes=10),
            )

    async def begin(self, digest, now, *, owner=None):
        row = await self.pool.fetchrow(
            "UPDATE itmo_login_tickets SET state='busy',attempts=attempts+1 "
            "WHERE token_hash=$1 AND expires_at>$2 AND state='issued' AND attempts<3 "
            "AND ($3::bigint IS NULL OR user_id=$3) RETURNING *",
            digest,
            now,
            owner,
        )
        if not row:
            raise LoginError("expired_link")
        return row

    async def finish(self, identity, encrypted, account, now):
        return await self.pool.fetchval(
            "UPDATE itmo_login_tickets SET state='verified',encrypted_token=$2,account=$3 "
            "WHERE id=$1 AND state='busy' AND expires_at>$4 RETURNING id",
            identity,
            encrypted,
            account,
            now,
        )

    async def failed(self, identity):
        await self.pool.execute(
            "UPDATE itmo_login_tickets SET state=CASE WHEN attempts<3 THEN 'issued' "
            "ELSE 'cancelled' END "
            "WHERE id=$1 AND state='busy'",
            identity,
        )

    async def status(self, owner, identity, now):
        row = await self.pool.fetchrow(
            "SELECT * FROM itmo_login_tickets WHERE user_id=$1 AND id=$2", owner, identity
        )
        if not row or row["expires_at"] <= now:
            raise LoginError("expired_link")
        return row
