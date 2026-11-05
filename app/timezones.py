"""Проверка IANA и локального времени с обоими вариантами fold."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, reset_tzpath

# Одинаковая закреплённая IANA-база на Windows, Linux и macOS; не зависим
# от версии системного tzdata на конкретной машине.
reset_tzpath(())


def zone(name: str) -> ZoneInfo:
    if not name or len(name) > 100:
        raise ValueError("unknown_timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("unknown_timezone") from None


def local_candidates(local: datetime, timezone: str) -> list[datetime]:
    tz = zone(timezone)
    moments = {}
    for fold in (0, 1):
        candidate = local.replace(tzinfo=tz, fold=fold)
        utc = candidate.astimezone(UTC)
        if utc.astimezone(tz).replace(tzinfo=None) == local:
            moments[utc] = candidate
    return list(moments.values())


def reminder_time(value: str, timezone: str, now: datetime) -> datetime:
    tz = zone(timezone)
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("timezone_required")
    local = moment.replace(tzinfo=None)
    candidates = local_candidates(local, timezone)
    if len(candidates) != 1:
        raise ValueError("ambiguous_local_time")
    if moment.utcoffset() != candidates[0].utcoffset():
        raise ValueError("timezone_mismatch")
    result = moment.astimezone(tz)
    if result <= now:
        raise ValueError("past_time")
    return result
