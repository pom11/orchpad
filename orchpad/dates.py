"""Date, reminder and repeat helpers for reminders-style item fields.

Due dates may be plain ISO dates (``2026-12-01``) or ISO datetimes with an
offset.  Reminders are always datetimes.  Relative input is resolved against an
injectable clock so tests never depend on the wall clock or the host timezone.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone

WEEKDAY_NAMES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

_RELATIVE_DAYS = {"today": 0, "tomorrow": 1, "yesterday": -1}
_OFFSET = re.compile(r"^\+([0-9]+)([hdw])$")
_TIME = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$")
_WEEKDAY = re.compile(
    r"^([a-z]+)(?:\s+([0-9]{1,2}:[0-9]{2}))?$", re.IGNORECASE
)
_EVERY = re.compile(r"^every\s+([0-9]+)\s+(days?|weeks?)$")

REPEAT_ALIASES = {
    "none": None,
    "daily": "daily",
    "day": "daily",
    "weekdays": "weekdays",
    "weekday": "weekdays",
    "weekly": "weekly",
    "week": "weekly",
    "monthly": "monthly",
    "month": "monthly",
    "yearly": "yearly",
    "year": "yearly",
}


def now() -> datetime:
    """Return the current local time with a concrete UTC offset."""

    return datetime.now().astimezone()


def coerce_now(moment: datetime | None = None) -> datetime:
    """Return an aware ``moment``, treating naive values as local time."""

    if moment is None:
        return now()
    if moment.tzinfo is None:
        return moment.astimezone()
    return moment


def _parse_date(text: str) -> date | None:
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_datetime(text: str, moment: datetime) -> datetime | None:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=moment.tzinfo or timezone.utc)
    return value


def _parse_time(text: str) -> tuple[int, int] | None:
    match = _TIME.match(text.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def normalize_repeat(value: object) -> str | None:
    """Return the canonical repeat spelling, or ``None`` for no repeat."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("repeat must be a string")
    text = value.strip().casefold()
    if not text:
        return None
    if text in REPEAT_ALIASES:
        return REPEAT_ALIASES[text]
    match = _EVERY.match(text)
    if match:
        count = int(match.group(1))
        if count < 1:
            raise ValueError("repeat interval must be at least one day")
        unit = "day" if match.group(2).startswith("day") else "week"
        return f"every {count} {unit}{'' if count == 1 else 's'}"
    raise ValueError(
        "repeat must be none, daily, weekdays, weekly, monthly, yearly, "
        "or 'every N days'/'every N weeks'"
    )


def parse_when(
    value: object,
    *,
    now_: datetime | None = None,
    time_of_day: tuple[int, int] = (9, 0),
) -> tuple[str, bool]:
    """Parse user-supplied due/reminder input.

    Returns ``(stored_value, has_time)``.  Dates stay date-only strings and
    datetimes carry an explicit offset.  ``""`` and ``none`` mean "clear".
    """

    if value is None:
        raise ValueError("date value is required")
    if not isinstance(value, str):
        raise ValueError("date value must be a string")
    text = value.strip()
    if not text or text.casefold() in ("none", "clear"):
        raise ValueError("date value must not be empty")
    moment = coerce_now(now_)
    today = moment.date()
    lowered = text.casefold()

    if lowered in _RELATIVE_DAYS:
        return (
            (today + timedelta(days=_RELATIVE_DAYS[lowered])).isoformat(),
            False,
        )

    time_part: tuple[int, int] | None = None
    date_part = lowered
    if " " in lowered:
        head, _, tail = lowered.partition(" ")
        if head in _RELATIVE_DAYS and _parse_time(tail) is not None:
            date_part = head
            time_part = _parse_time(tail)
    if date_part in _RELATIVE_DAYS:
        target = today + timedelta(days=_RELATIVE_DAYS[date_part])
        hour, minute = time_part or time_of_day
        return (
            _datetime_string(
                datetime.combine(
                    target,
                    datetime.min.time().replace(hour=hour, minute=minute),
                    tzinfo=moment.tzinfo,
                )
            ),
            True,
        )

    match = _WEEKDAY.match(lowered)
    if match and match.group(1) in WEEKDAY_NAMES:
        weekday = WEEKDAY_NAMES[match.group(1)]
        ahead = (weekday - today.weekday()) % 7
        target = today + timedelta(days=ahead)
        parsed_time = _parse_time(match.group(2)) if match.group(2) else None
        if parsed_time is None:
            return target.isoformat(), False
        hour, minute = parsed_time
        if ahead == 0:
            candidate = datetime.combine(
                target,
                datetime.min.time().replace(hour=hour, minute=minute),
                tzinfo=moment.tzinfo,
            )
            if candidate <= moment:
                target += timedelta(days=7)
        return (
            _datetime_string(
                datetime.combine(
                    target,
                    datetime.min.time().replace(hour=hour, minute=minute),
                    tzinfo=moment.tzinfo,
                )
            ),
            True,
        )

    offset = _OFFSET.match(lowered)
    if offset:
        amount = int(offset.group(1))
        unit = offset.group(2)
        if unit == "h":
            return _datetime_string(moment + timedelta(hours=amount)), True
        days = amount * (7 if unit == "w" else 1)
        return (today + timedelta(days=days)).isoformat(), False

    plain_date = _parse_date(text)
    if plain_date is not None:
        return plain_date.isoformat(), False

    parsed = _parse_datetime(text, moment)
    if parsed is not None:
        return _datetime_string(parsed), True

    raise ValueError(
        "date must be ISO (2026-12-01 or 2026-12-01T17:00:00+01:00), "
        "today, tomorrow, a weekday, +3d, +2w or +1h"
    )


def parse_due(value: object, *, now_: datetime | None = None) -> str | None:
    """Parse a due value; empty input clears the field."""

    if value is None:
        return None
    if isinstance(value, str) and (
        not value.strip() or value.strip().casefold() in ("none", "clear")
    ):
        return None
    parsed, _ = parse_when(value, now_=now_)
    return parsed


def parse_remind_at(value: object, *, now_: datetime | None = None) -> str | None:
    """Parse a reminder value, always returning an offset datetime."""

    if value is None:
        return None
    if isinstance(value, str) and (
        not value.strip() or value.strip().casefold() in ("none", "clear")
    ):
        return None
    moment = coerce_now(now_)
    parsed, has_time = parse_when(value, now_=moment)
    if has_time:
        return parsed
    target = date.fromisoformat(parsed)
    hour, minute = 9, 0
    return _datetime_string(
        datetime.combine(
            target,
            datetime.min.time().replace(hour=hour, minute=minute),
            tzinfo=moment.tzinfo,
        )
    )


def parse_repeat_token(value: object) -> str | None:
    """Parse CLI shorthand such as ``week`` or ``2d`` into a repeat."""

    if isinstance(value, str):
        text = value.strip().casefold()
        short = re.match(r"^([0-9]+)\s*([dw])$", text)
        if short:
            count = int(short.group(1))
            unit = "days" if short.group(2) == "d" else "weeks"
            return normalize_repeat(f"every {count} {unit}")
    return normalize_repeat(value)


def _datetime_string(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _add_months(value: date, count: int) -> date:
    month = value.month - 1 + count
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _step(value: str, repeat: str, *, forward: bool = True) -> str:
    direction = 1 if forward else -1
    if repeat == "weekdays":
        if len(value) == 10:
            current = date.fromisoformat(value)
            current += timedelta(days=direction)
            while current.weekday() >= 5:
                current += timedelta(days=direction)
            return current.isoformat()
        current_dt = datetime.fromisoformat(value)
        current_dt += timedelta(days=direction)
        while current_dt.weekday() >= 5:
            current_dt += timedelta(days=direction)
        return _datetime_string(current_dt)

    every = _EVERY.match(repeat)
    if every:
        count = int(every.group(1)) * direction
        days = count if every.group(2).startswith("day") else count * 7
        delta = timedelta(days=days)
    elif repeat in ("daily", "weekly"):
        days = 1 if repeat == "daily" else 7
        delta = timedelta(days=days * direction)
    elif repeat in ("monthly", "yearly"):
        months = (1 if repeat == "monthly" else 12) * direction
        if len(value) == 10:
            return _add_months(date.fromisoformat(value), months).isoformat()
        current_dt = datetime.fromisoformat(value)
        moved = _add_months(current_dt.date(), months)
        return _datetime_string(
            datetime.combine(moved, current_dt.timetz())
        )
    else:  # pragma: no cover - normalize_repeat rejects unknown values
        raise ValueError(f"unknown repeat {repeat!r}")

    if len(value) == 10:
        return (date.fromisoformat(value) + delta).isoformat()
    return _datetime_string(datetime.fromisoformat(value) + delta)


def advance(value: str | None, repeat: str | None, *, now_: datetime | None = None) -> str | None:
    """Advance a due/reminder value by one or more repeat periods.

    The result is always strictly after the current clock, so a daily task
    checked a week late lands on the next real occurrence rather than in the
    past.
    """

    if value is None or repeat is None:
        return None
    moment = coerce_now(now_)
    advanced = _step(value, repeat)
    for _ in range(10000):
        parsed = _as_datetime(advanced, moment)
        if parsed > moment:
            break
        advanced = _step(advanced, repeat)
    return advanced


def _as_datetime(value: str, moment: datetime) -> datetime:
    if len(value) == 10:
        return datetime.combine(
            date.fromisoformat(value),
            datetime.min.time(),
            tzinfo=moment.tzinfo,
        )
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=moment.tzinfo)
    return parsed


def due_state(value: str | None, *, now_: datetime | None = None) -> str | None:
    """Classify a due value as ``overdue``, ``today`` or ``upcoming``."""

    if not value:
        return None
    moment = coerce_now(now_)
    today = moment.date()
    due_date = _as_datetime(value, moment)
    if len(value) == 10:
        if due_date.date() < today:
            return "overdue"
        if due_date.date() == today:
            return "today"
        if due_date.date() <= today + timedelta(days=7):
            return "upcoming"
        return None
    if due_date < moment:
        return "overdue"
    if due_date.date() == today:
        return "today"
    if due_date <= moment + timedelta(days=7):
        return "upcoming"
    return None


def due_date(value: str | None, *, now_: datetime | None = None) -> date | None:
    """Return the calendar date of a due value, if it has one."""

    if not value:
        return None
    return _as_datetime(value, coerce_now(now_)).date()


def is_overdue(value: str | None, *, now_: datetime | None = None) -> bool:
    return due_state(value, now_=now_) == "overdue"


def due_sort_key(value: str | None, *, now_: datetime | None = None) -> tuple[int, float]:
    """Sort key that puts items without a due date last."""

    if not value:
        return (1, 0.0)
    moment = coerce_now(now_)
    parsed = _as_datetime(value, moment)
    return (0, parsed.timestamp())


def format_when(value: str | None) -> str:
    """Short human form: a plain date, or a date plus time when meaningful."""

    if not value:
        return ""
    if len(value) == 10:
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.hour == 0 and parsed.minute == 0:
        return parsed.date().isoformat()
    return parsed.strftime("%Y-%m-%d %H:%M")
