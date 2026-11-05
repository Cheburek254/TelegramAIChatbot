"""Скрытая персональная настройка не заменяет режим study/translate/review."""

import uuid
from datetime import UTC, datetime


class SpeechStyles:
    def __init__(self, store, repository, *, clock=None):
        self.store, self.repository = store, repository
        self.clock = clock or (lambda: datetime.now(UTC))

    async def command(self, owner, argument, key):
        if not argument:
            async with self.repository.session(owner) as conversation:
                profile = await conversation.profile()
            return (
                "Декоратор речи: "
                + ("включён." if profile.speech_style == "yiddish" else "выключен.")
                + "\n/jew on — включить\n/jew off — выключить"
            ), None
        if argument not in {"on", "off"}:
            return "Используйте /jew on или /jew off.", None
        pending = await self.store.propose(
            owner, key, "yiddish" if argument == "on" else "plain", self.clock()
        )
        return (
            (
                "Включить выраженную русско-идишскую стилизацию всего ответа?"
                if argument == "on"
                else "Выключить стилизацию речи?"
            )
            + "\n\nРежим, история и настройки сохранятся. Изменится только манера ответа. "
            "Подтверждение действует 5 минут.",
            pending,
        )

    async def confirm(self, owner, identity, *, cancel=False):
        state = await self.store.confirm(owner, uuid.UUID(str(identity)), self.clock(), cancel)
        return {
            "yiddish": "✅ Декоратор речи включён поверх текущего режима. Отключение: /jew off.",
            "plain": "✅ Декоратор речи выключен. Текущий режим и история сохранены.",
            "cancelled": "Изменение стиля отменено.",
        }.get(state, "Стиль не изменён: карточка устарела или уже обработана.")
