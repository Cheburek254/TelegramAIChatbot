import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from app.itmo import ITMOClient
from app.itmo_vault import STALE_WARNING, change_prompt

REFRESH = "synthetic-refresh-token-old"
ROTATED = "synthetic-refresh-token-rotated"
NOW = datetime(2026, 11, 7, tzinfo=UTC)


async def test_legacy_migration_refresh_rotation_and_manual_revocation(tmp_path):
    directory = tmp_path / "data" / "101"
    directory.mkdir(parents=True)
    path = directory / "state.json"
    path.write_text(json.dumps({"refreshToken": REFRESH, "loginTicket": "legacy"}))
    key = tmp_path / "vault-key"
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o600)
    revoke = AsyncMock(side_effect=[False, True])
    client = ITMOClient(directory.parent, key, clock=lambda: NOW, revoke=revoke)
    await client.migrate_legacy()
    encrypted = directory / "state.enc"
    assert not path.exists() and REFRESH.encode() not in encrypted.read_bytes()
    day = NOW.date()

    async def exchange(runtime, first, last):
        target = runtime / "state.json"
        assert runtime != directory and json.loads(target.read_bytes())["refreshToken"] == REFRESH
        target.write_text(json.dumps({"refreshToken": ROTATED}))
        return {
            "range": {"from": first.isoformat(), "to": last.isoformat()},
            "days": [
                {
                    "date": first.isoformat(),
                    "lessons": [
                        {"time": "10:00-11:30", "subject": "Synthetic lesson", "room": "A"}
                    ],
                }
            ],
        }

    client._exchange = AsyncMock(side_effect=exchange)
    assert (await client.fetch(101, day, day))[0]["title"] == "Synthetic lesson"
    assert not client._exchange.await_args.args[0].exists()
    assert ROTATED.encode() not in encrypted.read_bytes()
    assert await client.apply_control(101, "disconnect") == "revoking"
    assert not client.connected(101)
    revoke.assert_awaited_once_with(ROTATED)
    lessons, warning = await client.display(101, day, day)
    assert warning == STALE_WARNING and len(lessons) == 1
    client._exchange.assert_awaited_once()
    assert await client.apply_control(101, "disconnect") == "disconnected"
    assert client.vault.load(101)["sdk"] is None
    assert await client.apply_control(101, "disconnect") == "already_disconnected"
    assert await client.display(102, day, day) == (None, None)


async def test_manual_import_is_removed_without_revoking_the_source_session(tmp_path):
    revoke = AsyncMock()
    client = ITMOClient(tmp_path, revoke=revoke)
    async with client.session_lock(101):
        await client.install(101, REFRESH, "manual", bot_issued=False)
    assert await client.apply_control(101, "disconnect") == "local"
    revoke.assert_not_awaited()
    assert client.vault.load(101)["sdk"] is None


async def test_expiry_revokes_without_read_request_and_off_disables_expiry(tmp_path):
    now = NOW
    done = asyncio.Event()

    async def revoke(refresh):
        assert refresh == REFRESH
        done.set()
        return True

    client = ITMOClient(tmp_path, clock=lambda: now, revoke=revoke)
    async with client.session_lock(101):
        await client.install(101, REFRESH, "first")
    assert client.status(101)["days"] == 30
    assert "украденный токен" in change_prompt("off")
    assert await client.apply_control(101, "off") == "saved"
    now += timedelta(days=31)
    assert client.connected(101)
    assert await client.apply_control(101, 40) == "saved"
    now += timedelta(days=10)
    worker = asyncio.create_task(client.maintenance())
    try:
        await asyncio.wait_for(done.wait(), 3)
        assert client.status(101)["status"] == "disconnected"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
