import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.agent import Agent
from app.assistant import Assistant
from app.config import Settings
from app.handlers.assistant import START_TEXT, create_router
from app.llm import Completion, ToolCall
from app.orchestration.policy import PolicyBridge
from app.orchestration.supervisor import ROUTER_PROMPT, Supervisor
from app.prompts import PROMPTS
from app.repository import Profile
from app.response_style import (
    PLAIN_STYLE,
    YIDDISH_STYLE,
    enforce_style,
    mask_literals,
    restore_literals,
    strong_style,
)
from app.speech_style import SpeechStyles
from tests.fakes import MemoryRepository
from tests.test_supervisor import NOW, MemoryAssistantStore, bridge


@pytest.mark.parametrize("mode", ["study", "translate", "review"])
async def test_style_decorates_each_mode_preserves_history_and_does_not_leak_to_other_owner(mode):
    repository = MemoryRepository()
    repository.profiles[101] = Profile(mode=mode, temperature=0.7, speech_style="yiddish")
    repository.messages[101] = [
        {"role": "user", "content": "Ранее"},
        {"role": "assistant", "content": "Ответ"},
    ]
    llm = Mock(complete=AsyncMock(return_value=Completion("Ответ", "fake", "fake")))
    service = Assistant(Settings(bot_token="unused", postgres_password="unused"), repository, llm)
    result = await service.respond(101, "Текущий вопрос")
    assert llm.complete.await_count == 1
    assert result.content == "Ответ"
    assert repository.messages[101][-1]["content"] == result.content
    messages = llm.complete.call_args.args[0]
    assert messages[0]["content"].startswith(PROMPTS[mode])
    assert YIDDISH_STYLE in messages[0]["content"]
    assert messages[1]["content"] == "Ранее" and messages[-1]["content"] == "Текущий вопрос"
    assert llm.complete.call_args.args[1] == 0.7
    await service.respond(202, "Другой человек")
    assert llm.complete.call_args.args[0][0]["content"] == PROMPTS["study"]
    await service.mode(101, "review")
    await service.reset(101)
    assert repository.profiles[101].speech_style == "yiddish"
    # Off overrides styled old history without deleting it or changing mode.
    repository.profiles[101] = replace(repository.profiles[101], speech_style="plain")
    repository.messages[101] = [
        {"role": "user", "content": "Вопрос"},
        {"role": "assistant", "content": "Ой-вей, таки ответ"},
    ]
    await service.respond(101, "Теперь обычно")
    messages = llm.complete.call_args.args[0]
    assert PLAIN_STYLE in messages[0]["content"] and YIDDISH_STYLE not in messages[0]["content"]
    assert "Ой-вей" in messages[2]["content"]


async def test_native_tool_arguments_do_not_receive_style_prompt():
    raw = bridge()
    llm = Mock(
        complete=AsyncMock(
            side_effect=[
                Completion(
                    json.dumps(
                        {
                            "action": "call_tool",
                            "tool_name": "get_schedule",
                            "answer": "",
                            "reason": "schedule",
                        }
                    ),
                    "fake",
                    "fake",
                ),
                Completion(
                    "",
                    "fake",
                    "fake",
                    tool_calls=(ToolCall("call", "get_schedule", {"date": "2026-11-02"}),),
                ),
            ]
        )
    )
    agent = Agent(Settings(bot_token="unused", postgres_password="unused"), Mock(), llm, raw)
    result = await agent.choose(
        [{"role": "system", "content": "База"}, {"role": "user", "content": "Пары сегодня?"}],
        0.3,
        timezone="Europe/Moscow",
        speech_style="yiddish",
    )
    first, second = llm.complete.call_args_list
    assert YIDDISH_STYLE in first.args[0][0]["content"]
    assert YIDDISH_STYLE not in second.args[0][0]["content"]
    assert first.kwargs["response_schema"]["properties"]["action"]
    assert result.tool_calls[0].arguments == {"date": "2026-11-02"}


async def test_router_unchanged_knowledge_styled_and_schedule_facts_not_rewritten():
    repository = MemoryRepository()
    llm = Mock(
        complete=AsyncMock(
            return_value=Completion(
                json.dumps(
                    {
                        "target": "knowledge",
                        "intent": "answer",
                        "scope": "default",
                        "confidence": 1.0,
                        "reason": "materials",
                    }
                ),
                "fake",
                "fake",
            )
        )
    )
    assistant = Assistant(Settings(bot_token="unused", postgres_password="unused"), repository, llm)
    raw = bridge()
    service = Supervisor(assistant.settings, MemoryAssistantStore(), assistant, raw)
    await service.route("Как материал про MCP объясняет контракт?")
    assert llm.complete.call_args.args[0][0]["content"] == ROUTER_PROMPT
    raw.call.return_value = {
        "status": "ok",
        "source": "local_materials",
        "hits": [
            {"doc_id": "mcp-basics", "title": "MCP", "excerpt": "MCP задаёт контракт инструментов."}
        ],
    }
    llm.complete.return_value = Completion("Таки контракт [mcp-basics]", "fake", "fake")
    from app.orchestration.contracts import AgentRequest

    request = AgentRequest(
        request_id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
        actor_id=101,
        text="Объясни по конспекту MCP",
        intent="user_query",
        occurred_at=NOW,
        timezone="Europe/Moscow",
    )
    result = await service.knowledge(request, PolicyBridge(raw, "knowledge", owner=101), "yiddish")
    assert YIDDISH_STYLE in llm.complete.call_args.args[0][0]["content"]
    assert result.message.endswith("[mcp-basics]")
    facts = "📅 2026-11-02 · 09:30–11:00 · 301\n```python\nx = 42\n```"
    masked, saved = mask_literals(facts)
    assert restore_literals(masked, saved) == facts


async def test_hidden_private_commands_and_foreign_callback():
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    identity = uuid.uuid4()
    styles = Mock(
        command=AsyncMock(return_value=("Confirm", identity)), confirm=AsyncMock(return_value="OK")
    )
    assistant = Mock(respond=AsyncMock())
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router())
    message = Message(
        message_id=17,
        date=datetime.now(UTC),
        chat=Chat(id=101, type="private"),
        from_user=User(id=101, first_name="Test", is_bot=False),
        text="/jew on",
    )
    await dispatcher.feed_update(
        bot, Update(update_id=1, message=message), speech_styles=styles, assistant=assistant
    )
    styles.command.assert_awaited_once_with(101, "on", "telegram:101:17")
    assistant.respond.assert_not_called()
    assert "/jew" not in START_TEXT
    query = CallbackQuery(
        id="foreign",
        chat_instance="x",
        from_user=User(id=202, first_name="Other", is_bot=False),
        message=message,
        data="speech:confirm:" + str(identity),
    )
    await dispatcher.feed_update(
        bot, Update(update_id=2, callback_query=query), speech_styles=styles
    )
    styles.confirm.assert_not_called()
    query = query.model_copy(update={"from_user": message.from_user, "id": "own"})
    await dispatcher.feed_update(
        bot, Update(update_id=3, callback_query=query), speech_styles=styles
    )
    styles.confirm.assert_awaited_once_with(101, identity, cancel=False)
    styles.command.reset_mock()
    group = message.model_copy(update={"chat": Chat(id=-123, type="group")})
    await dispatcher.feed_update(bot, Update(update_id=4, message=group), speech_styles=styles)
    styles.command.assert_not_called()
    await bot.session.close()


async def test_invalid_style_command_has_no_write():
    store = Mock(propose=AsyncMock())
    service = SpeechStyles(store, MemoryRepository())
    _, pending = await service.command(101, "on user_id=202", "msg")
    assert pending is None
    store.propose.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        '```python\nprint("Ответ")\n```',
        '{"answer":"Ответ","time":"09:30"}',
        '["Ответ", 42]',
        '"Ответ"',
        "> Это точная цитата.",
        "The answer is an object with methods.",
        "",
    ],
)
async def test_style_editor_preserves_code_structured_output_quotes_and_language(body):
    llm = Mock(complete=AsyncMock())
    original = Completion(body, "fake", "fake")
    assert await enforce_style(original, llm, style="yiddish") == original
    llm.complete.assert_not_called()


NEUTRAL = (
    "Переменная сохраняет значение, к которому обращаются по имени.\n\n"
    "Функция выполняет действие и возвращает результат вычисления.\n\n"
    "Повторное использование позволяет не писать одно действие несколько раз."
)


def test_style_is_required_across_body_not_only_introduction():
    assert not strong_style(NEUTRAL)
    assert not strong_style("Ну, таки вот:\n\n" + NEUTRAL)
    assert not strong_style("Таки начнём. " + NEUTRAL)
    assert strong_style("\n\n".join("Вы таки спросите: " + p for p in NEUTRAL.split("\n\n")))


def test_literary_simile_can_finish_an_already_styled_explanation():
    text = "\n\n".join("Вы таки спросите: " + p for p in NEUTRAL.split("\n\n"))
    assert strong_style(text + "\n\nВаш код — как шутка в одесском кафе: весёлый и без сюрпризов.")


def test_coherent_paragraph_is_not_failed_by_arbitrary_word_boundary():
    half = "объясняем понятный принцип работы программы " * 8
    paragraph = "Вы таки спросите: " + half + "Так я вам скажу: " + half
    assert strong_style(paragraph)
    assert not strong_style("Вы таки спросите: " + "обычный нейтральный учебный текст " * 150)


def test_editor_cannot_move_fenced_code_into_a_sentence():
    masked, saved = mask_literals("```python\nx = 42\n```")
    assert restore_literals(masked, saved) == "```python\nx = 42\n```"
    for prefix, suffix in [("И наконец, ", ""), ("", " — шоб всё было понятно.")]:
        with pytest.raises(ValueError, match="code_layout"):
            restore_literals(prefix + masked + suffix, saved)


async def test_weak_style_is_rewritten_once_without_tools_and_usage_is_aggregated():
    async def edit(messages, *args, **kwargs):
        draft = messages[-1]["content"].split("\n", 1)[1]
        return Completion(
            "\n\n".join("Вы таки спросите: " + p for p in draft.split("\n\n")),
            "editor",
            "fake",
            {"prompt_tokens": 20},
            cost=0.0,
        )

    llm = Mock(complete=AsyncMock(side_effect=edit))
    original = Completion(NEUTRAL, "original", "fake", {"prompt_tokens": 10}, cost=0.0)
    result = await enforce_style(original, llm, style="yiddish")
    assert result != original and strong_style(result.content)
    assert not result.content.startswith("Ну, таки вот")
    assert result.usage == {"prompt_tokens": 30} and result.cost == 0.0
    llm.complete.assert_awaited_once()
    assert llm.complete.call_args.kwargs["tools"] == []
    assert "response_schema" not in llm.complete.call_args.kwargs
    assert await enforce_style(result, llm, style="yiddish") == result
    assert llm.complete.await_count == 1


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "reorder", "number", "code", "citation"]
)
def test_literal_guard_rejects_modified_or_invented_immutable_data(change):
    body = "Санкт-Петербург, 2026-11-02 09:30 [mcp-basics]\n```python\nx = 42\n```"
    masked, saved = mask_literals(body)
    assert restore_literals(masked, saved) == body
    token = saved[0][0]
    bad = {
        "missing": masked.replace(token, ""),
        "duplicate": token + masked,
        "reorder": masked.replace(token, "") + token,
        "number": masked + " 99",
        "code": masked + "\n`delete_all()`",
        "citation": masked + " [fake-source]",
    }[change]
    with pytest.raises(ValueError):
        restore_literals(bad, saved)


async def test_invalid_editor_or_budget_cannot_damage_valid_answer_or_loop():
    original = Completion(NEUTRAL + " 42", "fake", "fake")
    llm = Mock(
        complete=AsyncMock(return_value=Completion("Таки удалим все данные.", "fake", "fake"))
    )
    assert await enforce_style(original, llm, style="yiddish") == original
    llm.complete.assert_awaited_once()
    llm.complete.reset_mock()
    for kwargs in (
        {"style": "plain"},
        {"style": "yiddish", "mode": "translate"},
        {"style": "yiddish", "budget": 1},
    ):
        assert await enforce_style(original, llm, **kwargs) == original
    llm.complete.assert_not_called()
