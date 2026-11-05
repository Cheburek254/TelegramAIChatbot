"""Фиксированный корпус: пользователь не выбирает имя файла или каталог."""

import re
from pathlib import Path

from app.material_contracts import Catalog, CatalogItem, MaterialHit, SearchOutput

DOCUMENTS = {
    "mcp-basics": ("MCP и границы инструментов", "mcp.md"),
    "llm-context": ("Контекст и структурированный вывод", "context.md"),
    "reliable-delivery": ("Идемпотентность и доставка", "delivery.md"),
}


def terms(text):
    return {word[:6] for word in re.findall(r"[а-яёa-z0-9]{3,}", text.lower())}


class Materials:
    def __init__(self, root="assets/materials"):
        self.root = Path(root)

    def catalog(self):
        return Catalog(documents=[CatalogItem(doc_id=k, title=v[0]) for k, v in DOCUMENTS.items()])

    def search(self, query, limit=5, personal=()):
        documents = []
        for identity, (title, filename) in DOCUMENTS.items():
            path = self.root / filename
            if path.is_symlink():
                raise ValueError("invalid_corpus")
            documents.append((identity, title, path.read_text(encoding="utf-8")[:12000]))
        documents.extend((row["doc_id"], row["title"], row["content"]) for row in personal)
        return search_documents(query, limit, documents)


def search_documents(query, limit, documents):
    wanted = terms(query) - terms("найди объясни материал документ конспект назначение правила")
    found = []
    for identity, title, text in documents:
        # Bound chunks even for uploaded files with no blank lines; retain body
        # below a Markdown heading when it shares the same paragraph.
        body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
        paragraphs = [p[i : i + 1500] for p in paragraphs for i in range(0, len(p), 1300)]
        ranked = [(len(wanted & terms(p)), p) for p in paragraphs]
        score, excerpt = max(ranked, default=(0, ""), key=lambda p: p[0])
        if score:
            found.append((score, MaterialHit(doc_id=identity, title=title, excerpt=excerpt[:1600])))
    found.sort(key=lambda p: (-p[0], p[1].doc_id))
    hits = [item for _, item in found[:limit]]
    return SearchOutput(status="ok" if hits else "not_found", hits=hits)
