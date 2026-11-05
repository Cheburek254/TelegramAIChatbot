import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from app.db import create_pool
from app.migrations import migrate
from app.repository import PostgresRepository
from app.style_store import SpeechStyleStore
from tests.test_integration import database as database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Отдельная PostgreSQL"),
]


async def test_style_owner_ttl_replay_persistence_and_existing_mode_history(database):
    settings, *_ = database
    pool = await create_pool(settings)
    now = datetime(2026, 11, 2, 8, tzinfo=UTC)
    store, repository = SpeechStyleStore(pool), PostgresRepository(pool)
    try:
        await migrate(pool)
        await migrate(pool)
        async with repository.session(101) as conversation:
            await conversation.change_mode("review")
            await conversation.change_temperature(0.7)
            await conversation.save_turn("Synthetic question", "Synthetic answer", 20)
        identity = await store.propose(101, "on", "yiddish", now)
        assert await store.confirm(202, identity, now) == "stale"
        results = await asyncio.gather(*[store.confirm(101, identity, now) for _ in range(3)])
        assert results.count("yiddish") == 1 and results.count("stale") == 2
        assert await store.propose(101, "on", "yiddish", now) == identity
        async with repository.session(101) as conversation:
            profile = await conversation.profile()
            assert (profile.mode, profile.temperature, profile.speech_style) == (
                "review",
                0.7,
                "yiddish",
            )
            assert len(await conversation.history(20)) == 2
            await conversation.change_mode("translate")
            await conversation.reset()
            assert (await conversation.profile()).speech_style == "yiddish"
        async with repository.session(202) as conversation:
            assert (await conversation.profile()).speech_style == "default"
        expired = await store.propose(101, "expire", "plain", now)
        assert await store.confirm(101, expired, now + timedelta(minutes=5)) == "expired"
        cancelled = await store.propose(101, "cancel", "plain", now)
        assert await store.confirm(101, cancelled, now, cancel=True) == "cancelled"
        off = await store.propose(101, "off", "plain", now)
        assert await store.confirm(101, off, now) == "plain"
        await migrate(pool)
        async with repository.session(101) as conversation:
            profile = await conversation.profile()
            assert (
                profile.mode == "translate"
                and profile.temperature == 0.7
                and profile.speech_style == "plain"
            )
    finally:
        await pool.close()
