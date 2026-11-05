"""SQL и изоляция пользователей. Блокировка действует также между процессами."""

from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True)
class Profile:
    mode: str = "study"
    temperature: float = 0.3
    speech_style: str = "default"


class Conversation:
    def __init__(self, connection: asyncpg.Connection, user_id: int):
        self.connection = connection
        self.user_id = user_id

    async def profile(self) -> Profile:
        row = await self.connection.fetchrow(
            "SELECT mode, temperature, speech_style FROM chat_users WHERE user_id = $1",
            self.user_id,
        )
        return Profile(**dict(row))

    async def history(self, limit: int) -> list[dict[str, str]]:
        rows = await self.connection.fetch(
            "SELECT role, content FROM (SELECT id, role, content FROM chat_messages "
            "WHERE user_id = $1 ORDER BY id DESC LIMIT $2) recent ORDER BY id",
            self.user_id,
            limit,
        )
        return [dict(row) for row in rows]

    async def reset(self) -> None:
        await self.connection.execute("DELETE FROM chat_messages WHERE user_id = $1", self.user_id)

    async def change_mode(self, mode: str) -> None:
        await self.connection.execute(
            "UPDATE chat_users SET mode = $2 WHERE user_id = $1", self.user_id, mode
        )
        await self.reset()

    async def change_temperature(self, value: float) -> None:
        await self.connection.execute(
            "UPDATE chat_users SET temperature = $2 WHERE user_id = $1", self.user_id, value
        )

    async def save_turn(self, question: str, answer: str, limit: int) -> None:
        await self.connection.executemany(
            "INSERT INTO chat_messages(user_id, role, content) VALUES($1, $2, $3)",
            [(self.user_id, "user", question), (self.user_id, "assistant", answer)],
        )
        # Ограничиваем и постоянное хранение, оставляя полные ходы.
        retained = max(2, limit // 2 * 2)
        await self.connection.execute(
            "DELETE FROM chat_messages WHERE user_id = $1 AND id NOT IN "
            "(SELECT id FROM chat_messages WHERE user_id = $1 ORDER BY id DESC LIMIT $2)",
            self.user_id,
            retained,
        )


class PostgresRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @asynccontextmanager
    async def session(self, user_id: int):
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", user_id)
            await connection.execute(
                "INSERT INTO chat_users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id
            )
            yield Conversation(connection, user_id)
