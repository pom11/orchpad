from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchpad import dates

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone(timedelta(hours=1)))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-12-01", "2026-12-01"),
        ("today", "2026-11-25"),
        ("tomorrow", "2026-11-26"),
        ("mon", "2026-11-30"),
        ("fri", "2026-11-27"),
        ("+3d", "2026-11-28"),
        ("+2w", "2026-12-09"),
        ("2026-12-01T17:00:00+01:00", "2026-12-01T17:00:00+01:00"),
    ],
)
def test_parse_due_relative_to_fixed_clock(value, expected):
    assert dates.parse_due(value, now_=NOW) == expected


def test_parse_weekday_time_is_timezone_safe():
    assert dates.parse_due("fri 17:00", now_=NOW) == (
        "2026-11-27T17:00:00+01:00"
    )
    # On Friday after 17:00 the next occurrence is a week later.
    late = datetime(2026, 11, 27, 18, 0, tzinfo=timezone(timedelta(hours=1)))
    assert dates.parse_due("fri 17:00", now_=late) == (
        "2026-12-04T17:00:00+01:00"
    )


def test_parse_offsets_for_due_and_reminders():
    assert dates.parse_due("+1h", now_=NOW) == "2026-11-25T13:00:00+01:00"
    assert dates.parse_remind_at("+1h", now_=NOW) == (
        "2026-11-25T13:00:00+01:00"
    )
    assert dates.parse_remind_at("tomorrow", now_=NOW) == (
        "2026-11-26T09:00:00+01:00"
    )


def test_empty_values_clear_fields():
    for value in ("", "   ", "none", "clear"):
        assert dates.parse_due(value, now_=NOW) is None
        assert dates.parse_remind_at(value, now_=NOW) is None


def test_invalid_values_raise():
    with pytest.raises(ValueError):
        dates.parse_due("someday", now_=NOW)
    with pytest.raises(ValueError):
        dates.parse_remind_at("25:99", now_=NOW)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("day", "daily"),
        ("weekly", "weekly"),
        ("week", "weekly"),
        ("weekdays", "weekdays"),
        ("month", "monthly"),
        ("yearly", "yearly"),
        ("none", None),
        ("every 2 days", "every 2 days"),
        ("every 1 week", "every 1 week"),
        ("2d", "every 2 days"),
        ("3w", "every 3 weeks"),
    ],
)
def test_repeat_normalisation(value, expected):
    assert dates.parse_repeat_token(value) == expected


def test_unknown_repeat_raises():
    with pytest.raises(ValueError):
        dates.normalize_repeat("fortnightly")
    with pytest.raises(ValueError):
        dates.parse_repeat_token("every 0 days")


def test_advance_rolls_forward_past_now():
    assert dates.advance("2026-11-20", "weekly", now_=NOW) == "2026-11-27"
    assert dates.advance("2026-11-20", "daily", now_=NOW) == "2026-11-26"
    assert dates.advance("2026-11-25", "weekdays", now_=NOW) == "2026-11-26"
    assert dates.advance("2026-11-24", "monthly", now_=NOW) == "2026-12-24"
    assert dates.advance("2026-11-24", "yearly", now_=NOW) == "2027-11-24"
    assert dates.advance("2026-11-20T08:00:00+01:00", "daily", now_=NOW) == (
        "2026-11-26T08:00:00+01:00"
    )


def test_weekday_advance_skips_the_weekend():
    friday = datetime(2026, 11, 27, 12, 0, tzinfo=timezone(timedelta(hours=1)))
    assert dates.advance("2026-11-27", "weekdays", now_=friday) == "2026-11-30"


def test_due_state_and_sort_key():
    assert dates.due_state("2026-11-24", now_=NOW) == "overdue"
    assert dates.due_state("2026-11-25", now_=NOW) == "today"
    assert dates.due_state("2026-11-27", now_=NOW) == "upcoming"
    assert dates.due_state("2026-12-30", now_=NOW) is None
    assert dates.due_sort_key(None)[0] == 1
    early = dates.due_sort_key("2026-11-26", now_=NOW)
    late = dates.due_sort_key("2026-11-27", now_=NOW)
    assert early < late


def test_format_when_drops_midnight_time():
    assert dates.format_when("2026-12-01") == "2026-12-01"
    assert dates.format_when("2026-12-01T00:00:00+01:00") == "2026-12-01"
    assert dates.format_when("2026-12-01T17:30:00+01:00") == (
        "2026-12-01 17:30"
    )
