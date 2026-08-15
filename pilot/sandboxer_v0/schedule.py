"""Deterministic publication scheduling for the broadcast layer.

The operator asked for episodes to go live on *incremental odd days*: each new
video is scheduled on an odd-numbered day of the month, strictly after the
previous one, so the operator can preview the unlisted upload and request
changes before the homepage indexes it.

This module owns only the date arithmetic.  It is pure and deterministic: the
same ``start`` and ``count`` always produce the same dates, which keeps the
schedule reproducible and unit-testable without a clock or network.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterator, Sequence


class ScheduleError(ValueError):
    """A publication date request cannot be satisfied."""


@dataclass(frozen=True)
class PublicationSlot:
    """One scheduled publication: the index entry and its reveal date."""

    slot: int
    publish_date: dt.date


def _next_odd(start: dt.date) -> dt.date:
    """The first odd-numbered day strictly after ``start``."""
    candidate = start + dt.timedelta(days=1)
    while candidate.day % 2 == 0:
        candidate += dt.timedelta(days=1)
    return candidate


def next_odd_days(start: dt.date, count: int) -> list[dt.date]:
    """Return the next ``count`` odd-numbered calendar days after ``start``.

    Each date has an odd day-of-month, and dates are strictly increasing, so a
    series scheduled through this function lands on day 1, 3, 5, … of the
    month (wrapping across month boundaries naturally).
    """
    if count < 1:
        raise ScheduleError("SCHEDULE_COUNT_INVALID")
    result: list[dt.date] = []
    candidate = _next_odd(start)
    for _ in range(count):
        result.append(candidate)
        candidate = _next_odd(candidate)
    return result


def publication_slots(start: dt.date, count: int) -> list[PublicationSlot]:
    """The next ``count`` publication slots, 1-indexed, on odd days."""
    return [
        PublicationSlot(slot=index + 1, publish_date=date)
        for index, date in enumerate(next_odd_days(start, count))
    ]


def first_publication_date(start: dt.date) -> dt.date:
    """The next odd day on which an episode may be published."""
    return _next_odd(start)


def iso_date(value: dt.date) -> str:
    """ISO-8601 calendar date (``YYYY-MM-DD``)."""
    return value.isoformat()


def parse_iso_date(value: str) -> dt.date:
    """Parse ``YYYY-MM-DD`` strictly (no partial dates)."""
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise ScheduleError("SCHEDULE_DATE_INVALID") from error
    return parsed


def schedule_sequence(start: dt.date, count: int) -> Sequence[str]:
    """ISO strings for the next ``count`` odd days (convenience for CLIs)."""
    return [iso_date(date) for date in next_odd_days(start, count)]


def iter_odd_days(start: dt.date, limit: int) -> Iterator[dt.date]:
    """Yield odd-numbered days after ``start``, up to ``limit`` of them."""
    yield from next_odd_days(start, limit)


__all__ = [
    "PublicationSlot",
    "ScheduleError",
    "first_publication_date",
    "iso_date",
    "iter_odd_days",
    "next_odd_days",
    "parse_iso_date",
    "publication_slots",
    "schedule_sequence",
]
