"""Хранилище профилей, трасс и исходящих событий."""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS assistant_profiles (
 user_id BIGINT PRIMARY KEY, destination_chat_id BIGINT,
 city JSONB, briefing_enabled BOOLEAN NOT NULL DEFAULT false,
 consent_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assistant_city_pending (
 id UUID PRIMARY KEY, user_id BIGINT NOT NULL, city JSONB NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS assistant_requests (
 user_id BIGINT NOT NULL, request_key TEXT NOT NULL, response TEXT NOT NULL,
 pending_id UUID, trace_id UUID NOT NULL, PRIMARY KEY(user_id,request_key)
);
CREATE TABLE IF NOT EXISTS assistant_action_auth (
 action_id UUID PRIMARY KEY, user_id BIGINT NOT NULL, request_digest TEXT NOT NULL,
 route TEXT NOT NULL CHECK(route='schedule'), intent TEXT NOT NULL CHECK(intent='reminder')
);
CREATE TABLE IF NOT EXISTS assistant_traces (
 id UUID PRIMARY KEY, user_id BIGINT NOT NULL, request_id UUID NOT NULL,
 kind TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assistant_spans (
 id UUID PRIMARY KEY, trace_id UUID NOT NULL REFERENCES assistant_traces(id) ON DELETE CASCADE,
 parent_id UUID, component TEXT NOT NULL, operation TEXT NOT NULL,
 started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ NOT NULL,
 status TEXT NOT NULL, attributes JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS assistant_trace_owner_time ON assistant_traces(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS assistant_outbox (
 id UUID PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, user_id BIGINT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('reminder_delivery','daily_briefing')),
 due_at TIMESTAMPTZ NOT NULL, deadline TIMESTAMPTZ NOT NULL,
 consent_version INTEGER, reminder_id UUID, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
 available_at TIMESTAMPTZ NOT NULL, lease_until TIMESTAMPTZ, claim_token UUID,
 payload TEXT, error_code TEXT, trace_id UUID, sent_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS assistant_outbox_due ON assistant_outbox(state,available_at);
CREATE TABLE IF NOT EXISTS assistant_daily_reminders (
 id UUID PRIMARY KEY, user_id BIGINT NOT NULL,
 text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 500),
 minute INTEGER NOT NULL CHECK(minute BETWEEN 0 AND 1439),
 timezone TEXT NOT NULL, starts_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS assistant_daily_reminder_owner ON assistant_daily_reminders(user_id);
CREATE TABLE IF NOT EXISTS assistant_daily_pending (
 id UUID PRIMARY KEY, user_id BIGINT NOT NULL, request_key TEXT NOT NULL,
 reminder_id UUID, text TEXT, minute INTEGER, timezone TEXT,
 expires_at TIMESTAMPTZ NOT NULL, consumed BOOLEAN NOT NULL DEFAULT false,
 UNIQUE(user_id,request_key)
);
ALTER TABLE assistant_outbox ADD COLUMN IF NOT EXISTS daily_reminder_id UUID;
ALTER TABLE assistant_outbox DROP CONSTRAINT IF EXISTS assistant_outbox_kind_check;
ALTER TABLE assistant_outbox ADD CONSTRAINT assistant_outbox_kind_check
 CHECK(kind IN ('reminder_delivery','daily_briefing','daily_reminder_delivery',
                'task_deadline','itmo_change'));
CREATE INDEX IF NOT EXISTS assistant_outbox_daily_series ON assistant_outbox(daily_reminder_id)
 WHERE daily_reminder_id IS NOT NULL;
"""

DAILY_REMINDER_LIMIT = 10


def decode(value):
    return json.loads(value) if isinstance(value, str) else value


class AssistantStore:
    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def guard(self, owner):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"supervisor:{owner}"
            )
            yield

    async def contact(self, owner, destination):
        if type(owner) is not int or owner <= 0 or destination != owner:
            raise ValueError("private_contact_required")
        await self.pool.execute(
            "INSERT INTO assistant_profiles(user_id,destination_chat_id) VALUES($1,$2) "
            "ON CONFLICT(user_id) DO UPDATE SET destination_chat_id=excluded.destination_chat_id",
            owner,
            destination,
        )

    async def profile(self, owner):
        row = await self.pool.fetchrow(
            "SELECT p.*,s.timezone FROM assistant_profiles p LEFT JOIN agent_settings s "
            "ON s.user_id=p.user_id WHERE p.user_id=$1",
            owner,
        )
        return dict(row) if row else None

    async def profiles(self):
        return await self.pool.fetch(
            "SELECT p.*,s.timezone FROM assistant_profiles p JOIN agent_settings s "
            "ON s.user_id=p.user_id WHERE p.briefing_enabled AND p.city IS NOT NULL "
            "AND p.destination_chat_id IS NOT NULL"
        )

    async def city_proposal(self, owner, city, now):
        identity = uuid.uuid4()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"city:{owner}"
            )
            await conn.execute("DELETE FROM assistant_city_pending WHERE user_id=$1", owner)
            await conn.execute(
                "INSERT INTO assistant_city_pending(id,user_id,city,expires_at) "
                "VALUES($1,$2,$3,$4)",
                identity,
                owner,
                json.dumps(city),
                now + timedelta(minutes=5),
            )
        return identity

    async def confirm_city(self, owner, identity, now, cancel=False):
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM assistant_city_pending WHERE user_id=$1 AND id=$2 FOR UPDATE",
                owner,
                identity,
            )
            if not row or row["expires_at"] <= now or row["state"] != "pending":
                return False
            await conn.execute(
                "UPDATE assistant_city_pending SET state=$2 WHERE id=$1",
                identity,
                "cancelled" if cancel else "completed",
            )
            if not cancel:
                await conn.execute(
                    "UPDATE assistant_profiles SET city=$2,briefing_enabled=false,"
                    "consent_version=consent_version+1 WHERE user_id=$1",
                    owner,
                    row["city"],
                )
            return not cancel

    async def consent(self, owner, enabled):
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM assistant_profiles WHERE user_id=$1 FOR UPDATE", owner
            )
            timezone = await conn.fetchval(
                "SELECT timezone FROM agent_settings WHERE user_id=$1", owner
            )
            if enabled and (
                not row or not row["city"] or not timezone or not row["destination_chat_id"]
            ):
                return False
            await conn.execute(
                "UPDATE assistant_profiles SET briefing_enabled=$2,"
                "consent_version=consent_version+1 WHERE user_id=$1",
                owner,
                enabled,
            )
            if not enabled:
                await conn.execute(
                    "UPDATE assistant_outbox SET state='cancelled',payload=NULL WHERE user_id=$1 "
                    "AND kind='daily_briefing' AND state NOT IN ('sent','missed','failed')",
                    owner,
                )
            return True

    async def cached(self, owner, key):
        return await self.pool.fetchrow(
            "SELECT * FROM assistant_requests WHERE user_id=$1 AND request_key=$2", owner, key
        )

    async def save_reply(self, owner, key, reply, trace):
        await self.pool.execute(
            "INSERT INTO assistant_requests(user_id,request_key,response,pending_id,trace_id) "
            "VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
            owner,
            key,
            reply.content,
            reply.pending_id,
            trace,
        )

    async def authorize_action(self, owner, identity, digest):
        await self.pool.execute(
            "INSERT INTO assistant_action_auth(action_id,user_id,request_digest,route,intent) "
            "VALUES($1,$2,$3,'schedule','reminder') ON CONFLICT DO NOTHING",
            identity,
            owner,
            digest,
        )

    async def authorized_action(self, owner, identity):
        return bool(
            await self.pool.fetchval(
                "SELECT 1 FROM assistant_action_auth WHERE user_id=$1 AND action_id=$2 "
                "AND route='schedule' AND intent='reminder'",
                owner,
                identity,
            )
        )

    async def begin_trace(self, owner, identity, request, kind, now):
        await self.pool.execute(
            "INSERT INTO assistant_traces VALUES($1,$2,$3,$4,$5,'running')",
            identity,
            owner,
            request,
            kind,
            now,
        )

    async def span(self, identity, trace, parent, component, operation, start, end, status, attrs):
        await self.pool.execute(
            "INSERT INTO assistant_spans VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            identity,
            trace,
            parent,
            component,
            operation,
            start,
            end,
            status,
            json.dumps(attrs),
        )

    async def finish_trace(self, identity, status):
        await self.pool.execute(
            "UPDATE assistant_traces SET status=$2 WHERE id=$1", identity, status
        )

    async def trace(self, owner, identity=None):
        row = await self.pool.fetchrow(
            "SELECT * FROM assistant_traces WHERE user_id=$1 AND ($2::uuid IS NULL OR id=$2) "
            "ORDER BY created_at DESC LIMIT 1",
            owner,
            identity,
        )
        if not row:
            return None, []
        spans = await self.pool.fetch(
            "SELECT * FROM assistant_spans WHERE trace_id=$1 ORDER BY started_at,id", row["id"]
        )
        return row, spans

    async def enqueue_reminders(self, now):
        await self.pool.execute(
            "INSERT INTO assistant_outbox(id,event_key,user_id,kind,due_at,deadline,"
            "reminder_id,available_at,reminder_version) "
            "SELECT CASE WHEN r.version=1 THEN r.id ELSE gen_random_uuid() END,"
            "'reminder:'||r.id::text||CASE WHEN r.version=1 THEN '' ELSE ':'||r.version END,"
            "r.user_id,'reminder_delivery',r.remind_at,"
            "r.remind_at+interval '15 minutes',r.id,r.remind_at,r.version FROM agent_reminders r "
            "JOIN assistant_profiles p ON p.user_id=r.user_id WHERE r.remind_at<=$1 "
            "AND p.destination_chat_id IS NOT NULL AND r.completed_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM assistant_outbox o WHERE o.reminder_id=r.id "
            "AND o.due_at=r.remind_at AND o.state='sent') "
            "FOR SHARE OF r ON CONFLICT(event_key) DO NOTHING",
            now,
        )

    async def enqueue_task_alerts(self, now):
        await self.pool.execute(
            "INSERT INTO assistant_outbox(id,event_key,user_id,kind,due_at,deadline,"
            "task_id,available_at,reminder_version) "
            "SELECT gen_random_uuid(),'task:'||t.id::text||':'||t.version,"
            "t.user_id,'task_deadline',"
            "a.due,least(t.due_at,a.due+interval '15 minutes'),t.id,a.due,t.version "
            "FROM personal_tasks t JOIN assistant_profiles p USING(user_id) "
            "CROSS JOIN LATERAL (SELECT t.due_at-make_interval(mins=>t.notify_before) AS due) a "
            "WHERE p.destination_chat_id=t.user_id AND t.completed_at IS NULL "
            "AND t.notify_before IS NOT NULL AND t.due_at>$1 AND a.due<=$1 "
            "AND a.due>=$1-interval '15 minutes' AND NOT EXISTS ("
            "SELECT 1 FROM assistant_outbox o WHERE o.task_id=t.id AND o.due_at=a.due "
            "AND o.state='sent') ON CONFLICT(event_key) DO NOTHING",
            now,
        )

    async def daily_reminders(self, owner):
        return await self.pool.fetch(
            "SELECT * FROM assistant_daily_reminders WHERE user_id=$1 ORDER BY minute,id", owner
        )

    async def daily_reminder(self, owner, identity):
        return await self.pool.fetchrow(
            "SELECT * FROM assistant_daily_reminders WHERE user_id=$1 AND id=$2", owner, identity
        )

    async def daily_proposal(
        self, owner, key, now, *, identity=None, text=None, minute=None, timezone=None
    ):
        return await self.pool.fetchval(
            "INSERT INTO assistant_daily_pending(id,user_id,request_key,reminder_id,text,minute,"
            "timezone,expires_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8) "
            "ON CONFLICT(user_id,request_key) DO UPDATE SET request_key=excluded.request_key "
            "RETURNING id",
            uuid.uuid4(),
            owner,
            key,
            identity,
            text,
            minute,
            timezone,
            now + timedelta(minutes=5),
        )

    async def confirm_daily(self, owner, identity, now, *, cancel=False):
        async with self.pool.acquire() as conn, conn.transaction():
            # Same first lock as delivery. A successful stop serializes with a send in progress.
            destination = await conn.fetchval(
                "SELECT destination_chat_id FROM assistant_profiles WHERE user_id=$1 FOR UPDATE",
                owner,
            )
            if destination != owner:
                return "invalid"
            pending = await conn.fetchrow(
                "UPDATE assistant_daily_pending SET consumed=true "
                "WHERE user_id=$1 AND id=$2 AND NOT consumed AND expires_at>$3 RETURNING *",
                owner,
                identity,
                now,
            )
            if not pending:
                return "invalid"
            if cancel:
                return "cancelled"
            series = pending["reminder_id"]
            if series:
                deleted = await conn.fetchval(
                    "DELETE FROM assistant_daily_reminders WHERE user_id=$1 AND id=$2 RETURNING id",
                    owner,
                    series,
                )
                if not deleted:
                    return "invalid"
                await conn.execute(
                    "UPDATE assistant_outbox SET state='cancelled',payload=NULL "
                    "WHERE user_id=$1 AND daily_reminder_id=$2 "
                    "AND state IN ('pending','retry','working')",
                    owner,
                    series,
                )
                return "stopped"
            count = await conn.fetchval(
                "SELECT count(*) FROM assistant_daily_reminders WHERE user_id=$1", owner
            )
            if count >= DAILY_REMINDER_LIMIT:
                return "limit"
            await conn.execute(
                "INSERT INTO assistant_daily_reminders(id,user_id,text,minute,timezone,starts_at) "
                "VALUES($1,$2,$3,$4,$5,$6)",
                identity,
                owner,
                pending["text"],
                pending["minute"],
                pending["timezone"],
                now,
            )
            return "added"

    async def active_daily_reminders(self):
        return await self.pool.fetch(
            "SELECT r.* FROM assistant_daily_reminders r JOIN assistant_profiles p USING(user_id) "
            "WHERE p.destination_chat_id=r.user_id"
        )

    async def enqueue_daily_reminder(self, reminder, due, local_date):
        await self.pool.execute(
            "INSERT INTO assistant_outbox(id,event_key,user_id,kind,due_at,deadline,"
            "daily_reminder_id,available_at,reminder_version) "
            "SELECT $1,$2,r.user_id,'daily_reminder_delivery',$3,$4,r.id,$3,r.version "
            "FROM assistant_daily_reminders r WHERE r.id=$5 AND r.user_id=$6 AND r.version=$7 "
            "FOR SHARE OF r ON CONFLICT(event_key) DO UPDATE SET due_at=excluded.due_at,"
            "deadline=excluded.deadline,available_at=excluded.available_at,state='pending',"
            "attempts=0,lease_until=NULL,claim_token=NULL,payload=NULL,error_code=NULL,"
            "trace_id=NULL,reminder_version=excluded.reminder_version "
            "WHERE assistant_outbox.state IN ('cancelled','missed','failed') "
            "AND assistant_outbox.reminder_version<excluded.reminder_version",
            uuid.uuid4(),
            f"daily-reminder:{reminder['id']}:{local_date}",
            due,
            due + timedelta(minutes=15),
            reminder["id"],
            reminder["user_id"],
            reminder["version"],
        )

    async def enqueue_briefing(self, profile, due, local_date):
        await self.pool.execute(
            "INSERT INTO assistant_outbox(id,event_key,user_id,kind,due_at,deadline,"
            "consent_version,available_at) VALUES($1,$2,$3,'daily_briefing',$4,$5,$6,$4) "
            "ON CONFLICT(event_key) DO UPDATE SET due_at=excluded.due_at,"
            "deadline=excluded.deadline,available_at=excluded.available_at,"
            "consent_version=excluded.consent_version,state='pending',attempts=0,"
            "lease_until=NULL,claim_token=NULL,payload=NULL,error_code=NULL "
            "WHERE assistant_outbox.state IN ('cancelled','missed','failed') "
            "AND assistant_outbox.consent_version IS DISTINCT FROM excluded.consent_version",
            uuid.uuid4(),
            f"briefing:{profile['user_id']}:{local_date}",
            profile["user_id"],
            due,
            due + timedelta(hours=1),
            profile["consent_version"],
        )

    async def claim(self, now):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE assistant_outbox SET state='missed',payload=NULL WHERE deadline<$1 "
                "AND state IN ('pending','retry','working') "
                "AND (lease_until IS NULL OR lease_until<=$1)",
                now,
            )
            await conn.execute(
                "UPDATE assistant_outbox SET state='failed',"
                "error_code='attempts_exhausted',payload=NULL "
                "WHERE attempts>=3 AND state='working' AND lease_until<=$1",
                now,
            )
            row = await conn.fetchrow(
                "SELECT * FROM assistant_outbox WHERE deadline>=$1 AND attempts<3 AND "
                "((state IN ('pending','retry') AND available_at<=$1) OR "
                "(state='working' AND lease_until<=$1)) ORDER BY due_at,id "
                "FOR UPDATE SKIP LOCKED LIMIT 1",
                now,
            )
            if not row:
                return None
            return dict(
                await conn.fetchrow(
                    "UPDATE assistant_outbox SET state='working',attempts=attempts+1,"
                    "lease_until=$2,claim_token=$3 WHERE id=$1 RETURNING *",
                    row["id"],
                    now + timedelta(minutes=5),
                    uuid.uuid4(),
                )
            )

    async def payload(self, event, text, trace):
        return await self.pool.fetchval(
            "UPDATE assistant_outbox SET payload=$3,trace_id=$4 WHERE id=$1 AND claim_token=$2 "
            "AND state='working' RETURNING id",
            event["id"],
            event["claim_token"],
            text,
            trace,
        )

    @asynccontextmanager
    async def delivery(self, event, now):
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM assistant_profiles WHERE user_id=$1 FOR UPDATE", event["user_id"]
            )
            current = await conn.fetchrow(
                "SELECT * FROM assistant_outbox WHERE id=$1 FOR UPDATE", event["id"]
            )
            valid = bool(
                row
                and row["destination_chat_id"]
                and current
                and current["state"] == "working"
                and current["claim_token"] == event["claim_token"]
                and current["deadline"] >= now
            )
            if event["kind"] == "daily_briefing":
                valid = (
                    valid
                    and row["briefing_enabled"]
                    and row["consent_version"] == event["consent_version"]
                )
            elif event["kind"] == "daily_reminder_delivery":
                valid = (
                    valid
                    and row["destination_chat_id"] == event["user_id"]
                    and await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM assistant_daily_reminders "
                        "WHERE user_id=$1 AND id=$2 AND version=$3)",
                        event["user_id"],
                        event["daily_reminder_id"],
                        event["reminder_version"],
                    )
                )
            elif event["kind"] == "reminder_delivery":
                valid = (
                    valid
                    and row["destination_chat_id"] == event["user_id"]
                    and await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM agent_reminders WHERE user_id=$1 AND id=$2 "
                        "AND version=$3 AND completed_at IS NULL)",
                        event["user_id"],
                        event["reminder_id"],
                        event["reminder_version"],
                    )
                )
            elif event["kind"] == "task_deadline":
                valid = (
                    valid
                    and row["destination_chat_id"] == event["user_id"]
                    and await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM personal_tasks WHERE user_id=$1 AND id=$2 "
                        "AND version=$3 AND notify_before IS NOT NULL AND completed_at IS NULL)",
                        event["user_id"],
                        event["task_id"],
                        event["reminder_version"],
                    )
                )
            elif event["kind"] == "itmo_change":
                valid = (
                    valid
                    and row["destination_chat_id"] == event["user_id"]
                    and await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM itmo_watch_profiles "
                        "WHERE user_id=$1 AND enabled AND version=$2)",
                        event["user_id"],
                        event["consent_version"],
                    )
                )
            if not valid:
                await conn.execute(
                    "UPDATE assistant_outbox SET state=$3,payload=NULL WHERE id=$1 "
                    "AND claim_token=$2 AND state='working'",
                    event["id"],
                    event["claim_token"],
                    "missed" if current and current["deadline"] < now else "cancelled",
                )
                yield None
            else:
                yield row["destination_chat_id"], conn

    async def complete(self, event, now, *, error=None, permanent=False, conn=None, retry_after=0):
        state = (
            "sent"
            if error is None
            else "failed"
            if permanent or event["attempts"] >= 3
            else "retry"
        )
        await (conn or self.pool).execute(
            "UPDATE assistant_outbox SET state=$3,error_code=$4,available_at=$5,lease_until=NULL,"
            "sent_at=$6,payload=CASE WHEN $3 IN ('sent','failed') THEN NULL ELSE payload END "
            "WHERE id=$1 AND claim_token=$2 AND state='working'",
            event["id"],
            event["claim_token"],
            state,
            error,
            now + timedelta(seconds=max(retry_after, 30 if event["attempts"] == 1 else 120)),
            now if state == "sent" else None,
        )

    async def reminder(self, owner, identity):
        return await self.pool.fetchrow(
            "SELECT * FROM agent_reminders WHERE user_id=$1 AND id=$2 AND completed_at IS NULL",
            owner,
            identity,
        )

    async def reminders_between(self, owner, first, last):
        return await self.pool.fetch(
            "SELECT text,remind_at FROM agent_reminders WHERE user_id=$1 AND remind_at>=$2 "
            "AND remind_at<$3 AND completed_at IS NULL ORDER BY remind_at",
            owner,
            first,
            last,
        )

    async def cleanup(self, now):
        from app.material_store import MaterialStore

        await MaterialStore(self.pool).cleanup(now)
        await self.pool.execute(
            "DELETE FROM assistant_traces WHERE created_at<$1", now - timedelta(days=30)
        )
        await self.pool.execute("DELETE FROM assistant_city_pending WHERE expires_at<$1", now)
        await self.pool.execute("DELETE FROM assistant_daily_pending WHERE expires_at<$1", now)
        await self.pool.execute("DELETE FROM planner_actions WHERE expires_at<$1", now)
        await self.pool.execute("DELETE FROM itmo_watch_actions WHERE expires_at<$1", now)
        await self.pool.execute(
            "DELETE FROM assistant_outbox WHERE kind IN "
            "('daily_reminder_delivery','task_deadline','itmo_change') "
            "AND deadline<$1 AND state IN ('sent','missed','failed','cancelled')",
            now - timedelta(days=30),
        )
