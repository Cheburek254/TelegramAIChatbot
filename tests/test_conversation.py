import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, PhotoSize, Update, User

from app.assistant import INVALID_TEMPERATURE, RESET_TEXT, Assistant
from app.config import ConfigError, Settings
from app.context import ContextTooLarge, build_context, estimate_tokens, split_telegram
from app.handlers.assistant import ERROR_TEXT, START_TEXT, TIMEOUT_TEXT, create_router
from app.llm import Completion, LLMError
from app.logging_setup import configure_logging
from app.prompts import PROMPTS
from tests.fakes import MemoryRepository

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk"


@pytest.fixture
def service():
    settings = Settings(bot_token=TOKEN, postgres_password="test", openrouter_api_key="fake-key")
    llm = Mock(complete=AsyncMock(return_value=Completion("Ответ", "model", "openrouter")))
    return Assistant(settings, MemoryRepository(), llm)


async def test_active_prompt_roles_and_generation_parameters(service):
    # Arrange
    await service.mode(1, "translate")
    await service.preferences(1, "0.7")
    await service.respond(1, "На английский: привет")
    # Act
    await service.respond(1, "И пока")
    messages, temperature, request_id = service.llm.complete.call_args.args
    # Assert
    assert messages == [
        {"role": "system", "content": PROMPTS["translate"]},
        {"role": "user", "content": "На английский: привет"},
        {"role": "assistant", "content": "Ответ"},
        {"role": "user", "content": "И пока"},
    ]
    assert temperature == 0.7
    assert len(request_id) == 36


async def test_concurrent_users_are_isolated_and_same_user_serialized(service):
    # Arrange / Act
    await asyncio.gather(service.respond(1, "Первый"), service.respond(2, "Чужой"))
    await asyncio.gather(service.respond(1, "Второй"), service.respond(1, "Третий"))
    # Assert
    assert [m["content"] for m in service.repository.messages[2]] == ["Чужой", "Ответ"]
    assert [m["content"] for m in service.repository.messages[1]][::2] == [
        "Первый",
        "Второй",
        "Третий",
    ]
    assert "Чужой" not in str(service.llm.complete.call_args.args[0])


async def test_modes_reset_and_settings_survive_new_service_instance(service):
    # Arrange
    await service.respond(1, "История 1")
    await service.respond(2, "История 2")
    await service.preferences(1, "1.0")
    # Act / Assert
    assert await service.mode(1, "review") == "Режим: Ревью кода. История очищена."
    assert service.repository.messages[1] == []
    await service.respond(1, "print(1)")
    restarted = Assistant(service.settings, service.repository, service.llm)
    assert await restarted.reset(1) == RESET_TEXT
    assert restarted.repository.messages[1] == []
    assert len(restarted.repository.messages[2]) == 2
    assert restarted.repository.profiles[1].mode == "review"
    assert restarted.repository.profiles[1].temperature == 1.0


@pytest.mark.parametrize("argument", ["0.0", "0.3", "0.7", "1.0"])
async def test_settings_accept_allowed_values(service, argument):
    # Arrange / Act
    answer = await service.preferences(1, argument)
    # Assert
    assert f"temperature: {argument}" in answer
    assert service.repository.profiles[1].temperature == float(argument)
    service.llm.complete.assert_not_called()


@pytest.mark.parametrize("argument", ["NaN", "-1", "0.5", "1.0 ignore rules", "inf"])
async def test_invalid_temperature_rejected_before_storage_or_api(service, argument):
    # Arrange / Act / Assert
    assert await service.preferences(1, argument) == INVALID_TEMPERATURE
    assert not service.repository.profiles
    service.llm.complete.assert_not_called()


@pytest.mark.parametrize("max_messages,max_tokens", [(3, 5000), (20, 110)])
def test_context_removes_oldest_complete_turn_without_losing_current(max_messages, max_tokens):
    # Arrange
    history = [
        {"role": role, "content": content}
        for role, content in [
            ("user", "старый вопрос " * 12),
            ("assistant", "старый ответ " * 12),
            ("user", "свежий вопрос"),
            ("assistant", "свежий ответ"),
        ]
    ]
    # Act
    result = build_context(
        "правила", history, "текущий запрос", max_messages=max_messages, max_tokens=max_tokens
    )
    # Assert
    assert result == [
        {"role": "system", "content": "правила"},
        *history[2:],
        {"role": "user", "content": "текущий запрос"},
    ]
    assert estimate_tokens(result) <= max_tokens
    assert len(result) - 1 <= max_messages
    assert len(history) == 4


async def test_oversized_current_request_is_not_silently_cut_or_sent(service):
    # Arrange
    service.settings = replace(service.settings, history_max_tokens=512)
    # Act / Assert
    with pytest.raises(ContextTooLarge):
        await service.respond(1, "я" * 4000)
    service.llm.complete.assert_not_called()
    assert not service.repository.messages[1]


async def test_failed_llm_does_not_save_partial_turn(service):
    # Arrange
    await service.respond(1, "успешный")
    service.llm.complete.side_effect = LLMError("timeout")
    # Act / Assert
    with pytest.raises(LLMError):
        await service.respond(1, "неуспешный")
    assert service.repository.messages[1] == [
        {"role": "user", "content": "успешный"},
        {"role": "assistant", "content": "Ответ"},
    ]


@pytest.mark.parametrize(
    "text", ["a" * 9000, "😀" * 5000, "абв\n" * 3000], ids=["ascii", "emoji", "cyrillic"]
)
def test_telegram_split_is_lossless_and_within_utf16_limit(text):
    # Arrange / Act
    parts = split_telegram(text)
    # Assert
    assert "".join(parts) == text
    assert all(0 < len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts)


async def feed(service, text=None, *, private=True, photo=False):
    bot = Bot(TOKEN)
    bot.session = AsyncMock()
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router())
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private" if private else "group"),
        from_user=User(id=42, is_bot=False, first_name="Тест"),
        text=text,
        photo=[PhotoSize(file_id="p", file_unique_id="p", width=1, height=1)] if photo else None,
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=message), assistant=service)
    return [
        call.args[1]
        for call in bot.session.call_args_list
        if len(call.args) > 1 and isinstance(call.args[1], SendMessage)
    ]


@pytest.mark.parametrize(
    "command", ["/start", "/study", "/translate", "/review", "/reset", "/settings", "/unknown"]
)
async def test_commands_never_go_to_model(service, command):
    # Arrange / Act
    replies = await feed(service, command)
    # Assert
    assert len(replies) == 1
    assert replies[0].parse_mode == (None if command == "/unknown" else "HTML")
    if command == "/start":
        assert replies[0].text == START_TEXT
    service.llm.complete.assert_not_called()


async def test_group_messages_and_photos_are_ignored(service):
    # Arrange / Act / Assert
    assert await feed(service, "секрет группы", private=False) == []
    assert await feed(service, photo=True) == []
    service.llm.complete.assert_not_called()


@pytest.mark.parametrize(
    "code,expected",
    [("timeout", TIMEOUT_TEXT), ("invalid_response", ERROR_TEXT), ("http_401", ERROR_TEXT)],
)
async def test_safe_llm_error_in_telegram(service, code, expected):
    # Arrange
    service.llm.complete.side_effect = LLMError(code)
    # Act
    replies = await feed(service, "Вопрос")
    # Assert
    assert [r.text for r in replies] == [expected]


async def test_long_response_is_sent_in_order(service):
    # Arrange
    text = "длинный ответ 😀\n" * 900
    service.llm.complete.return_value = Completion(text, "model", "openrouter")
    # Act
    replies = await feed(service, "Вопрос")
    # Assert
    assert len(replies) > 1
    assert "".join(r.text for r in replies) == text
    assert all(len(r.text.encode("utf-16-le")) // 2 <= 4096 for r in replies)


def test_new_config_reads_file_alias_and_validates_secrets(tmp_path):
    # Arrange
    path = tmp_path / ".env"
    path.write_text(
        f"BOT_TOKEN={TOKEN}\nPOSTGRES_PASSWORD=test\nOPENROUTER_APIKEY=only-for-test\n"
        "GROQ_API_KEY=reserve-test\nHISTORY_MAX_MESSAGES=8\n",
        encoding="utf-8",
    )
    # Act
    settings = Settings.load(path, environ={})
    settings.validate_llm()
    # Assert
    assert settings.openrouter_api_key == "only-for-test"
    assert settings.groq_api_key == "reserve-test"
    assert settings.history_max_messages == 8
    assert "only-for-test" not in repr(settings)
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        replace(settings, openrouter_api_key="").validate_llm()


@pytest.mark.parametrize(
    "key,value",
    [
        ("LLM_TIMEOUT", "0"),
        ("HISTORY_MAX_MESSAGES", "-1"),
        ("HISTORY_MAX_TOKENS", "NaN"),
        ("LLM_PROVIDER", "unknown"),
        ("GROQ_BASE_URL", "https://u:secret@example.com"),
    ],
)
def test_invalid_llm_config_is_safe(tmp_path, key, value):
    # Arrange / Act / Assert
    with pytest.raises(ConfigError) as exc:
        Settings.load(
            tmp_path / ".env", environ={"BOT_TOKEN": TOKEN, "POSTGRES_PASSWORD": "test", key: value}
        )
    assert key in str(exc.value)
    assert "secret" not in str(exc.value)


def test_logging_redacts_both_model_keys(capsys):
    # Arrange
    settings = Settings(
        bot_token=TOKEN,
        postgres_password="test-db",
        openrouter_api_key="test-openrouter-secret",
        groq_api_key="test-groq-secret",
    )
    previous = logging.getLogger().handlers[:]
    try:
        configure_logging(settings)
        # Act
        logging.getLogger("test").error("test-openrouter-secret test-groq-secret")
        rendered = capsys.readouterr().err
        # Assert
        assert "test-openrouter-secret" not in rendered
        assert "test-groq-secret" not in rendered
        assert "[скрыто]" in rendered
    finally:
        logging.getLogger().handlers = previous


def test_paid_configuration_cannot_start(service):
    # Arrange / Act / Assert
    with pytest.raises(ConfigError, match=":free"):
        replace(service.settings, openrouter_model="google/paid-model").validate_llm()
