"""Ожидание входа и привязка только после подтверждения в Telegram."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from app.db import job_owners, owner_scope
from app.itmo import ITMOError
from app.itmo_login import ITMOLogin, LoginError
from app.itmo_vault import load_cipher


class ITMOAuth:
    def __init__(self, store, client, public_url, *, clock=None, login=None, cipher=None):
        self.store, self.client, self.public_url = store, client, public_url.rstrip("/")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.login = login or ITMOLogin()
        self.cipher = cipher or load_cipher(client.state_dir)

    async def cleanup(self):
        for owner in await job_owners(self.store.pool):
            with owner_scope(owner):
                await self.store.cleanup(self.clock())

    async def propose_control(self, owner, value):
        if value not in ("disconnect", "off") and (type(value) is not int or not 1 <= value <= 365):
            raise ITMOError("invalid_itmo_policy")
        return await self.store.propose_control(owner, value, self.clock())

    async def apply_control(self, owner, identity, *, cancel=False):
        async with self.store.control(owner, identity, self.clock()) as value:
            return "cancelled" if cancel else await self.client.apply_control(owner, value)

    async def issue_ticket(self, owner):
        secret = secrets.token_urlsafe(32)
        row = await self.store.create(
            owner, hashlib.sha256(secret.encode()).hexdigest(), self.clock()
        )
        return row["id"], secret

    async def issue(self, owner):
        identity, secret = await self.issue_ticket(owner)
        return identity, self.public_url + "/itmo/login#" + secret

    async def sign_in(self, secret, username, password, *, owner=None):
        if not isinstance(secret, str) or len(secret) != 43:
            raise LoginError("expired_link")
        if (
            not isinstance(username, str)
            or not 1 <= len(username.strip()) <= 128
            or not isinstance(password, str)
            or not 1 <= len(password) <= 1024
        ):
            raise LoginError("invalid_input")
        row = await self.store.begin(
            hashlib.sha256(secret.encode()).hexdigest(), self.clock(), owner=owner
        )
        with owner_scope(row.get("user_id", owner)):
            try:
                refresh, account = await self.login.login(username.strip(), password)
                encrypted = self.cipher.encrypt(refresh.encode()).decode()
                if not await self.store.finish(row["id"], encrypted, account, self.clock()):
                    raise LoginError("expired_link")
                return account
            except BaseException:
                await self.store.failed(row["id"])
                raise

    async def status(self, owner, identity):
        return await self.store.status(owner, uuid.UUID(str(identity)), self.clock())

    async def confirm(self, owner, identity, *, cancel=False):
        identity = uuid.UUID(str(identity))
        async with self.store.pool.acquire() as conn, conn.transaction():
            await self.store.lock_control(conn, owner)
            row = await conn.fetchrow(
                "SELECT * FROM itmo_login_tickets WHERE user_id=$1 AND id=$2 FOR UPDATE",
                owner,
                identity,
            )
            if not row:
                raise LoginError("expired_link")
            if row["state"] == "completed":
                return row["account"]
            if row["expires_at"] <= self.clock() or row["state"] not in {
                "issued",
                "verified",
                "busy",
            }:
                raise LoginError("expired_link")
            if cancel:
                # A committed file can outlive a failed DB commit. Treat that retry
                # as already completed; never report cancellation after installation.
                directory = self.client.state_dir / str(owner)
                if directory.is_dir() and not directory.is_symlink():
                    async with self.client.session_lock(owner):
                        if self.client.installed(self.client.state(owner), identity):
                            await conn.execute(
                                "UPDATE itmo_login_tickets SET state='completed',"
                                "encrypted_token=NULL "
                                "WHERE user_id=$1 AND id=$2",
                                owner,
                                identity,
                            )
                            return row["account"]
                await conn.execute(
                    "UPDATE itmo_login_tickets SET state='cancelled',encrypted_token=NULL "
                    "WHERE user_id=$1 AND id=$2",
                    owner,
                    identity,
                )
                return None
            if row["state"] != "verified" or not row["encrypted_token"]:
                raise LoginError("not_ready")
            refresh = self.cipher.decrypt(row["encrypted_token"].encode()).decode()
            directory = self.client.state_dir / str(owner)
            if directory.is_symlink():
                raise LoginError("invalid_state")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            async with self.client.session_lock(owner):
                try:
                    await self.client.install(owner, refresh, identity)
                except ITMOError as exc:
                    raise LoginError(str(exc)) from None
            await conn.execute(
                "UPDATE itmo_session_actions SET consumed=true WHERE user_id=$1 AND NOT consumed",
                owner,
            )
            await conn.execute(
                "UPDATE itmo_login_tickets SET state='completed',encrypted_token=NULL "
                "WHERE user_id=$1 AND id=$2",
                owner,
                identity,
            )
            return row["account"]
