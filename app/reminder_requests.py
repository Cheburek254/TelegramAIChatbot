"""Elapsed reminder times are computed from the application's clock."""

import re
from dataclasses import dataclass
from datetime import UTC, timedelta
from decimal import Decimal

NUMBERS = {
    "один": 1,
    "одна": 1,
    "одну": 1,
    "одного": 1,
    "два": 2,
    "две": 2,
    "пару": 2,
    "пары": 2,
    "парочку": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "полтора": 1.5,
    "полторы": 1.5,
}
WORD_NUMBER = "|".join(NUMBERS)
AMOUNT = rf"(?:[0-9]+(?:[.,][0-9]+)?|(?:{WORD_NUMBER})(?:\s+(?:{WORD_NUMBER}))?)"
UNIT_SECONDS = dict.fromkeys(
    ("секунда", "секунду", "секунды", "секунд", "секундочку", "сек", "сек.", "с"), 1
)
UNIT_SECONDS.update(
    dict.fromkeys(
        (
            "минута",
            "минуту",
            "минуты",
            "минут",
            "минутку",
            "минутки",
            "минуток",
            "мин",
            "мин.",
            "м",
        ),
        60,
    )
)
UNIT_SECONDS.update(
    dict.fromkeys(("час", "часа", "часов", "часик", "часика", "часиков", "ч"), 3600)
)
UNIT_SECONDS.update(dict.fromkeys(("день", "дня", "дней", "сутки", "суток"), 86400))
UNIT_SECONDS.update(dict.fromkeys(("неделю", "недели", "недель"), 604800))
UNIT = "|".join(re.escape(value) for value in sorted(UNIT_SECONDS, key=len, reverse=True))
TERM = re.compile(
    rf"(?:(?P<half>полчаса|полчасика|четверть\s+часа)|"
    rf"(?P<amount>{AMOUNT})\s*(?P<unit>{UNIT})|(?P<one>час|минуту|минутку|секунду|день|сутки|неделю))"
    r"(?![а-яёa-z0-9])",
    re.I,
)
SEPARATOR = re.compile(r"(?:\s*(?:,|и)\s*|\s+)")
THROUGH = re.compile(r"\bчерез\s+", re.I)
UNCLEAR_DELAY = (
    "Уточните интервал и текст. Например, «Напомни через 2 минуты проверить чай».\n\n"
    "«Пару» считаю как 2. Для «несколько» или «скоро» укажите точное время."
)


@dataclass(frozen=True)
class RelativeReminder:
    delay: timedelta
    text: str

    def moment(self, now, timezone):
        return (now.astimezone(UTC) + self.delay).astimezone(timezone)


def duration(value):
    """Return elapsed seconds and the end of the recognized duration."""
    position = end = 0
    seconds = Decimal(0)
    while match := TERM.match(value, position):
        if match["half"]:
            amount, unit = Decimal(1), "полчаса"
            factor = 900 if match["half"].lower().startswith("четверть") else 1800
        else:
            raw = match["amount"]
            amount = Decimal(1)
            if raw and raw[0].isdigit():
                amount = Decimal(raw.replace(",", "."))
            elif raw:
                words = raw.lower().split()
                if len(words) == 2 and not (NUMBERS[words[0]] >= 20 and NUMBERS[words[1]] < 10):
                    raise ValueError("unclear_reminder_delay")
                amount = sum(Decimal(str(NUMBERS[word])) for word in words)
            unit = (match["unit"] or match["one"]).lower()
            factor = UNIT_SECONDS[unit]
        seconds += amount * factor
        end = match.end()
        separator = SEPARATOR.match(value, end)
        if not separator:
            break
        position = separator.end()
    if not end or seconds <= 0:
        raise ValueError("unclear_reminder_delay")
    try:
        delay = timedelta(seconds=float(seconds))
    except OverflowError:
        raise ValueError("unclear_reminder_delay") from None
    if delay <= timedelta(0):
        raise ValueError("unclear_reminder_delay")
    return delay, end


def relative_reminder(body):
    """Accept the duration before or after the reminder's text."""
    through = THROUGH.search(body)
    if not through:
        return None
    delay, end = duration(body[through.end() :])
    before = body[: through.start()].strip(" ,:–—-")
    after = body[through.end() + end :].strip(" ,:–—-")
    if before and after.strip(".!?"):
        raise ValueError("unclear_reminder_delay")
    text = before or after
    if not text or len(text) > 500:
        raise ValueError("unclear_reminder_delay")
    return RelativeReminder(delay, text)
