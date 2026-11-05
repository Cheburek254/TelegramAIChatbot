"""Настоящая PostgreSQL; Telegram/LLM/MCP заменены, время управляемое."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from app.agent_store import AgentStore
from app.db import create_pool
from app.migrations import migrate
from app.orchestration.notifications import Notifications
from app.orchestration.scheduler import Scheduler
from app.orchestration.store import AssistantStore
from app.orchestration.tracing import Trace, span
from app.planner import Planner, task_intent
from app.planner_store import PlannerStore
from tests.test_integration import database as database
from tests.test_supervisor import CITY, bridge

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Отдельная PostgreSQL"),
]


async def test_outbox_concurrency_consent_boundaries_retries_restart_and_trace_privacy(database):
    settings, docker, ready, name = database
    pool = await create_pool(settings)
    now = datetime(2026, 10, 12, 5, tzinfo=UTC)
    sender = Mock(send_message=AsyncMock())
    raw = bridge()

    async def tool(name, args, **kw):
        if name == "get_schedule":
            return {"date": args["date"], "lessons": [], "source": "synthetic"}
        if name == "get_weather":
            raise OSError("synthetic weather failure")
        return {"status": "not_found", "source": "local_materials", "hits": []}

    raw.call.side_effect = tool
    store = AssistantStore(pool)
    scheduler = Scheduler(store, raw, sender, clock=lambda: now, briefing_timeout=1)

    async def profile(owner, enabled=False):
        await store.contact(owner, owner)
        assert await store.consent(owner, True) is False
        await AgentStore(pool).timezone(owner, "Europe/Moscow")
        assert await store.consent(owner, True) is False
        pending = await store.city_proposal(owner, CITY, now)
        assert not await store.confirm_city(owner + 1, pending, now)
        assert await store.confirm_city(owner, pending, now)
        assert not await store.confirm_city(owner, pending, now)
        if enabled:
            assert await store.consent(owner, True)
        return await store.profile(owner)

    async def reminder(owner, due):
        identity, action = uuid.uuid4(), uuid.uuid4()
        await pool.execute(
            "INSERT INTO agent_pending(id,user_id,request_key,text,remind_at,timezone,"
            "expires_at,state) "
            "VALUES($1,$2,$3,'SYNTHETIC_PRIVATE',$4,'Europe/Moscow',$5,'completed')",
            action,
            owner,
            str(action),
            due,
            now + timedelta(minutes=5),
        )
        await pool.execute(
            "INSERT INTO agent_reminders(id,action_id,user_id,text,remind_at,timezone) "
            "VALUES($1,$2,$3,'SYNTHETIC_PRIVATE',$4,'Europe/Moscow')",
            identity,
            action,
            owner,
            due,
        )
        return identity

    async def state(identity):
        return await pool.fetchrow("SELECT * FROM assistant_outbox WHERE id=$1", identity)

    try:
        await migrate(pool)
        await migrate(pool)
        await profile(101)
        with pytest.raises(ValueError):
            await store.contact(101, 999)
        past_city = await store.city_proposal(101, CITY, now - timedelta(minutes=6))
        assert not await store.confirm_city(101, past_city, now)
        identity = await reminder(101, now)
        await store.enqueue_reminders(now)
        await store.enqueue_reminders(now)
        await asyncio.gather(store.enqueue_reminders(now), store.enqueue_reminders(now))
        assert (
            await pool.fetchval("SELECT count(*) FROM assistant_outbox WHERE id=$1", identity) == 1
        )
        await asyncio.gather(scheduler.deliver_one(), scheduler.deliver_one())
        assert (await state(identity))["state"] == "sent" and sender.send_message.await_count == 1
        # A late worker with the old claim must not overwrite a terminal state.
        old_event = dict(await state(identity))
        async with store.delivery(old_event, now) as target:
            assert target is None
        assert (await state(identity))["state"] == "sent"
        assert sender.send_message.call_args.args[0] == 101
        sender.send_message.reset_mock()
        missed = await reminder(101, now - timedelta(minutes=15, microseconds=1))
        boundary = await reminder(101, now - timedelta(minutes=15))
        future = await reminder(101, now + timedelta(days=1))
        await store.enqueue_reminders(now)
        await scheduler.deliver_one()
        assert (await state(missed))["state"] == "missed"
        assert (await state(boundary))["state"] == "sent"
        assert "задержкой" in sender.send_message.call_args.args[1]
        assert not await state(future)
        sender.send_message.reset_mock()
        await profile(102)
        permanent = await reminder(102, now)
        sender.send_message.side_effect = TelegramForbiddenError(
            method=SendMessage(chat_id=102, text="X"), message="blocked"
        )
        await scheduler.prepare_due()
        await scheduler.deliver_one()
        assert (await state(permanent))["state"] == "failed"
        assert (await state(permanent))["attempts"] == 1
        sender.send_message.reset_mock()
        sender.send_message.side_effect = OSError("temporary")
        retry = await reminder(102, now)
        await scheduler.prepare_due()
        await scheduler.deliver_one()
        assert (await state(retry))["state"] == "retry"
        assert not await scheduler.deliver_one()
        now += timedelta(seconds=30)
        await scheduler.deliver_one()
        now += timedelta(seconds=120)
        await scheduler.deliver_one()
        assert (await state(retry))["state"] == "failed" and (await state(retry))["attempts"] == 3
        assert sender.send_message.await_count == 3
        sender.send_message.reset_mock()
        sender.send_message.side_effect = None
        unfinished = await reminder(102, now)
        await scheduler.prepare_due()
        lease = await store.claim(now)
        assert lease["id"] == unfinished
        await pool.close()
        await asyncio.to_thread(docker, "restart", name)
        await asyncio.to_thread(ready)
        pool = await create_pool(settings)
        store = AssistantStore(pool)
        scheduler = Scheduler(store, raw, sender, clock=lambda: now, briefing_timeout=1)
        now += timedelta(minutes=5, seconds=1)
        await scheduler.deliver_one()
        assert (await state(unfinished))["state"] == "sent" and (await state(unfinished))[
            "attempts"
        ] == 2
        assert sender.send_message.await_count == 1
        # Explicit consent and one local-day event even under concurrent preparation.
        p = await profile(106, True)
        due = datetime(2026, 10, 12, 5, tzinfo=UTC)
        await store.enqueue_briefing(p, due, due.date())
        await store.enqueue_briefing(p, due, due.date())
        await asyncio.gather(
            store.enqueue_briefing(p, due, due.date()), store.enqueue_briefing(p, due, due.date())
        )
        assert await pool.fetchval("SELECT count(*) FROM assistant_outbox WHERE user_id=106") == 1
        await store.consent(106, False)
        before = sender.send_message.await_count
        assert not await scheduler.deliver_one()
        assert sender.send_message.await_count == before
        await profile(107, True)
        await scheduler.prepare_due()
        await scheduler.deliver_one()
        message = sender.send_message.call_args.args[1]
        assert "занятий нет" in message and "недоступны" in message and "08:" in message
        assert sender.send_message.call_args.args[0] == 107
        await scheduler.prepare_due()
        assert not await scheduler.deliver_one()
        assert await pool.fetchval("SELECT count(*) FROM assistant_outbox WHERE user_id=107") == 1
        # Revoke after preparation begins, before final delivery check.
        await profile(109, True)

        async def revoke(*args):
            await store.consent(109, False)
            return "SYNTHETIC_PRIVATE"

        scheduler.briefing.prepare = AsyncMock(side_effect=revoke)
        before = sender.send_message.await_count
        await scheduler.prepare_due()
        await scheduler.deliver_one()
        assert sender.send_message.await_count == before
        now = datetime(2026, 10, 12, 6, 0, 0, 1, tzinfo=UTC)
        await profile(110, True)
        await scheduler.prepare_due()
        assert not await scheduler.deliver_one()
        assert (
            await pool.fetchval("SELECT state FROM assistant_outbox WHERE user_id=110") == "missed"
        )
        # Exactly 60 minutes is still within the briefing window.
        now = datetime(2026, 10, 12, 6, tzinfo=UTC)
        await profile(111, True)
        scheduler = Scheduler(store, raw, sender, clock=lambda: now, briefing_timeout=1)
        await scheduler.prepare_due()
        await scheduler.deliver_one()
        assert await pool.fetchval("SELECT state FROM assistant_outbox WHERE user_id=111") == "sent"
        async with Trace(store, 101, clock=lambda: now) as trace:
            async with span("knowledge", "search", text="SYNTHETIC_PRIVATE", token="SECRET"):
                pass
        row, spans = await store.trace(101, trace.id)
        assert row and spans
        assert await store.trace(102, trace.id) == (None, [])
        raw_trace = await pool.fetch("SELECT attributes FROM assistant_spans")
        assert "SYNTHETIC_PRIVATE" not in repr(raw_trace) and "SECRET" not in repr(raw_trace)
    finally:
        await pool.close()


async def test_daily_reminders_repeat_and_confirmed_stop_invalidates_claim(database):
    settings, *_ = database
    pool = await create_pool(settings)
    now = datetime(2026, 10, 12, 15, tzinfo=UTC)
    owner, stranger = 301, 302
    store = AssistantStore(pool)
    sender = Mock(send_message=AsyncMock())
    scheduler = Scheduler(store, bridge(), sender, clock=lambda: now)
    notifications = Notifications(store, None, clock=lambda: now)
    try:
        await migrate(pool)
        await migrate(pool)
        for user in (owner, stranger):
            await store.contact(user, user)
            await AgentStore(pool).timezone(user, "Europe/Moscow")

        for invalid in ("add 24:00 test", "add 19:00", "add 19:00 " + "x" * 501):
            assert (await notifications.add_daily(owner, invalid, invalid))[1] is None

        _, cancelled = await notifications.add_daily(owner, "cancel", "add 19:00 Cancel me")
        assert await store.confirm_daily(owner, cancelled, now, cancel=True) == "cancelled"
        assert await store.confirm_daily(owner, cancelled, now) == "invalid"
        _, expired = await notifications.add_daily(owner, "expired", "add 19:00 Too late")
        assert await store.confirm_daily(owner, expired, now + timedelta(minutes=5)) == "invalid"

        args = "add 19:00 Synthetic daily text"
        _, series = await notifications.add_daily(owner, "message", args)
        assert (await notifications.add_daily(owner, "message", args))[1] == series
        assert not await notifications.list_daily(owner)
        assert await store.confirm_daily(stranger, series, now) == "invalid"
        assert sorted(
            await asyncio.gather(
                store.confirm_daily(owner, series, now), store.confirm_daily(owner, series, now)
            )
        ) == ["added", "invalid"]
        assert not await notifications.list_daily(stranger)
        assert (await notifications.stop_daily(stranger, "foreign", series))[1] is None
        await AgentStore(pool).timezone(owner, "America/New_York")
        await scheduler.tick()
        sender.send_message.assert_not_awaited()

        now += timedelta(hours=1)
        await asyncio.gather(scheduler.prepare_due(), scheduler.prepare_due())
        await asyncio.gather(scheduler.deliver_one(), scheduler.deliver_one())
        assert sender.send_message.await_count == 1
        assert sender.send_message.call_args.args[0] == owner
        assert "19:00 (Europe/Moscow)" in sender.send_message.call_args.args[1]
        assert "Synthetic daily text" in sender.send_message.call_args.args[1]
        # New scheduler instance, same database. The next date gets its own event.
        scheduler = Scheduler(AssistantStore(pool), bridge(), sender, clock=lambda: now)
        await scheduler.tick()
        assert sender.send_message.await_count == 1
        now += timedelta(days=1)
        await scheduler.tick()
        assert sender.send_message.await_count == 2

        now += timedelta(days=1)
        await scheduler.prepare_due()
        event = await store.claim(now)
        assert event["daily_reminder_id"] == series
        _, pending = await notifications.stop_daily(owner, "button", series)
        assert await store.confirm_daily(stranger, pending, now) == "invalid"
        assert await store.confirm_daily(owner, pending, now) == "stopped"
        assert await store.confirm_daily(owner, pending, now) == "invalid"
        assert not await notifications.list_daily(owner)
        assert (
            await pool.fetchval("SELECT state FROM assistant_outbox WHERE id=$1", event["id"])
            == "cancelled"
        )
        async with store.delivery(event, now) as target:
            assert target is None
        now += timedelta(days=1)
        await scheduler.tick()
        assert sender.send_message.await_count == 2
    finally:
        await pool.close()


async def test_planner_tasks_briefing_and_rescheduled_reminders(database):
    settings, *_ = database
    pool = await create_pool(settings)
    owner, stranger = 401, 402
    now = datetime(2026, 11, 6, 13, tzinfo=UTC)
    store = AssistantStore(pool)
    planner_store = PlannerStore(pool)
    notifications = Notifications(store, None, clock=lambda: now)
    planner = Planner(planner_store, notifications, "Europe/Moscow", clock=lambda: now)
    sender = Mock(send_message=AsyncMock())
    scheduler = Scheduler(store, bridge(), sender, clock=lambda: now)
    try:
        await migrate(pool)
        await migrate(pool)
        for user in (owner, stranger):
            await store.contact(user, user)
            await AgentStore(pool).timezone(user, "Europe/Moscow")
        _, task = await planner.add_task(owner, "task", task_intent("Сдать отчёт в 18:00"))
        assert await planner_store.confirm(stranger, task, now) == ("invalid", None, None)
        assert await planner_store.confirm(owner, task, now) == ("added", "t", task)
        assert await planner_store.confirm(owner, task, now) == ("invalid", None, None)
        assert not await planner_store.active(stranger, "t", 0)
        content = await scheduler.briefing.prepare(
            owner, {"timezone": "Europe/Moscow", "city": CITY}, now
        )
        assert "📌 Задачи" in content and "18:00 · Сдать отчёт" in content
        original = await planner_store.item(owner, "t", task)
        _, move = await planner.change(owner, "task-move", "t", original, "day")
        _, stale = await planner.change(owner, "stale", "t", original, "done")
        assert await planner_store.confirm(owner, move, now) == ("moved", "t", task)
        assert await planner_store.confirm(owner, stale, now) == ("stale", "t", task)
        moved = await planner_store.item(owner, "t", task)
        assert moved["due_at"] == original["due_at"] + timedelta(days=1)
        _, done = await planner.change(owner, "task-done", "t", moved, "done")
        assert await planner_store.confirm(owner, done, now) == ("done", "t", task)
        assert not await planner_store.briefing_tasks(owner, now + timedelta(days=3))

        action, reminder = uuid.uuid4(), uuid.uuid4()
        await pool.execute(
            "INSERT INTO agent_pending(id,user_id,request_key,text,remind_at,timezone,"
            "expires_at,state) "
            "VALUES($1,$2,'one-off','Synthetic reminder',$3,'Europe/Moscow',$3,'completed')",
            action,
            owner,
            now,
        )
        await pool.execute(
            "INSERT INTO agent_reminders(id,action_id,user_id,text,remind_at,timezone) "
            "VALUES($1,$2,$3,'Synthetic reminder',$4,'Europe/Moscow')",
            reminder,
            action,
            owner,
            now,
        )
        await scheduler.prepare_due()
        claimed = await store.claim(now)
        assert claimed["reminder_id"] == reminder
        row = await planner_store.item(owner, "r", reminder)
        _, reschedule = await planner.change(owner, "r-move", "r", row, "hour")
        assert await planner_store.confirm(owner, reschedule, now) == ("moved", "r", reminder)
        async with store.delivery(claimed, now) as target:
            assert target is None
        await scheduler.tick()
        sender.send_message.assert_not_awaited()
        now += timedelta(hours=1)
        await scheduler.tick()
        assert sender.send_message.await_count == 1
        assert "Synthetic reminder" in sender.send_message.call_args.args[1]
        assert (
            sender.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0].text
            == "✅ Готово"
        )
        row = await planner_store.item(owner, "r", reminder)
        _, done = await planner.change(owner, "r-done", "r", row, "done")
        assert await planner_store.confirm(owner, done, now) == ("done", "r", reminder)
        assert not await planner_store.active(owner, "r", 0)

        _, series = await notifications.add_daily(owner, "daily", "add 18:00 Synthetic series")
        assert await store.confirm_daily(owner, series, now) == "added"
        now += timedelta(hours=1)
        await scheduler.prepare_due()
        old_occurrence = await store.claim(now)
        assert old_occurrence["daily_reminder_id"] == series
        row = await planner_store.item(owner, "d", series)
        _, reschedule = await planner.change(owner, "d-move", "d", row, "hour")
        assert await planner_store.confirm(owner, reschedule, now) == ("moved", "d", series)
        async with store.delivery(old_occurrence, now) as target:
            assert target is None
        now += timedelta(hours=1)
        await scheduler.tick()
        assert sender.send_message.await_count == 2
        assert "19:00" in sender.send_message.call_args.args[1]
        assert (
            await pool.fetchval(
                "SELECT reminder_version FROM assistant_outbox "
                "WHERE daily_reminder_id=$1 AND state='sent'",
                series,
            )
            == 2
        )
        row = await planner_store.item(owner, "d", series)
        _, reschedule = await planner.change(owner, "d-move-after", "d", row, "hour")
        assert await planner_store.confirm(owner, reschedule, now) == ("moved", "d", series)
        now += timedelta(hours=1)
        await scheduler.tick()
        assert sender.send_message.await_count == 2
        now += timedelta(days=1)
        await scheduler.tick()
        assert sender.send_message.await_count == 3
        assert "20:00" in sender.send_message.call_args.args[1]
        now += timedelta(hours=23)
        row = await planner_store.item(owner, "d", series)
        _, done = await planner.change(owner, "d-done", "d", row, "done")
        assert await planner_store.confirm(owner, done, now) == ("done_today", "d", series)
        now += timedelta(hours=1)
        await scheduler.tick()
        assert sender.send_message.await_count == 3
        now += timedelta(days=1)
        await scheduler.tick()
        assert sender.send_message.await_count == 4
    finally:
        await pool.close()
