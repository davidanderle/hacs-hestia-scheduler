"""Unit tests for Holiday mode's pure parsing and transition helpers."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest

_HOLIDAY_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "hestia_scheduler"
    / "holiday.py"
)
_SPEC = importlib.util.spec_from_file_location("hestia_holiday", _HOLIDAY_PATH)
assert _SPEC and _SPEC.loader
_HOLIDAY = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HOLIDAY)
HolidayParseError = _HOLIDAY.HolidayParseError
holiday_next_transition = _HOLIDAY.holiday_next_transition
parse_holiday_reply = _HOLIDAY.parse_holiday_reply
validate_until = _HOLIDAY.validate_until


NOW = datetime(2026, 11, 21, 20, 56, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("36h", datetime(2026, 11, 23, 8, 56, tzinfo=timezone.utc)),
        ("3d", datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)),
        ("2w", datetime(2026, 12, 5, 18, 0, tzinfo=timezone.utc)),
        ("28/11", datetime(2026, 11, 28, 18, 0, tzinfo=timezone.utc)),
        ("28/11 19:30", datetime(2026, 11, 28, 19, 30, tzinfo=timezone.utc)),
        ("28/11/2026 18:00", datetime(2026, 11, 28, 18, 0, tzinfo=timezone.utc)),
    ],
)
def test_parse_holiday_reply(reply, expected):
    assert parse_holiday_reply(reply, NOW) == expected


def test_date_without_year_rolls_to_next_future_year():
    now = datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc)
    assert parse_holiday_reply("01/01", now) == datetime(
        2027, 1, 1, 18, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize("reply", ["yesterday", "31/02", "21/11/2026 19:00", "61d"])
def test_invalid_or_unsafe_reply_is_rejected(reply):
    with pytest.raises(HolidayParseError):
        parse_holiday_reply(reply, NOW)


def test_cancel_reply_is_a_noop():
    assert parse_holiday_reply("cancel", NOW) is None


def test_malformed_reply_has_actionable_error_message():
    with pytest.raises(HolidayParseError, match="Use dd/mm"):
        parse_holiday_reply("28 November sometime", NOW)


def test_validate_until_is_shared_by_iso_entry_points():
    until = NOW + timedelta(days=2)
    assert validate_until(until, NOW) is until


@pytest.mark.parametrize(
    ("until", "message"),
    [
        (NOW, "must be in the future"),
        (NOW + timedelta(days=61), "must be within 60 days"),
        (datetime(2026, 11, 28, 18, 0), "must include a timezone"),
    ],
)
def test_validate_until_rejects_unsafe_values(until, message):
    with pytest.raises(HolidayParseError, match=message):
        validate_until(until, NOW)


@dataclass
class FakeSlot:
    time: str
    preset: str | None = None
    temperature: float | None = None
    preemptable: bool = False


class FakeZone:
    pass


def test_holiday_next_transition_uses_slot_active_at_return_and_disables_preemption():
    normal_slot = FakeSlot("17:30", preset="home", preemptable=True)
    observed = {}

    def find_active(zone, when):
        observed["zone"] = zone
        observed["when"] = when
        return normal_slot, 2

    zone = FakeZone()
    until = datetime(2026, 11, 28, 18, 0, tzinfo=timezone.utc)
    returned, transition_at = holiday_next_transition(zone, until, find_active)

    assert observed == {"zone": zone, "when": until}
    assert transition_at == until
    assert returned == FakeSlot("17:30", preset="home", preemptable=False)
    assert returned is not normal_slot
    assert normal_slot.preemptable is True


def test_holiday_next_transition_handles_empty_schedule():
    assert holiday_next_transition(FakeZone(), NOW, lambda _z, _t: (None, -1)) == (None, None)
