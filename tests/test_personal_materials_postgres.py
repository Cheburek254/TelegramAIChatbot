import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from app.agent_store import AgentStore
from app.db import create_pool
from app.material_service import PersonalMaterials
from app.material_store import MaterialStore
from app.mcp_client import MCPBridge, MCPFailure
from app.migrations import migrate
from app.orchestration.store import AssistantStore
from tests.test_integration import database as database
from tests.test_supervisor import CITY

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Отдельная PostgreSQL"),
]


async def test_private_materials_confirm_replay_quota_and_signed_mcp(database):
    settings, *_ = database
    pool = await create_pool(settings)
    store = MaterialStore(pool)
    now = datetime(2026, 11, 2, 6, tzinfo=UTC)
    service = PersonalMaterials(store, clock=lambda: now)
    bridge = MCPBridge(settings, "synthetic-secret-for-private-materials")
    try:
        await migrate(pool)
        await migrate(pool)
        _, pending = await service.add(
            101, "upload", "Личный конспект", "Ксенобиология изучает жизнь."
        )
        assert not await store.documents(101)
        assert await store.confirm(202, pending, now) == "stale"
        results = await asyncio.gather(*[store.confirm(101, pending, now) for _ in range(3)])
        assert results.count("material_add") == 1 and results.count("stale") == 2
        assert len(await store.documents(101)) == 1 and not await store.documents(202)
        _, repeated = await service.add(
            101, "upload", "Личный конспект", "Ксенобиология изучает жизнь."
        )
        assert repeated == pending
        assert await store.confirm(101, repeated, now) == "stale"
        _, expired = await service.add(101, "expired", "Другой", "Содержимое")
        assert await store.confirm(101, expired, now + timedelta(minutes=5)) == "expired"
        assert (
            await pool.fetchval(
                "SELECT payload::text FROM user_content_pending WHERE id=$1", expired
            )
            == "{}"
        )
        assert await bridge.start()
        own = await bridge.call("search_materials", {"query": "ксенобиология"}, owner=101)
        assert len(own["hits"]) == 1 and own["hits"][0]["title"] == "Личный конспект"
        assert (await bridge.call("search_materials", {"query": "ксенобиология"}, owner=202))[
            "hits"
        ] == []
        assert (await bridge.call("search_materials", {"query": "ксенобиология"}))["hits"] == []
        assert len((await bridge.read_catalog(101))["documents"]) == 4
        assert len((await bridge.read_catalog(202))["documents"]) == 3
        assert len((await bridge.read_catalog())["documents"]) == 3
        with pytest.raises(MCPFailure):
            await bridge.call(
                "search_materials", {"query": "ксенобиология", "owner": 101}, owner=202
            )
        doc_id = own["hits"][0]["doc_id"]
        _, deletion = await service.command(101, "delete", "delete " + doc_id)
        assert await store.confirm(202, deletion, now) == "stale"
        assert await store.confirm(101, deletion, now) == "material_delete"
        assert (await bridge.call("search_materials", {"query": "ксенобиология"}, owner=101))[
            "hits"
        ] == []
        # Repository quota remains enforced even if callers bypass the service.
        for index in range(20):
            pending = await store.proposal(
                101, f"quota-{index}", "material_add", {"title": "X", "content": "Y"}, now
            )
            assert await store.confirm(101, pending, now) == "material_add"
        pending = await store.proposal(
            101, "over-quota", "material_add", {"title": "X", "content": "Y"}, now
        )
        assert await store.confirm(101, pending, now) == "quota"
        assert len(await store.documents(101)) == 20
    finally:
        await bridge.close()
        await pool.close()


async def test_time_change_invalidates_queue_and_never_resends_sent_day(database):
    settings, *_ = database
    pool = await create_pool(settings)
    now = datetime(2026, 11, 2, 5, tzinfo=UTC)
    store, content = AssistantStore(pool), MaterialStore(pool)
    try:
        await migrate(pool)
        await store.contact(101, 101)
        await AgentStore(pool).timezone(101, "Europe/Moscow")
        city = await store.city_proposal(101, CITY, now)
        assert await store.confirm_city(101, city, now)
        assert await store.consent(101, True)
        before = await store.profile(101)
        assert before["briefing_minute"] == 480
        await store.enqueue_briefing(before, now, now.date())
        old_event = await store.claim(now)
        pending = await content.proposal(101, "time1", "briefing_time", {"minute": 570}, now)
        assert await content.confirm(202, pending, now) == "stale"
        assert (await store.profile(101))["briefing_minute"] == 480
        assert await content.confirm(101, pending, now) == "briefing_time"
        after = await store.profile(101)
        assert after["briefing_minute"] == 570 and after["briefing_enabled"]
        async with store.delivery(old_event, now) as target:
            assert target is None
        new_due = now + timedelta(minutes=90)
        await store.enqueue_briefing(after, new_due, now.date())
        assert await store.claim(now) is None
        event = await store.claim(new_due)
        assert event["consent_version"] == after["consent_version"]
        async with store.delivery(event, new_due) as target:
            assert target[0] == 101
            await store.complete(event, new_due, conn=target[1])
        pending = await content.proposal(101, "time2", "briefing_time", {"minute": 720}, now)
        assert await content.confirm(101, pending, now) == "briefing_time"
        await store.enqueue_briefing(await store.profile(101), now + timedelta(hours=4), now.date())
        assert await store.claim(now + timedelta(hours=4)) is None
        assert await pool.fetchval("SELECT count(*) FROM assistant_outbox WHERE state='sent'") == 1
    finally:
        await pool.close()
