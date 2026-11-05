"""Сокращение контекста целыми парами, без потери текущего запроса."""

from collections.abc import Sequence


class ContextTooLarge(ValueError):
    pass


def estimate_tokens(messages: Sequence[dict[str, str]]) -> int:
    # UTF-8 bytes / 3 — оценка, а не токенизатор конкретного провайдера.
    return sum((len(m["content"].encode("utf-8")) + 2) // 3 + 8 for m in messages)


def build_context(
    system: str,
    history: list[dict[str, str]],
    current: str,
    *,
    max_messages: int,
    max_tokens: int,
) -> list[dict[str, str]]:
    instruction = {"role": "system", "content": system}
    question = {"role": "user", "content": current}
    if estimate_tokens([instruction, question]) > max_tokens:
        raise ContextTooLarge
    retained = list(history)
    while retained and (
        len(retained) + 1 > max_messages
        or estimate_tokens([instruction, *retained, question]) > max_tokens
    ):
        # Хранилище возвращает успешные полные ходы user → assistant.
        del retained[:2]
    return [instruction, *retained, question]


def split_telegram(text: str, limit: int = 4096) -> list[str]:
    """Сохраняет весь текст; emoji считаются как две UTF-16 единицы."""
    parts, chunk, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            parts.append("".join(chunk))
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        parts.append("".join(chunk))
    return parts
