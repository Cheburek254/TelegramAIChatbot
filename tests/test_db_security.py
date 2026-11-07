import os
import secrets
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest

from app.agent_store import AgentStore
from app.assistant import Assistant
from app.db import create_pool, job_owners, owner_scope
from app.db_security import RUNTIME, TABLES, install
from app.itmo_auth_store import AuthStore
from app.itmo_login import LoginError
from app.llm import LLMError
from app.mcp_client import MCPBridge, MCPFailure
from app.orchestration.store import AssistantStore
from app.orchestration.supervisor import Supervisor
from app.repository import PostgresRepository
from app.timezones import zone
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


async def test_tools_in_review_use_signed_stdio_and_scoped_database(database, tmp_path):
    settings, _, _, _ = database
    admin = await create_pool(settings)
    password = secrets.token_urlsafe(32)
    runtime_settings = replace(
        settings,
        postgres_user=RUNTIME,
        postgres_password=password,
        postgres_rls=True,
        itmo_state_dir=str(tmp_path),
    )
    mcp = MCPBridge(runtime_settings, secrets.token_urlsafe(32))
    runtime = None
    now = datetime.now(UTC)
    today = now.astimezone(zone("Europe/Moscow")).date()
    try:
        await install(admin, password)
        await admin.execute(
            'INSERT INTO personal_schedule_lessons(user_id,date,"start","end",title,location) '
            "VALUES(101,$1,'10:00','11:30','Синтетическая пара','301')",
            today,
        )
        await admin.execute(
            "INSERT INTO personal_tasks(id,user_id,text,due_at,timezone) "
            "VALUES($1,101,'Синтетический дедлайн',$2,'Europe/Moscow')",
            uuid.uuid4(),
            now + timedelta(days=1),
        )
        await admin.execute(
            "INSERT INTO user_materials(doc_id,user_id,title,content,created_at) "
            "VALUES('fixture-only',101,'Личный конспект','Синтетический орбитрон',$1)",
            now,
        )
        runtime = await create_pool(runtime_settings)
        assert await mcp.start()
        llm = Mock(complete=AsyncMock(side_effect=LLMError("synthetic_outage")))
        repository = PostgresRepository(runtime)
        service = Supervisor(
            runtime_settings,
            AssistantStore(runtime),
            Assistant(runtime_settings, repository, llm),
            mcp,
        )
        with owner_scope(101):
            await AgentStore(runtime).timezone(101, "Europe/Moscow")
            async with repository.session(101) as conversation:
                await conversation.change_mode("review")
            for key, text, expected in [
                ("day", "какие у меня пары седня?", "Синтетическая пара"),
                ("week", "расписание на неделю", "Синтетическая пара"),
                ("tasks", "шо по дедлайнам?", "Синтетический дедлайн"),
                ("catalog", "покажи мои материалы", "Личный конспект"),
                ("search", "найди в материалах орбитрон", "Синтетический орбитрон"),
            ]:
                assert expected in (await service.respond(101, text, key)).content
            pending = await service.respond(
                101, "Напомни завтра в 18:00 проверить отчёт", "reminder"
            )
            assert (
                pending.pending_id
                and await runtime.fetchval("SELECT count(*) FROM agent_reminders") == 0
            )
            with pytest.raises(MCPFailure, match="confirmation_required"):
                await mcp.call(
                    "add_reminder",
                    {"text": "Проверка", "remind_at": (now + timedelta(days=1)).isoformat()},
                    owner=101,
                    action=pending.pending_id,
                )
            assert "создано" in (await service.confirm(101, pending.pending_id)).content
            assert "уже создано" in (await service.confirm(101, pending.pending_id)).content
            assert await runtime.fetchval("SELECT count(*) FROM agent_reminders") == 1
            before = datetime.now(UTC) + timedelta(minutes=2)
            relative = await service.respond(
                101, "Напомни мне через пару минут проверить чай", "relative"
            )
            after = datetime.now(UTC) + timedelta(minutes=2)
            moment = await runtime.fetchval(
                "SELECT remind_at FROM agent_pending WHERE id=$1", relative.pending_id
            )
            assert before <= moment <= after
            assert await runtime.fetchval("SELECT count(*) FROM agent_reminders") == 1
            assert "создано" in (await service.confirm(101, relative.pending_id)).content
            assert await runtime.fetchval("SELECT count(*) FROM agent_reminders") == 2
            short = await service.respond(101, "Напомни через 10 секунд проверить чай", "short")
            await runtime.execute(
                "UPDATE agent_pending SET remind_at=$2 WHERE id=$1",
                short.pending_id,
                datetime.now(UTC) - timedelta(seconds=1),
            )
            assert "истёк" in (await service.confirm(101, short.pending_id)).content
            assert await runtime.fetchval("SELECT count(*) FROM agent_reminders") == 2
            async with repository.session(101) as conversation:
                assert (await conversation.profile()).mode == "review"
        with owner_scope(202):
            assert (
                "Синтетическая пара"
                not in (await service.respond(202, "Пары сегодня?", "day")).content
            )
            assert (
                "Личный конспект"
                not in (await service.respond(202, "Покажи материалы", "catalog")).content
            )
            assert await runtime.fetchval("SELECT count(*) FROM agent_reminders") == 0
        assert llm.complete.await_count == 1  # Only the material summary was attempted.
    finally:
        await mcp.close()
        if runtime:
            await runtime.close()
        await admin.close()
