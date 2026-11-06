import os
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.db import create_pool, job_owners, owner_scope
from app.db_security import RUNTIME, TABLES, install
from app.itmo_auth_store import AuthStore
from app.itmo_login import LoginError
from tests.test_integration import database as database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="Отдельная PostgreSQL"),
]


async def test_rls_existing_data_isolation_role_and_browser_ticket(database):
    settings, _, _, _ = database
    admin = await create_pool(settings)
    password = secrets.token_urlsafe(32)
    runtime = None
    try:
        await install(admin, password)
        await admin.execute("INSERT INTO chat_users(user_id) VALUES(101), (202)")
        await admin.execute("INSERT INTO assistant_profiles(user_id) VALUES(101), (202)")
        await install(admin, password)
        assert await admin.fetchval("SELECT count(*) FROM chat_users") == 2
        assert await admin.fetchval(
            "SELECT count(*) FROM pg_class WHERE relname=ANY($1::text[]) "
            "AND relrowsecurity AND relforcerowsecurity",
            list(TABLES),
        ) == len(TABLES)
        runtime = await create_pool(
            replace(settings, postgres_user=RUNTIME, postgres_password=password, postgres_rls=True)
        )
        assert await runtime.fetch("SELECT * FROM chat_users") == []
        assert set(await job_owners(runtime)) == {101, 202}
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await runtime.execute("ALTER TABLE chat_users DISABLE ROW LEVEL SECURITY")
        with owner_scope(101):
            assert await runtime.fetchval("SELECT user_id FROM chat_users") == 101
            assert await runtime.execute("DELETE FROM chat_users WHERE user_id=202") == "DELETE 0"
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await runtime.execute("INSERT INTO chat_users(user_id) VALUES(303)")
            now = datetime.now(UTC)
            await AuthStore(runtime).create(101, "fixture-digest", now)
            control = await AuthStore(runtime).propose_control(101, "off", now)
        with owner_scope(202):
            assert await runtime.fetchval("SELECT user_id FROM chat_users") == 202
            assert await runtime.fetch("SELECT * FROM itmo_login_tickets") == []
            with pytest.raises(LoginError, match="expired_itmo_change"):
                async with AuthStore(runtime).control(202, control, now):
                    pytest.fail("foreign confirmation accepted")
        with owner_scope(101):
            async with AuthStore(runtime).control(101, control, now) as value:
                assert value == "off"
            with pytest.raises(LoginError, match="expired_itmo_change"):
                async with AuthStore(runtime).control(101, control, now):
                    pytest.fail("replay accepted")
            expired = await AuthStore(runtime).propose_control(101, 7, now)
            with pytest.raises(LoginError, match="expired_itmo_change"):
                async with AuthStore(runtime).control(101, expired, now + timedelta(minutes=5)):
                    pytest.fail("expired confirmation accepted")
        assert await runtime.fetch("SELECT * FROM chat_users") == []
        row = await AuthStore(runtime).begin("fixture-digest", now)
        assert row["user_id"] == 101 and row["state"] == "busy"
        assert await runtime.fetch("SELECT * FROM itmo_login_tickets") == []
    finally:
        if runtime:
            await runtime.close()
        await admin.close()
