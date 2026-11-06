import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest

from app.db import create_pool
from app.itmo import ITMOClient
from app.itmo_auth import ITMOAuth, load_cipher
from app.itmo_auth_store import AuthStore
from app.itmo_login import LoginError
from app.migrations import migrate
from tests.test_integration import database as database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="Отдельная PostgreSQL"),
]


async def test_itmo_login_owner_confirmation_encryption_replays_expiry_and_rate(database, tmp_path):
    settings, _, _, _ = database
    pool = await create_pool(settings)
    now = datetime.now(UTC)
    refresh = "synthetic-refresh-for-confirmation-tests"
    login = Mock(login=AsyncMock(return_value=(refresh, "111111")))
    client = ITMOClient(tmp_path)
    try:
        await migrate(pool)
        await migrate(pool)
        store = AuthStore(pool)
        auth = ITMOAuth(store, client, "https://study.example", clock=lambda: now, login=login)
        identity, url = await auth.issue(101)
        secret = urlsplit(url).fragment
        row = await auth.status(101, identity)
        assert secret not in row["token_hash"] and row["encrypted_token"] is None
        assert not (tmp_path / "101" / "state.json").exists()
        with pytest.raises(LoginError):
            await auth.confirm(101, identity)
        with pytest.raises(LoginError, match="expired_link"):
            await auth.sign_in(secret, "111111", "synthetic-password", owner=202)
        login.login.assert_not_called()
        await auth.sign_in(secret, "111111", "synthetic-password", owner=101)
        row = await auth.status(101, identity)
        assert refresh not in row["encrypted_token"]
        assert load_cipher(tmp_path).decrypt(row["encrypted_token"].encode()).decode() == refresh
        assert not (tmp_path / "101" / "state.json").exists()
        with pytest.raises(LoginError):
            await auth.status(202, identity)
        with pytest.raises(LoginError):
            await auth.confirm(202, identity)
        restarted = ITMOAuth(store, client, "https://study.example", clock=lambda: now)
        first, second = await asyncio.gather(
            restarted.confirm(101, identity), restarted.confirm(101, identity)
        )
        assert first == second == "111111"
        path = tmp_path / "101" / "state.json"
        value = json.loads(path.read_text())
        assert value == {"refreshToken": refresh, "loginTicket": str(identity)}
        assert (await auth.status(101, identity))["encrypted_token"] is None
        # Completed callbacks must not overwrite a rotated token.
        value["refreshToken"] = "synthetic-rotated-refresh"
        path.write_text(json.dumps(value))
        await auth.confirm(101, identity)
        assert json.loads(path.read_text())["refreshToken"] == "synthetic-rotated-refresh"
        # Simulate file installation followed by a failed DB commit.
        await pool.execute(
            "UPDATE itmo_login_tickets SET state='verified',encrypted_token=$2 WHERE id=$1",
            identity,
            auth.cipher.encrypt(refresh.encode()).decode(),
        )
        assert await auth.confirm(101, identity, cancel=True) == "111111"
        assert json.loads(path.read_text())["refreshToken"] == "synthetic-rotated-refresh"
        cancelled, url = await auth.issue(303)
        await auth.sign_in(urlsplit(url).fragment, "303", "synthetic-password")
        assert await auth.confirm(303, cancelled, cancel=True) is None
        assert not (tmp_path / "303" / "state.json").exists()
        with pytest.raises(LoginError):
            await auth.confirm(303, cancelled)
        expired, url = await auth.issue(404)
        await auth.sign_in(urlsplit(url).fragment, "404", "synthetic-password")
        auth.clock = lambda: now + timedelta(minutes=11)
        with pytest.raises(LoginError):
            await auth.confirm(404, expired)
        await store.cleanup(auth.clock())
        assert (
            await pool.fetchval(
                "SELECT encrypted_token FROM itmo_login_tickets WHERE id=$1", expired
            )
            is None
        )
        auth.clock = lambda: now
        bad, url = await auth.issue(505)
        login.login.side_effect = LoginError("credentials_or_extra_step")
        for _ in range(3):
            with pytest.raises(LoginError):
                await auth.sign_in(urlsplit(url).fragment, "505", "synthetic-password")
        assert (await auth.status(505, bad))["attempts"] == 3
        with pytest.raises(LoginError, match="expired_link"):
            await auth.sign_in(urlsplit(url).fragment, "505", "synthetic-password")
        for _ in range(5):
            await auth.issue(606)
        with pytest.raises(LoginError, match="rate_limited"):
            await auth.issue(606)
    finally:
        await pool.close()
