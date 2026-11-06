from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

import asyncpg

from app.config import Settings

_owner = ContextVar("database_owner", default=None)


@contextmanager
def owner_scope(owner):
    token = _owner.set(owner)
    try:
        yield
    finally:
        _owner.reset(token)


class ScopedPool:
    """Transaction-local identity never survives checkout or exception."""

    def __init__(self, pool):
        self._pool = pool

    @asynccontextmanager
    async def acquire(self):
        async with self._pool.acquire() as connection, connection.transaction():
            owner = _owner.get()
            if owner is not None:
                if type(owner) is not int or not 0 < owner < 2**63:
                    raise ValueError("invalid_database_owner")
                await connection.execute("SELECT set_config('app.owner_id', $1, true)", str(owner))
            yield connection

    async def execute(self, sql, *args, **kwargs):
        async with self.acquire() as connection:
            return await connection.execute(sql, *args, **kwargs)

    async def fetch(self, sql, *args, **kwargs):
        async with self.acquire() as connection:
            return await connection.fetch(sql, *args, **kwargs)

    async def fetchrow(self, sql, *args, **kwargs):
        async with self.acquire() as connection:
            return await connection.fetchrow(sql, *args, **kwargs)

    async def fetchval(self, sql, *args, **kwargs):
        async with self.acquire() as connection:
            return await connection.fetchval(sql, *args, **kwargs)

    async def close(self):
        await self._pool.close()

    def terminate(self):
        self._pool.terminate()


class OwnerMiddleware:
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        with owner_scope(user.id if user else None):
            return await handler(event, data)


async def job_owners(pool):
    if isinstance(pool, ScopedPool):
        rows = await pool.fetch("SELECT public.study_job_owners() AS user_id")
        return [row["user_id"] for row in rows]
    return [None]


async def create_pool(settings: Settings) -> asyncpg.Pool | ScopedPool:
    # Небольшого пула достаточно для учебной ВМ. Таблицы добавляют студенты.
    pool = await asyncpg.create_pool(
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password,
        min_size=1,
        max_size=10,
        timeout=5,
        command_timeout=5,
    )
    try:
        await pool.fetchval("SELECT 1", timeout=3)
        if settings.postgres_rls and not await pool.fetchval("""
            SELECT NOT (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole)
                AND rolname='study_assistant_runtime'
                AND (SELECT relrowsecurity AND relforcerowsecurity
                     FROM pg_class WHERE oid='public.chat_users'::regclass)
            FROM pg_roles WHERE rolname=current_user
        """):
            raise ValueError("restricted_database_role_and_policies_required")
    except BaseException:
        await pool.close()
        raise
    return ScopedPool(pool) if settings.postgres_rls else pool
