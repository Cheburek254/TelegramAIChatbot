import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, User

from app.handlers.assistant import create_router
from app.handlers.features import BoundedUpload, create_feature_router, materials_keyboard
from app.material_service import PersonalMaterials, parse_briefing_time
from app.mcp_service.materials import Materials
from app.orchestration.policy import PolicyBridge
from app.orchestration.scheduler import briefing_due
from tests.test_supervisor import bridge


@pytest.mark.parametrize("text", ["24:00", "09:60", "9:30", "09:30 UTC", "-1:30", "9", ""])
def test_invalid_briefing_time(text):
    with pytest.raises(ValueError):
        parse_briefing_time(text)


def test_custom_time_midnight_and_dst():
    assert parse_briefing_time("00:00") == 0
    assert parse_briefing_time("23:59") == 1439
    now = datetime(2026, 11, 2, 6, 29, tzinfo=UTC)
    assert briefing_due(now, "Europe/Moscow", 570) is None
    now = now.replace(minute=30)
    assert briefing_due(now, "Europe/Moscow", 570).hour == 9
    spring = datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    assert briefing_due(spring, "Europe/Berlin", 150).hour == 3
    fall = datetime(2026, 10, 25, 1, 10, tzinfo=UTC)
    assert briefing_due(fall, "Europe/Berlin", 150).fold == 0


async def test_policy_binds_materials_to_authenticated_owner():
    raw = bridge()
    policy = PolicyBridge(raw, "knowledge", owner=101)
    await policy.call("search_materials", {"query": "конспект"}, owner=999)
    assert raw.call.call_args.kwargs["owner"] == 101
    await policy.resource("materials://catalog", owner=999)
    raw.read_catalog.assert_awaited_once_with(101)


def test_material_chunks_find_tail_and_keep_markdown_body():
    text = "# Лекция\n" + "обычный текст " * 2500 + "\n\nКсенобиология изучает чужую жизнь."
    result = Materials().search(
        "ксенобиология", personal=[{"doc_id": "u-test", "title": "Лекция", "content": text}]
    )
    assert len(result.hits) == 1 and result.hits[0].doc_id == "u-test"
    assert "Ксенобиология" in result.hits[0].excerpt
    assert len(result.hits[0].excerpt) <= 1600
    assert Materials().search("ксенобиология").hits == []


async def test_add_is_pending_and_foreign_delete_is_not_proposed():
    store = Mock(
        documents=AsyncMock(return_value=[]), proposal=AsyncMock(return_value=uuid.uuid4())
    )
    service = PersonalMaterials(store)
    text, identity = await service.command(101, "message-1", "add Лекция | Личный текст")
    assert identity and "провайдеру модели" in text
    assert store.proposal.call_args.args[:3] == (101, "message-1", "material_add")
    store.proposal.reset_mock()
    text, identity = await service.command(202, "message-2", "delete u-foreign")
    assert identity is None and "не найден" in text
    store.proposal.assert_not_called()
    assert (await service.add(101, "bad", "X", "x" * 50_001))[1] is None
    store.proposal.assert_not_called()


def test_stream_rejects_oversize_even_if_telegram_size_missing():
    stream = BoundedUpload()
    stream.write(b"x" * 200_000)
    with pytest.raises(ValueError):
        stream.write(b"x")


async def test_upload_utf8_private_owner_and_foreign_confirmation(monkeypatch):
    bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    bot.session = AsyncMock()
    download = AsyncMock(
        side_effect=lambda doc, destination: destination.write("Содержание".encode())
    )
    monkeypatch.setattr(bot, "download", download)
    materials = Mock(add=AsyncMock(return_value=("Confirm", uuid.uuid4())), confirm=AsyncMock())
    notifications = Mock(time=AsyncMock(return_value=("Time", uuid.uuid4())))
    dispatcher = Dispatcher()
    dispatcher.include_router(create_feature_router(notifications, Mock(), materials))
    dispatcher.include_router(create_router())
    message = Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=101, type="private"),
        from_user=User(id=101, is_bot=False, first_name="Test"),
        document=Document(file_id="id", file_unique_id="unique", file_name="../Лекция.md"),
    )
    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
    materials.add.assert_awaited_once_with(101, "telegram:101:10", "Лекция", "Содержание")
    query = CallbackQuery(
        id="foreign",
        chat_instance="x",
        from_user=User(id=202, is_bot=False, first_name="Other"),
        message=message,
        data="content:confirm:" + str(uuid.uuid4()),
    )
    await dispatcher.feed_update(bot, Update(update_id=2, callback_query=query))
    materials.confirm.assert_not_called()
    query = query.model_copy(
        update={"id": "preset", "from_user": message.from_user, "data": "features:at:09:00"}
    )
    await dispatcher.feed_update(bot, Update(update_id=3, callback_query=query))
    notifications.time.assert_awaited_once_with(101, "callback:preset", "09:00")
    back = query.model_copy(
        update={"id": "back", "data": materials_keyboard().inline_keyboard[-1][0].callback_data}
    )
    agent = Mock(tools=AsyncMock(return_value="Tools"))
    await dispatcher.feed_update(bot, Update(update_id=20, callback_query=back), agent=agent)
    agent.tools.assert_awaited_once()
    # Unsupported/oversize documents and groups never download or stage content.
    download.reset_mock()
    materials.add.reset_mock()
    for index, update in enumerate(
        [
            {"document": message.document.model_copy(update={"file_name": "x.pdf"})},
            {"document": message.document.model_copy(update={"file_size": 200_001})},
            {"chat": Chat(id=-321, type="group")},
        ],
        start=4,
    ):
        await dispatcher.feed_update(
            bot, Update(update_id=index, message=message.model_copy(update=update))
        )
    download.assert_not_called()
    materials.add.assert_not_called()
    await bot.session.close()
