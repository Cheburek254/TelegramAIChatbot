"""Запускается интерактивно внутри контейнера: пароль и токен не передаются в чат/argv."""

import argparse
import asyncio
import getpass
import uuid
from datetime import datetime, timedelta

from app.config import Settings
from app.itmo import ITMOClient, ITMOError
from app.timezones import zone


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telegram-user", type=int, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    token = getpass.getpass("Refresh token my.itmo (ввод скрыт): ").strip()
    client = ITMOClient(settings.itmo_state_dir, settings.itmo_vault_key_file)
    try:
        async with client.session_lock(args.telegram_user):
            await client.install(
                args.telegram_user, token, uuid.uuid4(), replace=args.replace, bot_issued=False
            )
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
