"""Реальные SQL-блокировки и stdio MCP; только отдельный тестовый контейнер."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent import Agent
from app.agent_store import ActionError, AgentStore
from app.db import create_pool
from app.llm import Completion, ToolCall
from app.mcp_client import MCPBridge, MCPFailure
from app.migrations import migrate
from tests.test_integration import database as database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Включите RUN_INTEGRATION=1 для отдельной PostgreSQL",
    ),
]


async def test_agent_real_postgres_update_and_confirmation_races_restart(database):
    settings, docker, ready, name = database
    pool = await create_pool(settings)
    bridge = MCPBridge(settings, "integration-only-context-secret-" + uuid.uuid4().hex)
    now = datetime.now(UTC)
    moment = (now + timedelta(days=1)).replace(second=0, microsecond=0)
    arguments = {
        "text": "Отправить отчёт",
        "remind_at": moment.astimezone(
            __import__("zoneinfo").ZoneInfo("Europe/Moscow")
        ).isoformat(),
    }
    try:
        await migrate(pool)
        await migrate(pool)
        assert await bridge.start()
        store = AgentStore(pool)
        await store.timezone(101, "Europe/Moscow")
        await store.timezone(202, "Asia/Tbilisi")
        agent = Agent(settings, store, Mock(), bridge, clock=lambda: now)
        agent.choose = AsyncMock(
            return_value=Completion(
                "", "fake", "fake", tool_calls=(ToolCall("call-test", "add_reminder", arguments),)
            )
        )
        a, b = await asyncio.gather(
            agent.respond(101, "Напомни завтра об отчёте", "message:100"),
            agent.respond(101, "Напомни завтра об отчёте", "message:100"),
        )
        assert a.pending_id == b.pending_id
        agent.choose.assert_awaited_once()
        assert await pool.fetchval("SELECT count(*) FROM agent_pending") == 1
        assert await pool.fetchval("SELECT count(*) FROM agent_requests") == 1
        assert await pool.fetchval("SELECT count(*) FROM agent_reminders") == 0
        with pytest.raises(ActionError, match="not_owned"):
            await store.confirmation(202, a.pending_id, now)
        assert await store.last_audit(202) is None
        with pytest.raises(MCPFailure, match="trusted_context_required"):
            await bridge.call("add_reminder", arguments)
        # Подпись host сама по себе не заменяет запись подтверждения в БД.
        with pytest.raises(MCPFailure, match="confirmation_required"):
            await bridge.call("add_reminder", arguments, owner=101, action=a.pending_id)
        first, second = await asyncio.gather(
            agent.confirm(101, a.pending_id), agent.confirm(101, a.pending_id)
        )
        assert "создано" in first.content and "создано" in second.content
        assert await pool.fetchval("SELECT count(*) FROM agent_reminders") == 1
        assert (
            await pool.fetchval("SELECT state FROM agent_pending WHERE id=$1", a.pending_id)
            == "completed"
        )
        # Сетевой повтор server-side idempotency возвращает тот же ID.
        repeated = await bridge.call("add_reminder", arguments, owner=101, action=a.pending_id)
        assert repeated["created"] is False
        assert repeated["id"] in first.content and repeated["id"] in second.content
        with pytest.raises(MCPFailure, match="action_mismatch"):
            await bridge.call(
                "add_reminder", {**arguments, "text": "Подмена"}, owner=101, action=a.pending_id
            )
        with pytest.raises(MCPFailure, match="confirmation_required"):
            await bridge.call("add_reminder", arguments, owner=202, action=a.pending_id)
        await bridge.close()
        await pool.close()
        await asyncio.to_thread(docker, "restart", name)
        await asyncio.to_thread(ready)
        pool = await create_pool(settings)
        restarted = AgentStore(pool)
        assert await restarted.timezone(101) == "Europe/Moscow"
        assert await restarted.timezone(202) == "Asia/Tbilisi"
        assert await pool.fetchval("SELECT count(*) FROM agent_reminders") == 1
        async with restarted.request(101, "message:100") as unit:
            cached = await unit.cached()
            assert cached["pending_id"] == a.pending_id
        assert (await restarted.last_audit(101))["status"] == "completed"
    finally:
        await bridge.close()
        await pool.close()


async def test_agent_real_postgres_expiry_cancel_supersede_and_rollback(database):
    settings, _, _, _ = database
    pool = await create_pool(settings)
    now = datetime.now(UTC)
    moment = now + timedelta(days=1)
    args = {"text": "Проверка", "remind_at": moment.isoformat()}
    try:
        await migrate(pool)
        store = AgentStore(pool)
        async with store.request(1, "first") as unit:
            first = await unit.prepare(args, "Europe/Moscow", moment, now)
        async with store.request(1, "second") as unit:
            second = await unit.prepare(args, "Europe/Moscow", moment, now)
        assert (
            await pool.fetchval("SELECT state FROM agent_pending WHERE id=$1", first)
            == "superseded"
        )
        with pytest.raises(ActionError, match="superseded"):
            await store.confirmation(1, first, now)
        row = await store.confirmation(1, second, now, cancel=True)
        assert row["state"] == "cancelled"
        with pytest.raises(ActionError, match="confirmation_required"):
            await store.create_reminder(1, second, args, now)
        async with store.request(1, "expired") as unit:
            expired = await unit.prepare(args, "Europe/Moscow", moment, now)
        row = await store.confirmation(1, expired, now + timedelta(minutes=5))
        assert row["state"] == "expired"
        with pytest.raises(RuntimeError):
            async with store.request(2, "rollback") as unit:
                await unit.prepare(args, "Europe/Moscow", moment, now)
                raise RuntimeError("simulated failure")
        assert await pool.fetchval("SELECT count(*) FROM agent_pending WHERE user_id=2") == 0
        assert await pool.fetchval("SELECT count(*) FROM agent_reminders") == 0
    finally:
        await pool.close()
