"""Повторяемые миграции PostgreSQL без удаления существующих данных."""

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_users (
    user_id BIGINT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'study' CHECK (mode IN ('study', 'translate', 'review')),
    temperature DOUBLE PRECISION NOT NULL DEFAULT 0.3
        CHECK (temperature IN (0.0, 0.3, 0.7, 1.0))
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES chat_users(user_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL CHECK (length(content) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_messages_user_id_id ON chat_messages(user_id, id);
CREATE TABLE IF NOT EXISTS agent_settings (
    user_id BIGINT PRIMARY KEY,
    timezone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_requests (
    user_id BIGINT NOT NULL,
    request_key TEXT NOT NULL,
    response TEXT NOT NULL,
    pending_id UUID,
    PRIMARY KEY(user_id, request_key)
);
CREATE TABLE IF NOT EXISTS agent_pending (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL,
    request_key TEXT NOT NULL,
    text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 500),
    remind_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending','confirmed','completed','cancelled','expired','superseded')),
    UNIQUE(user_id,request_key)
);
CREATE TABLE IF NOT EXISTS agent_reminders (
    id UUID PRIMARY KEY,
    action_id UUID NOT NULL UNIQUE REFERENCES agent_pending(id),
    user_id BIGINT NOT NULL,
    text TEXT NOT NULL,
    remind_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS agent_audit (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    action TEXT NOT NULL,
    tool TEXT,
    arguments JSONB NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    duration_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_audit_user_id_id ON agent_audit(user_id,id DESC);
CREATE INDEX IF NOT EXISTS agent_pending_user_state ON agent_pending(user_id,state);
CREATE TABLE IF NOT EXISTS itmo_login_tickets (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('issued','busy','verified','completed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    encrypted_token TEXT,
    account TEXT
);
CREATE INDEX IF NOT EXISTS itmo_login_owner_created ON itmo_login_tickets(user_id,created_at);
CREATE TABLE IF NOT EXISTS personal_schedule_lessons (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    date DATE NOT NULL,
    "start" TIME NOT NULL,
    "end" TIME NOT NULL CHECK ("end" > "start"),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
    location TEXT NOT NULL CHECK(length(location) BETWEEN 1 AND 200),
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS personal_schedule_owner_date ON personal_schedule_lessons(user_id,date);
CREATE TABLE IF NOT EXISTS personal_schedule_actions (
    id UUID PRIMARY KEY,
    user_id BIGINT NOT NULL,
    request_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    lesson_id BIGINT,
    expected_version INTEGER,
    payload JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('pending','completed','cancelled','expired','superseded','stale')),
    result_id BIGINT,
    UNIQUE(user_id,request_key)
);
ALTER TABLE personal_schedule_lessons ADD COLUMN IF NOT EXISTS series_id UUID;
ALTER TABLE personal_schedule_actions
    DROP CONSTRAINT IF EXISTS personal_schedule_actions_operation_check;
ALTER TABLE personal_schedule_actions ADD CONSTRAINT personal_schedule_actions_operation_check
    CHECK(operation IN ('add','edit','delete','repeat','delete_series','import'));
"""


async def migrate(pool: asyncpg.Pool) -> None:
    from app.itmo_watch_store import SCHEMA as ITMO_WATCH_SCHEMA
    from app.material_store import SCHEMA as MATERIAL_SCHEMA
    from app.orchestration.store import SCHEMA as ASSISTANT_SCHEMA
    from app.planner_store import SCHEMA as PLANNER_SCHEMA
    from app.style_store import SCHEMA as STYLE_SCHEMA

    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("SELECT pg_advisory_xact_lock(-3214001)")
        await connection.execute(SCHEMA)
        await connection.execute(ASSISTANT_SCHEMA)
        await connection.execute(MATERIAL_SCHEMA)
        await connection.execute(STYLE_SCHEMA)
        await connection.execute(PLANNER_SCHEMA)
        await connection.execute(ITMO_WATCH_SCHEMA)
