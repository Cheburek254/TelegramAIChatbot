"""Одноразовые подключения с hash ссылкой и зашифрованным refresh, всегда с владельцем."""

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

from app.db import ScopedPool, owner_scope
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
        await self.pool.execute("DELETE FROM itmo_session_actions WHERE expires_at<=$1", now)

    async def propose_control(self, owner, value, now):
        identity = uuid.uuid4()
        async with self.pool.acquire() as conn, conn.transaction():
            await self.lock_control(conn, owner)
            await conn.execute(
                "UPDATE itmo_session_actions SET consumed=true WHERE user_id=$1 AND NOT consumed",
                owner,
            )
            await conn.execute(
                "INSERT INTO itmo_session_actions(id,user_id,value,expires_at) VALUES($1,$2,$3,$4)",
                identity,
                owner,
                str(value),
                now + timedelta(minutes=5),
            )
        return identity.hex

    @staticmethod
    async def lock_control(conn, owner):
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"itmo-control:{owner}"
        )

    @asynccontextmanager
    async def control(self, owner, identity, now):
        async with self.pool.acquire() as conn, conn.transaction():
            await self.lock_control(conn, owner)
            value = await conn.fetchval(
                "UPDATE itmo_session_actions SET consumed=true "
                "WHERE id=$1 AND user_id=$2 AND NOT consumed AND expires_at>$3 RETURNING value",
                uuid.UUID(identity),
                owner,
                now,
            )
            if value is None:
                raise LoginError("expired_itmo_change")
            yield int(value) if value.isascii() and value.isdecimal() else value

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
        if owner is None and isinstance(self.pool, ScopedPool):
            owner = await self.pool.fetchval("SELECT public.study_itmo_ticket_owner($1)", digest)
            if owner is None:
                raise LoginError("expired_link")
        with owner_scope(owner):
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
