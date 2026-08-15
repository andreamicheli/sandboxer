from __future__ import annotations

import datetime as dt

import pytest

from sandboxer_v0.schedule import (
    ScheduleError,
    first_publication_date,
    next_odd_days,
    parse_iso_date,
    publication_slots,
    schedule_sequence,
)


def test_next_odd_days_are_strictly_odd_and_increasing():
    start = dt.date(2026, 8, 15)  # Saturday, day 15 (odd)
    days = next_odd_days(start, 6)
    assert len(days) == 6
    assert all(day.day % 2 == 1 for day in days)
    assert all(a < b for a, b in zip(days, days[1:]))
    # From an odd start the first strictly-later odd day is the day after next.
    assert days[0] == dt.date(2026, 8, 17)


def test_next_odd_days_wraps_month_boundary():
    # Day 31 is odd; the next odd day after it is day 1 of the following month.
    days = next_odd_days(dt.date(2026, 8, 31), 2)
    assert days == [dt.date(2026, 9, 1), dt.date(2026, 9, 3)]


def test_first_publication_date_is_next_odd_day():
    assert first_publication_date(dt.date(2026, 8, 14)) == dt.date(2026, 8, 15)
    assert first_publication_date(dt.date(2026, 8, 15)) == dt.date(2026, 8, 17)


def test_publication_slots_are_one_indexed():
    slots = publication_slots(dt.date(2026, 8, 15), 3)
    assert [slot.slot for slot in slots] == [1, 2, 3]
    assert [slot.publish_date.day for slot in slots] == [17, 19, 21]


def test_schedule_sequence_returns_iso_strings():
    assert schedule_sequence(dt.date(2026, 8, 15), 2) == ["2026-08-17", "2026-08-19"]


def test_parse_iso_date_rejects_garbage():
    with pytest.raises(ScheduleError, match="SCHEDULE_DATE_INVALID"):
        parse_iso_date("15/08/2026")


def test_zero_count_rejected():
    with pytest.raises(ScheduleError, match="SCHEDULE_COUNT_INVALID"):
        next_odd_days(dt.date(2026, 8, 15), 0)
