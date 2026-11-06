"""Authenticated, owner-bound storage for the ITMO SDK state."""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

DEFAULT_REVOKE_DAYS = 3
STALE_WARNING = "Данные ИТМО могут быть устаревшими. Показано сохранённое расписание."
PARTIAL_WARNING = STALE_WARNING + " Сохранённая копия покрывает только часть периода."
NO_CACHE_WARNING = "Данные ИТМО могут быть устаревшими. Показано только личное расписание."
NO_EXPIRY_WARNING = (
    "Без автоотключения украденный токен может оставаться пригодным для входа "
    "до истечения срока у ИТМО или ручного отзыва."
)


def change_prompt(value):
    if value == "off":
        return "Выключить автоматическое отключение ИТМО?\n\n⚠️ " + NO_EXPIRY_WARNING
    if value == "disconnect":
        return (
            "Отключить ИТМО и отозвать сессию бота? Входы на других устройствах сохранятся. "
            "Сохранённое расписание можно будет просматривать."
        )
    return f"Отключать ИТМО через {value} дн. после входа?"


CHANGE_RESULTS = {
    "saved": "Настройка автоотключения ИТМО сохранена.",
    "cancelled": "Изменение отменено.",
    "disconnected": "ИТМО отключён. Сессия бота отозвана. Сохранённое расписание может устареть.",
    "revoking": "ИТМО отключён в боте. ИТМО пока не подтвердил отзыв. Бот повторит запрос.",
    "local": "ИТМО отключён в боте. Вручную добавленный токен удалён из бота. "
    "Сессия, из которой его скопировали, остаётся в ИТМО.",
    "already_disconnected": "У бота уже нет активной сессии ИТМО.",
}


def atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".new-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_cipher(directory, key_file=None):
    if key_file:
        path = Path(key_file)
        if path.is_symlink() or (os.name == "posix" and path.stat().st_mode & 0o077):
            raise ValueError("invalid_vault_key_permissions")
        return Fernet(path.read_bytes().strip())
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / ".auth-key"
    if path.is_symlink():
        raise ValueError("invalid_auth_key")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(Fernet.generate_key())
    return Fernet(path.read_bytes())


class SessionVault:
    def __init__(self, root, key_file=None):
        self.root = root
        self.key_file = key_file
        self.cipher = load_cipher(root, key_file) if key_file else None

    def directory(self, owner):
        if type(owner) is not int or not 0 < owner < 2**63:
            raise ValueError("invalid_owner")
        directory = self.root / str(owner)
        if directory.is_symlink():
            raise ValueError("invalid_directory")
        return directory

    def crypto(self):
        if self.cipher is None:
            self.cipher = load_cipher(self.root)
        return self.cipher

    def load(self, owner):
        directory = self.directory(owner)
        target = directory / "state.enc"
        legacy = directory / "state.json"
        if target.is_file():
            return self.read(owner, "state.enc")
        if legacy.is_symlink():
            raise ValueError("invalid_vault_path")
        sdk = json.loads(legacy.read_bytes()) if legacy.is_file() else None
        return {
            "sdk": sdk,
            "bot_issued": bool(sdk and sdk.get("loginTicket")),
            "started": datetime.now(UTC).timestamp(),
            "days": DEFAULT_REVOKE_DAYS,
            "status": "active" if legacy.is_file() else "never",
        }

    def save(self, owner, value):
        directory = self.directory(owner)
        self.write(owner, "state.enc", value)
        (directory / "state.json").unlink(missing_ok=True)

    def read(self, owner, name):
        path = self.directory(owner) / name
        if path.is_symlink():
            raise ValueError("invalid_vault_path")
        value = json.loads(self.crypto().decrypt(path.read_bytes()))
        if value.pop("owner", None) != owner:
            raise ValueError("invalid_vault_owner")
        return value

    def write(self, owner, name, value):
        data = json.dumps({**value, "owner": owner}, ensure_ascii=False).encode()
        atomic_write(self.directory(owner) / name, self.crypto().encrypt(data))
