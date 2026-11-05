"""Режимы диалога без зависимости от Telegram."""

import uuid

from app.config import Settings
from app.context import build_context
from app.llm import Completion
from app.prompts import MODE_NAMES, PROMPTS
from app.response_style import enforce_style, styled_prompt

TEMPERATURES = {"0.0": 0.0, "0.3": 0.3, "0.7": 0.7, "1.0": 1.0}
RESET_TEXT = "История очищена. Режим и temperature сохранены."
INVALID_TEMPERATURE = "Допустимые значения: /settings 0.0, 0.3, 0.7 или 1.0."


class Assistant:
    def __init__(self, settings: Settings, repository, llm):
        self.settings = settings
        self.repository = repository
        self.llm = llm

    async def mode(self, user_id: int, mode: str) -> str:
        if mode not in PROMPTS:
            raise ValueError("unknown_mode")
        async with self.repository.session(user_id) as conversation:
            await conversation.change_mode(mode)
        return f"Режим: {MODE_NAMES[mode]}. История очищена."

    async def reset(self, user_id: int) -> str:
        async with self.repository.session(user_id) as conversation:
            await conversation.reset()
        return RESET_TEXT

    async def preferences(self, user_id: int, argument: str = "") -> str:
        if argument and argument not in TEMPERATURES:
            return INVALID_TEMPERATURE
        async with self.repository.session(user_id) as conversation:
            if argument:
                await conversation.change_temperature(TEMPERATURES[argument])
            profile = await conversation.profile()
        provider = self.settings.llm_provider
        model = getattr(self.settings, f"{provider}_model")
        backup = "groq" if provider == "openrouter" else "openrouter"
        reserve = (
            f"\nРезерв: {backup} / {getattr(self.settings, f'{backup}_model')}"
            if getattr(self.settings, f"{backup}_api_key")
            else ""
        )
        return (
            f"Режим: {MODE_NAMES[profile.mode]}\nМодель: {provider} / {model}"
            f"{reserve}\ntemperature: {profile.temperature:.1f}\n"
            "Изменить: /settings 0.0, 0.3, 0.7 или 1.0."
        )

    async def respond(self, user_id: int, text: str, *, allow_fallback: bool = True) -> Completion:
        async with self.repository.session(user_id) as conversation:
            profile = await conversation.profile()
            history = await conversation.history(self.settings.history_max_messages // 2 * 2)
            messages = build_context(
                styled_prompt(PROMPTS[profile.mode], profile.speech_style),
                history,
                text,
                max_messages=self.settings.history_max_messages,
                max_tokens=self.settings.history_max_tokens,
            )
            result = await self.llm.complete(
                messages, profile.temperature, str(uuid.uuid4()), allow_fallback=allow_fallback
            )
            result = await enforce_style(
                result,
                self.llm,
                style=profile.speech_style,
                mode=profile.mode,
                budget=self.settings.history_max_tokens,
                temperature=profile.temperature,
                allow_fallback=allow_fallback,
            )
            if result.truncated:
                result = Completion(
                    result.content + "\n\nОтвет прерван лимитом генерации. Попросите продолжить.",
                    result.model,
                    result.provider,
                    result.usage,
                    True,
                    result.cost,
                )
            await conversation.save_turn(text, result.content, self.settings.history_max_messages)
        return result
