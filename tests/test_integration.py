"""RUN_INTEGRATION=1 python -m pytest -m integration -v.

Тест создаёт собственные контейнер и volume; пользовательскую БД не использует.
"""

import asyncio
import json
import os
import secrets
import socket
import subprocess
import time
from dataclasses import replace

import asyncpg
import pytest

from app.config import Settings
from app.db import create_pool
from app.health import HealthState, health_result

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "1",
        reason="Включите RUN_INTEGRATION=1 для Docker-тестов",
    ),
]


@pytest.fixture
def database():
    name = "itmo-test-" + secrets.token_hex(6)
    volume = name + "-data"
    password = secrets.token_urlsafe(24)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    def docker(*args):
        return subprocess.check_output(
            ["docker", *args], text=True, stderr=subprocess.PIPE, timeout=180
        )

    def wait_until_ready():
        deadline = time.monotonic() + 60
        while True:
            try:
                docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "bot", "-d", "bot")
                return
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    docker("info")
    docker("volume", "create", volume)
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:5432",
            "-e",
            "POSTGRES_DB=bot",
            "-e",
            "POSTGRES_USER=bot",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "--mount",
            f"type=volume,source={volume},target=/var/lib/postgresql/data",
            "postgres:16-bookworm",
        )
        info = json.loads(docker("inspect", name))[0]
        assert int(info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]) == port
        wait_until_ready()
        yield (
            Settings(
                bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                postgres_password=password,
                postgres_port=port,
            ),
            docker,
            wait_until_ready,
            name,
        )
    finally:
        try:
            docker("rm", "-f", name)
        finally:
            docker("volume", "rm", volume)


async def test_real_db_password_restart_health_and_persistence(database):
    # Arrange
    settings, docker, wait_until_ready, name = database
    pool = await create_pool(settings)
    task = asyncio.create_task(asyncio.Event().wait())
    state = HealthState(pool=pool, initialized=True, polling_task=task)
    try:
        await pool.execute("CREATE TABLE integration_probe (value text NOT NULL)")
        await pool.execute("INSERT INTO integration_probe VALUES ($1)", "Сохранилось 👋")
        # Act / Assert: реальное подключение и ошибка пароля.
        assert (await health_result(state))[0] == 200
        with pytest.raises(asyncpg.InvalidPasswordError):
            await create_pool(replace(settings, postgres_password="wrong-password"))
        # Act / Assert: потеря соединения и сохранность данных после запуска.
        await asyncio.to_thread(docker, "stop", name)
        assert (await health_result(state))[0] == 503
        await asyncio.to_thread(docker, "start", name)
        await asyncio.to_thread(wait_until_ready)
        for _ in range(60):
            if (await health_result(state))[0] == 200:
                break
            await asyncio.sleep(1)
        assert (await health_result(state))[0] == 200
        assert await pool.fetchval("SELECT value FROM integration_probe") == "Сохранилось 👋"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await pool.close()


async def test_chat_real_postgres_isolation_rollback_concurrency_and_restart(database):
    # Arrange
    from unittest.mock import AsyncMock, Mock

    from app.assistant import Assistant
    from app.llm import Completion, LLMError
    from app.migrations import migrate
    from app.repository import PostgresRepository

    settings, docker, wait_until_ready, name = database
    pool = await create_pool(settings)
    llm = Mock(complete=AsyncMock(return_value=Completion("Ответ", "test", "fake")))
    try:
        await migrate(pool)
        await migrate(pool)
        service = Assistant(settings, PostgresRepository(pool), llm)
        # Act: одновременно разные пользователи и два сообщения одного пользователя.
        await asyncio.gather(
            service.respond(1, "'; DROP TABLE chat_users; --"), service.respond(2, "Чужой")
        )
        await asyncio.gather(service.respond(1, "Второй"), service.respond(1, "Третий"))
        await service.preferences(1, "0.7")
        await pool.close()
        await asyncio.to_thread(docker, "restart", name)
        await asyncio.to_thread(wait_until_ready)
        pool = await create_pool(settings)
        restarted = Assistant(settings, PostgresRepository(pool), llm)
        await restarted.respond(1, "После перезапуска")
        # Assert: фактически переданные после перезапуска роли и независимые данные.
        messages, temperature, _ = llm.complete.call_args.args
        questions = [m["content"] for m in messages[1:]][::2]
        assert questions[0] == "'; DROP TABLE chat_users; --"
        assert questions[-1] == "После перезапуска"
        # Concurrent requests may acquire the DB lock in either order. Both
        # complete turns must survive, without assuming pool/lock FIFO order.
        assert len(questions) == 4 and set(questions[1:3]) == {"Второй", "Третий"}
        assert [m["role"] for m in messages] == ["system"] + ["user", "assistant"] * 3 + ["user"]
        assert [m["content"] for m in messages[2:-1:2]] == ["Ответ"] * 3
        assert temperature == 0.7
        assert await pool.fetchval("SELECT count(*) FROM chat_users") == 2
        llm.complete.side_effect = LLMError("timeout")
        with pytest.raises(LLMError):
            await restarted.respond(1, "Не сохранять")
        assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE user_id=1") == 8
        await restarted.mode(1, "review")
        await restarted.reset(1)
        assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE user_id=1") == 0
        assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE user_id=2") == 2
        profile = await pool.fetchrow("SELECT mode,temperature FROM chat_users WHERE user_id=1")
        assert dict(profile) == {"mode": "review", "temperature": 0.7}
    finally:
        await pool.close()
