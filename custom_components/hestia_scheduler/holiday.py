"""Pure helpers for Hestia Scheduler holiday mode."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
import re
from typing import Any, Callable

DEFAULT_RETURN_TIME = time(18, 0)
MAX_HOLIDAY_DAYS = 60
HOLIDAY_RETURN_SAFETY_FACTOR = 1.15

_RELATIVE_RE = re.compile(r"^(\d+)\s*([hdw])$", re.IGNORECASE)
_DATE_RE = re.compile(
    r"^(?P<day>\d{1,2})/(?P<month>\d{1,2})"
    r"(?:/(?P<year>\d{4}))?"
    r"(?:\s+(?P<hour>\d{1,2}):(?P<minute>\d{2}))?$"
)


class HolidayParseError(ValueError):
    """Raised when a holiday reply cannot be interpreted safely."""


def validate_until(
    until: datetime,
    now: datetime,
    *,
    max_days: int = MAX_HOLIDAY_DAYS,
) -> datetime:
    """Validate and return a timezone-aware Holiday mode end time.

    All entry points (text parser, ISO service/MQTT requests and the card) use
    this one validation path so they expose the same limits and error text.
    """
    if until.tzinfo is None or now.tzinfo is None:
        raise HolidayParseError("The return date/time must include a timezone")
    if until <= now:
        raise HolidayParseError("The return date/time must be in the future")
    if until > now + timedelta(days=max_days):
        raise HolidayParseError(f"The return date/time must be within {max_days} days")
    return until


def parse_holiday_reply(
    text: str,
    now: datetime,
    *,
    default_return_time: time = DEFAULT_RETURN_TIME,
    max_days: int = MAX_HOLIDAY_DAYS,
) -> datetime | None:
    """Parse a phone reply into an aware local datetime.

    Accepted forms are ``3d``, ``2w``, ``36h``, ``28/11``,
    ``28/11 18:00`` and ``28/11/2026 18:00``.  Day/week replies and
    date-only replies land at the configured default return time; hour replies
    are exact elapsed durations.  ``cancel`` and ``0`` intentionally perform
    no action and return ``None``.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    value = (text or "").strip().lower()
    if value in {"cancel", "0"}:
        return None
    if not value:
        raise HolidayParseError("Enter a return date/time, for example 28/11 18:00 or 3d")

    relative = _RELATIVE_RE.fullmatch(value)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        if amount <= 0:
            raise HolidayParseError("The holiday duration must be greater than zero")
        if unit == "h":
            until = now + timedelta(hours=amount)
        else:
            days = amount if unit == "d" else amount * 7
            target_date = now.date() + timedelta(days=days)
            until = datetime.combine(target_date, default_return_time, tzinfo=now.tzinfo)
    else:
        match = _DATE_RE.fullmatch(value)
        if not match:
            raise HolidayParseError(
                "Use dd/mm, dd/mm hh:mm, dd/mm/yyyy hh:mm, or a duration such as 3d, 2w or 36h"
            )
        day = int(match.group("day"))
        month = int(match.group("month"))
        explicit_year = match.group("year")
        hour = int(match.group("hour")) if match.group("hour") is not None else default_return_time.hour
        minute = int(match.group("minute")) if match.group("minute") is not None else default_return_time.minute
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise HolidayParseError("The return time is invalid")

        year = int(explicit_year) if explicit_year else now.year
        try:
            until = datetime(year, month, day, hour, minute, tzinfo=now.tzinfo)
        except ValueError as err:
            raise HolidayParseError("The return date is invalid") from err
        if not explicit_year and until <= now:
            try:
                until = until.replace(year=year + 1)
            except ValueError as err:
                raise HolidayParseError("The return date is invalid") from err

    return validate_until(until, now, max_days=max_days)


def holiday_next_transition(
    zone: Any,
    until: datetime,
    find_active: Callable[[Any, datetime], tuple[Any | None, int]],
) -> tuple[Any | None, datetime | None]:
    """Return the normal slot active at ``until``, firing exactly at ``until``.

    The returned copy is never preemptable: ending holiday mode must not send a
    normal schedule notification or create a rollback record for the slot's
    weekly key.
    """
    slot, _ = find_active(zone, until)
    if slot is None:
        return None, None
    return replace(slot, preemptable=False), until
