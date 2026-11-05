"""Добавление личных конспектов без выбора файловых путей или владельца моделью."""

import re
import uuid
from datetime import UTC, datetime

from app.material_contracts import Catalog
from app.mcp_service.materials import Materials

MAX_FILE_BYTES = 200_000
HELP = (
    "📚 Личные материалы\n\n"
    "Пришлите файл .txt или .md в UTF-8 (до 200 КБ и 50 000 символов), "
    "или текст командой:\n/materials add Название | Текст конспекта\n\n"
    "До 20 личных материалов. Добавление и удаление — после подтверждения. "
    "Вопрос: «По моим материалам объясни …». Подходящие выдержки отправляются "
    "провайдеру модели для ответа.\n\n"
    "/materials — каталог\n/materials delete ID — удалить свой материал"
)


class PersonalMaterials:
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))

    async def add(self, owner, key, title, content):
        title, content = title.strip(), content.strip()
        if not 1 <= len(title) <= 140 or any(ord(c) < 32 for c in title):
            return "Название должно содержать 1–140 символов без переносов строк.", None
        if not 1 <= len(content) <= 50_000 or "\x00" in content:
            return "Нужен текст без нулевых байтов длиной от 1 до 50 000 символов.", None
        if len(await self.store.documents(owner)) >= 20:
            return "Достигнут предел 20 материалов. Сначала удалите ненужный.", None
        identity = await self.store.proposal(
            owner, key, "material_add", {"title": title, "content": content}, self.clock()
        )
        return (
            f"📄 Добавить личный материал «{title}»?\n\n"
            f"Объём: {len(content)} символов.\nФрагмент:\n{content[:350]}\n\n"
            "Доступен только вам. При вопросе по материалам подходящие выдержки будут "
            "отправлены провайдеру модели. Подтверждение действует 5 минут.",
            identity,
        )

    async def command(self, owner, key, text):
        if text.startswith("add "):
            title, sep, content = text[4:].partition("|")
            if not sep:
                return HELP, None
            return await self.add(owner, key, title, content)
        if text.startswith("delete "):
            identity = text[7:].strip()
            documents = await self.store.documents(owner)
            row = next((r for r in documents if r["doc_id"] == identity), None)
            if row is None:
                return "Личный материал не найден. Список: /materials", None
            pending = await self.store.proposal(
                owner, key, "material_delete", {"doc_id": identity}, self.clock()
            )
            return f"🗑 Удалить ваш материал «{row['title']}»? Подтверждение — 5 минут.", pending
        if text:
            return HELP, None
        documents = await self.store.documents(owner)
        text = "📚 Общие материалы\n\n" + "\n\n".join(
            f"• [{d.doc_id}] {d.title}" for d in Materials().catalog().documents
        )
        text += "\n\n👤 Ваши материалы\n\n" + (
            "\n\n".join(f"• [{d['doc_id']}] {d['title']}" for d in documents)
            or "Пока ничего не добавлено."
        )
        return text + "\n\n" + HELP, None

    async def confirm(self, owner, identity, cancel=False):
        result = await self.store.confirm(owner, uuid.UUID(str(identity)), self.clock(), cancel)
        return {
            "material_add": "✅ Материал сохранён. Спросите: «По моим материалам объясни …».",
            "material_delete": "🗑 Личный материал удалён.",
            "briefing_time": "✅ Время сводки сохранено. Проверить: /notifications.",
            "quota": "Достигнут предел 20 материалов. Удалите ненужный.",
            "cancelled": "Действие отменено.",
        }.get(result, "Действие не выполнено: карточка устарела или уже обработана.")


async def personal_catalog(store, owner):
    public = Materials().catalog()
    return Catalog.model_validate(
        {
            "documents": [d.model_dump() for d in public.documents]
            + [
                {"doc_id": row["doc_id"], "title": row["title"]}
                for row in await store.documents(owner)
            ]
        }
    )


def parse_briefing_time(text):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError("invalid_time")
    hour, minute = map(int, text.split(":"))
    return hour * 60 + minute


def format_briefing_time(minute):
    return f"{minute // 60:02}:{minute % 60:02}"
