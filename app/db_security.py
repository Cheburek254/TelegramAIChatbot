"""Provision a restricted login and row policies as the database administrator."""

import argparse
import asyncio
import getpass
import os
from pathlib import Path

import asyncpg

from app.migrations import migrate

RUNTIME = "study_assistant_runtime"
OWNER = "NULLIF(pg_catalog.current_setting('app.owner_id', true), '')::bigint"
TABLES = (
    "chat_users",
    "chat_messages",
    "chat_style_actions",
    "agent_settings",
    "agent_requests",
    "agent_pending",
    "agent_reminders",
    "agent_audit",
    "assistant_profiles",
    "assistant_city_pending",
    "assistant_requests",
    "assistant_action_auth",
    "assistant_traces",
    "assistant_spans",
    "assistant_outbox",
    "assistant_daily_reminders",
    "assistant_daily_pending",
    "itmo_login_tickets",
    "itmo_session_actions",
    "itmo_watch_profiles",
    "itmo_watch_actions",
    "personal_schedule_lessons",
    "personal_schedule_actions",
    "personal_tasks",
    "planner_actions",
    "user_materials",
    "user_content_pending",
)


async def install(pool, password):
    await migrate(pool)
    async with pool.acquire() as conn, conn.transaction():
        admin = await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")
        if not admin:
            raise ValueError("Database administrator required")
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", RUNTIME)
        if not exists:
            await conn.execute(f"CREATE ROLE {RUNTIME}")
        if await conn.fetchval(
            "SELECT EXISTS(SELECT FROM pg_auth_members WHERE member=$1::regrole)", RUNTIME
        ):
            raise ValueError("Runtime role must have no role memberships")
        quoted = await conn.fetchval("SELECT quote_literal($1::text)", password)
        await conn.execute(
            f"ALTER ROLE {RUNTIME} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {quoted}"
        )
        database = await conn.fetchval("SELECT quote_ident(current_database())")
        await conn.execute(f"""
            REVOKE CREATE ON SCHEMA public FROM PUBLIC, {RUNTIME};
            REVOKE CREATE, TEMPORARY ON DATABASE {database} FROM PUBLIC, {RUNTIME};
            GRANT CONNECT ON DATABASE {database} TO {RUNTIME};
            GRANT USAGE ON SCHEMA public TO {RUNTIME};
        """)
        for name in TABLES:
            if name == "assistant_spans":
                predicate = (
                    "EXISTS (SELECT FROM public.assistant_traces t "
                    f"WHERE t.id=trace_id AND t.user_id = {OWNER})"
                )
            else:
                predicate = f"user_id = {OWNER}"
            await conn.execute(f"""
                ALTER TABLE public.{name} ENABLE ROW LEVEL SECURITY;
                ALTER TABLE public.{name} FORCE ROW LEVEL SECURITY;
                DROP POLICY IF EXISTS study_owner ON public.{name};
                CREATE POLICY study_owner ON public.{name} TO {RUNTIME}
                    USING ({predicate}) WITH CHECK ({predicate});
                GRANT SELECT, INSERT, UPDATE, DELETE ON public.{name} TO {RUNTIME};
            """)
        await conn.execute(f"""
            GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME};
            CREATE OR REPLACE FUNCTION public.study_job_owners()
            RETURNS SETOF bigint LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog AS $fn$
                SELECT user_id FROM public.assistant_profiles
                UNION SELECT user_id FROM public.itmo_login_tickets
                UNION SELECT user_id FROM public.itmo_session_actions
                UNION SELECT user_id FROM public.itmo_watch_profiles
            $fn$;
            CREATE OR REPLACE FUNCTION public.study_itmo_ticket_owner(digest text)
            RETURNS bigint LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog AS $fn$
                SELECT user_id FROM public.itmo_login_tickets
                WHERE token_hash=digest AND expires_at>statement_timestamp()
                    AND state='issued' AND attempts<3
            $fn$;
            REVOKE ALL ON FUNCTION public.study_job_owners() FROM PUBLIC;
            REVOKE ALL ON FUNCTION public.study_itmo_ticket_owner(text) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION public.study_job_owners() TO {RUNTIME};
            GRANT EXECUTE ON FUNCTION public.study_itmo_ticket_owner(text) TO {RUNTIME};
        """)


def runtime_password():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password-file")
    args = parser.parse_args()
    if args.password_file:
        password = Path(args.password_file).read_text(encoding="utf-8").strip()
    else:
        password = getpass.getpass("Runtime password: ")
    if len(password) < 24:
        raise SystemExit("Runtime password must contain at least 24 characters")
    return password


async def main(password):
    async with asyncpg.create_pool(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "bot"),
        user=os.getenv("POSTGRES_USER", "bot"),
        password=os.environ["POSTGRES_PASSWORD"],
        min_size=1,
        max_size=1,
    ) as pool:
        await install(pool, password)


if __name__ == "__main__":
    asyncio.run(main(runtime_password()))
