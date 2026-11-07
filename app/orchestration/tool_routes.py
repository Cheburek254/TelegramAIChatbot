"""Recognize direct requests while leaving translation and review text intact."""

import re
from datetime import date, timedelta

from app.planner import DAY, WEEKDAY, weekday_number

QUESTION = r"(?:(?:какое|какие|покажи(?: мне)?|есть ли) )?"
OWNER = r"(?:(?:у меня|мо[еи]) )?"
RELATIVE = {"сегодня": 0, "седня": 0, "севодня": 0, "завтра": 1, "послезавтра": 2}
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
CALENDAR_DAY = rf"[0-9]{{1,2}} (?:{'|'.join(MONTHS)})(?: [0-9]{{4}})?"
WEEK = r"(?:на )?(?:эту |текущую )?неделю|на (?:этой|текущей) неделе"
NEXT_WEEK = r"на следующ(?:ую неделю|ей неделе)"
WHEN = rf"(?:{DAY}|седня|севодня|{CALENDAR_DAY}|{WEEK}|{NEXT_WEEK})"
LESSONS = r"(?:пары|занятия|лекции|расписание(?: занятий)?)"
SCHEDULE = re.compile(
    rf"(?:{QUESTION}{OWNER}(?:(?P<before>{WHEN}) )?{LESSONS}|"
    rf"(?:что|шо) (?:у меня )?по (?:парам|занятиям|расписанию))"
    rf"(?: (?:(?:на|в|во) )?(?P<after>{WHEN}))?(?: у меня)?"
)
TASK_QUALIFIER = r"ближайшие|предстоящие|просроченные|открытые|активные|все"
TASKS = re.compile(
    rf"(?:{QUESTION}{OWNER}(?:(?P<qualifier>{TASK_QUALIFIER}) )?"
    r"(?P<noun>задачи|дедлайны|сроки сдачи)|"
    r"(?:что|шо) (?:у меня )?по (?P<about>задачам|дедлайнам|срокам))"
    r"(?: (?:на )?(?P<when>сегодня|седня|завтра|эту неделю|неделю|на этой неделе))?"
    r"(?: у меня)?"
)
MATERIAL_CATALOG = re.compile(
    r"(?:(?:покажи(?: мне)?|перечисли) )?(?:список |каталог )?(?:мои |мое )?(?:учебные )?материалы"
    r"|(?:(?:покажи(?: мне)?|перечисли) )?(?:список|каталог) (?:моих |учебных )?материалов"
)
MATERIAL_SEARCH = re.compile(
    r"^(?:найди|поищи|проверь|покажи|объясни|расскажи|что (?:написано|сказано))"
    r"(?: мне)? (?:по|в) (?:моих |учебных )?(?:конспект[а-я]*|материал[а-я]*)\b"
)
TEXT_OPERATION = re.compile(
    r"^(?:переведи|перевод|перевести|отревью|ревью|проверь код|исправь код|что такое)\b"
)


def normalized(text):
    return " ".join(text.lower().replace("ё", "е").split()).rstrip("?!., ")


def schedule_query(text):
    match = SCHEDULE.fullmatch(normalized(text))
    if match and not (match["before"] and match["after"]):
        return match["before"] or match["after"] or "сегодня"
    return None


def schedule_scope(query):
    return "week" if re.fullmatch(WEEK, query) else "day"


def schedule_day(query, today):
    if query in RELATIVE:
        return today + timedelta(days=RELATIVE[query])
    if re.fullmatch(WEEKDAY, query):
        return today + timedelta(days=(weekday_number(query) - today.weekday()) % 7)
    if " " in query:
        day, month, *year = query.split()
        return date(int(year[0]) if year else today.year, MONTHS.index(month) + 1, int(day))
    if "." in query:
        day, month, *year = map(int, query.split("."))
        return date(year[0] if year else today.year, month, day)
    return date.fromisoformat(query)


def tasks_scope(text):
    match = TASKS.fullmatch(normalized(text))
    if not match:
        return None
    if match["when"]:
        return {
            "сегодня": "today",
            "седня": "today",
            "завтра": "tomorrow",
        }.get(match["when"], "week")
    if match["qualifier"]:
        return {
            "ближайшие": "upcoming",
            "предстоящие": "upcoming",
            "просроченные": "overdue",
        }.get(match["qualifier"], "all")
    return "all" if match["noun"] == "задачи" or match["about"] == "задачам" else "upcoming"
