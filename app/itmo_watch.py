"""Проверка будущего расписания и сравнение только общей части двух выгрузок."""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from app.db import job_owners, owner_scope
from app.itmo import ITMOError
from app.timezones import zone

MOSCOW = zone("Europe/Moscow")


def _describe(entry):
    day, start, end, title, location = entry
    return f"{day[8:10]}.{day[5:7]} {start[:5]}–{end[:5]} · {title} · {location}"


def changes(previous, current, now):
    if previous is None:
        return None
    first = max(previous["first"], current["first"])
    last = min(previous["last"], current["last"])

    def future(snapshot):
        return {
            (row["date"], row["start"], row["end"], row["title"], row["location"])
            for row in snapshot["entries"]
            if first <= row["date"] <= last
            and datetime.fromisoformat(f"{row['date']}T{row['start']}").replace(tzinfo=MOSCOW) > now
        }

    before, after = future(previous), future(current)
    removed, added = before - after, after - before
    descriptions = []
    # Повторяющиеся пары сопоставляются только при однозначном совпадении.
    for indices in ((0, 1, 2, 3), (0, 3), (3,)):
        left, right = defaultdict(list), defaultdict(list)
        for entry in removed:
            left[tuple(entry[index] for index in indices)].append(entry)
        for entry in added:
            right[tuple(entry[index] for index in indices)].append(entry)
        for key in sorted(left.keys() & right.keys()):
            if len(left[key]) == len(right[key]) == 1:
                old, new = left[key][0], right[key][0]
                label = "Смена аудитории" if old[:3] == new[:3] else "Перенос"
                descriptions.append(f"• {label}\n  {_describe(old)}\n  → {_describe(new)}")
                removed.remove(old)
                added.remove(new)
    descriptions.extend(f"• Убрано из расписания · {_describe(entry)}" for entry in sorted(removed))
    descriptions.extend(f"• Добавлено · {_describe(entry)}" for entry in sorted(added))
    if not descriptions:
        return None
    shown = descriptions[:3]
    if len(descriptions) > len(shown):
        shown.append(f"Ещё изменений: {len(descriptions) - len(shown)}. Откройте расписание ИТМО.")
    return "🔔 Изменения в расписании ИТМО\n\n" + "\n\n".join(shown)


class ItmoWatch:
    def __init__(self, store, client, *, clock=None):
        self.store, self.client = store, client
        self.clock = clock or (lambda: datetime.now(UTC))

    async def tick(self):
        claimed = False
        for owner in await job_owners(getattr(self.store, "pool", None)):
            with owner_scope(owner):
                claimed = await self.tick_owner() or claimed
        return claimed

    async def tick_owner(self):
        now = self.clock()
        claim = await self.store.claim(now)
        if claim is None:
            return False
        owner = claim["user_id"]
        if not self.client.connected(owner):
            return True
        first = now.astimezone(MOSCOW).date()
        last = first + timedelta(days=6)
        try:
            async with asyncio.timeout(50):
                entries = await self.client.fetch(owner, first, last)
        except (ITMOError, TimeoutError):
            return True
        snapshot = {"first": first.isoformat(), "last": last.isoformat(), "entries": entries}
        now = self.clock()
        await self.store.save(claim, snapshot, changes(claim["snapshot"], snapshot, now), now)
        return True

    async def run(self, stopped):
        while not stopped.is_set():
            try:
                if await self.tick():
                    continue
            except Exception:
                # Подробности выгрузки и личный идентификатор остаются приватными.
                pass
            try:
                await asyncio.wait_for(stopped.wait(), 30)
            except TimeoutError:
                pass
