"""Запускается интерактивно внутри контейнера: пароль и токен не передаются в чат/argv."""

import argparse
import asyncio
import getpass
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.itmo import ITMOClient, ITMOError
from app.timezones import zone


def store_session(root, owner, token, *, replace=False, connection_id=None):
    if type(owner) is not int or owner <= 0:
        raise ValueError("invalid_owner")
    if (
        not isinstance(token, str)
        or not 20 <= len(token) <= 16000
        or any(c.isspace() for c in token)
    ):
        raise ValueError("invalid_token")
    root = Path(root).resolve()
    directory = root / str(owner)
    if directory.is_symlink():
        raise ValueError("invalid_directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    target = directory / "state.json"
    if target.exists() and not replace:
        raise ValueError("already_connected_use_replace")
    temporary = directory / (".new-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            value = {"refreshToken": token}
            if connection_id is not None:
                value["loginTicket"] = str(connection_id)
            json.dump(value, stream)
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telegram-user", type=int, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    token = getpass.getpass("Refresh token my.itmo (ввод скрыт): ").strip()
    client = ITMOClient(settings.itmo_state_dir)
    try:
        if not 0 < args.telegram_user < 2**63:
            raise ValueError
        directory = client.state_dir / str(args.telegram_user)
        if directory.is_symlink():
            raise ValueError
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        async with client.session_lock(args.telegram_user):
            store_session(settings.itmo_state_dir, args.telegram_user, token, replace=args.replace)
    except (OSError, ValueError):
        print("Не удалось сохранить сессию. Проверьте ID, права папки и необходимость --replace.")
        return 1
    finally:
        token = ""
    today = datetime.now(zone("Europe/Moscow")).date()
    try:
        rows = await client.fetch(args.telegram_user, today, today + timedelta(days=6))
    except ITMOError:
        print(
            "Сессия сохранена, но ИТМО не подтвердил доступ. Проверьте токен и сеть; "
            "для повторного ввода используйте --replace."
        )
        return 1
    print(f"Подключено. Расписание доступно; занятий за 7 дней: {len(rows)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
