import asyncio
import copy
from contextlib import asynccontextmanager
from dataclasses import replace

from app.repository import Profile


class MemoryRepository:
    """Подмена границы хранения для сценариев, не для доказательства SQL-гарантий."""

    def __init__(self):
        self.profiles = {}
        self.messages = {}
        self.locks = {}

    @asynccontextmanager
    async def session(self, user_id):
        async with self.locks.setdefault(user_id, asyncio.Lock()):
            profile = self.profiles.setdefault(user_id, Profile())
            messages = copy.deepcopy(self.messages.setdefault(user_id, []))
            conversation = MemoryConversation(profile, messages)
            yield conversation
            self.profiles[user_id] = conversation.current_profile
            self.messages[user_id] = conversation.messages


class MemoryConversation:
    def __init__(self, profile, messages):
        self.current_profile = profile
        self.messages = messages

    async def profile(self):
        return self.current_profile

    async def history(self, limit):
        return self.messages[-limit:] if limit else []

    async def reset(self):
        self.messages.clear()

    async def change_mode(self, mode):
        self.current_profile = replace(self.current_profile, mode=mode)
        await self.reset()

    async def change_temperature(self, value):
        self.current_profile = replace(self.current_profile, temperature=value)

    async def save_turn(self, question, answer, limit):
        self.messages.extend(
            [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
        )
        self.messages = self.messages[-max(2, limit // 2 * 2) :]
