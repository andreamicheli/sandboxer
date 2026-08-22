"""Deterministic scheduling authority for the broadcast layer.

Two responsibilities live here, both pure and deterministic:

1. Publication-date arithmetic: episodes go live on *incremental odd days*,
   strictly after the previous one, so the operator can preview the unlisted
   upload before the homepage indexes it.  The same ``start`` and ``count``
   always produce the same dates, reproducible without a clock or network.

2. Editorial-timeline packing: :func:`validate_and_pack` is the single
   authority that turns an LLM commentary draft into a schedule that fits its
   per-scene budget.  Model-provided offsets are never trusted as
   authoritative — they are clamped into the scene window, lines are sorted by
   time, repacked back-to-back without overlaps, and any non-conforming line
   (empty text, over-budget words, unknown scene, unusable offset, or anything
   that no longer fits before ``window_end_s``) is dropped and counted.  When
   nothing survives, a caller-supplied deterministic fallback draft is packed
   instead and every block is marked with ``PROVENANCE_FALLBACK`` so downstream
   review can tell model prose from machinery.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

PROVENANCE_MODEL_DRAFT = "model_draft"
"""Block content passed through the packer from the model draft unmodified."""

PROVENANCE_FALLBACK = "deterministic_fallback"
"""Block was repaired by the packer or came from the deterministic fallback."""

# Speaking-rate planning constants shared by every consumer of the packer.
WORDS_PER_SECOND = 2.6    # measured speaking rate for the pinned voices
LEAD_TAIL_SECONDS = 0.3   # per-block lead-in/tail margin
MIN_LINE_SECONDS = 1.5    # a spoken block is never planned shorter than this
MAX_LINE_SECONDS = 10.0   # … nor longer than this, whatever the word count

_EPSILON = 1e-9


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


@dataclass(frozen=True)
class LineBudget:
    """Editorial budget for one scene's commentary window.

    ``window_start_s``/``window_end_s`` are seconds relative to the *start of
    the scene*; no packed block may end after ``window_end_s``.
    """

    max_lines: int
    max_words_per_line: int
    max_total_words: int
    window_start_s: float
    window_end_s: float

    def __post_init__(self) -> None:
        if self.max_lines < 1 or self.max_words_per_line < 1 or self.max_total_words < 1:
            raise ScheduleError("SCHEDULE_BUDGET_INVALID")
        if not 0.0 <= self.window_start_s < self.window_end_s:
            raise ScheduleError("SCHEDULE_BUDGET_INVALID")


@dataclass(frozen=True)
class PackedSchedule:
    """The outcome of packing one draft against a set of scene budgets."""

    blocks: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0
    repaired: int = 0
    used_fallback: bool = False
    drop_reasons: tuple[str, ...] = ()


def line_duration_seconds(text: str, *, words_per_second: float = WORDS_PER_SECOND,
                          lead_tail_s: float = LEAD_TAIL_SECONDS,
                          min_line_s: float = MIN_LINE_SECONDS,
                          max_line_s: float = MAX_LINE_SECONDS) -> float:
    """Planning duration for a spoken line at a conservative speaking rate."""
    words = max(1, len(text.split()))
    return min(max_line_s, max(min_line_s, words / words_per_second + lead_tail_s))


# Draft fields copied onto packed blocks; everything else is scheduling noise.
_PASSTHROUGH_KEYS = ("voice_role", "line_type", "model", "event_ids")


def validate_and_pack(
    draft: Sequence[Mapping[str, Any]],
    budget: Mapping[str, LineBudget] | LineBudget,
    *,
    fallback: Sequence[Mapping[str, Any]] | None = None,
    gap_s: float = 0.0,
) -> PackedSchedule:
    """Pack a draft into per-scene windows under hard editorial budgets.

    The draft lines carry ``scene`` and a scene-relative ``offset_seconds``
    suggestion.  Neither is authoritative: offsets are clamped into the scene
    window, lines are sorted by time, then repacked strictly back-to-back so
    nothing overlaps, and any line that cannot conform is dropped:

    - empty text, more than ``max_words_per_line`` words, unknown ``scene``,
      or a non-numeric ``offset_seconds``;
    - lines beyond ``max_lines`` or past the cumulative ``max_total_words``;
    - lines whose repacked block would end after ``window_end_s``.

    Returns a :class:`PackedSchedule`; each block keeps its content fields plus
    the packed ``offset_seconds``/``end_seconds`` and a ``provenance`` marker —
    ``PROVENANCE_FALLBACK`` whenever the packer had to repair it, and on every
    block when the caller-supplied deterministic ``fallback`` draft had to be
    packed instead (all-nonconforming drafts).  An empty draft stays empty:
    silence is a legitimate schedule.
    """
    budgets: Mapping[str, LineBudget]
    if isinstance(budget, LineBudget):
        # Single-scene mode: every line packs against this one budget.
        budgets = {"": budget}
        draft = [dict(line, scene="") for line in draft]
    else:
        budgets = dict(budget)
    result = _pack_lines(draft, budgets, gap_s=gap_s)
    if result.blocks or not draft or fallback is None:
        return result
    # Every draft line was rejected: pack the deterministic fallback instead
    # and mark every block as machinery, never model prose.
    packed_fallback = _pack_lines(fallback, budgets, gap_s=gap_s)
    for block in packed_fallback.blocks:
        block["provenance"] = PROVENANCE_FALLBACK
    return PackedSchedule(
        blocks=packed_fallback.blocks,
        dropped=result.dropped + packed_fallback.dropped,
        repaired=result.repaired + packed_fallback.repaired,
        used_fallback=True,
        drop_reasons=result.drop_reasons + packed_fallback.drop_reasons,
    )


def _pack_lines(
    draft: Sequence[Mapping[str, Any]],
    budgets: Mapping[str, LineBudget],
    *,
    gap_s: float,
) -> PackedSchedule:
    dropped = 0
    repaired_count = 0
    reasons: list[str] = []
    blocks: list[dict[str, Any]] = []
    scene_indices: dict[str, list[int]] = {}
    for index, line in enumerate(draft):
        scene_indices.setdefault(str(line.get("scene", "")), []).append(index)
    for scene, indices in scene_indices.items():
        scene_budget = budgets.get(scene)
        if scene_budget is None:
            dropped += len(indices)
            reasons.extend(f"{index}:unknown_scene" for index in indices)
            continue
        entries: list[tuple[float, int, Mapping[str, Any], str, bool]] = []
        for index in indices:
            line = draft[index]
            text = str(line.get("text", "")).strip()
            offset_raw = line.get("offset_seconds")
            if not text:
                dropped += 1
                reasons.append(f"{index}:empty_text")
                continue
            if len(text.split()) > scene_budget.max_words_per_line:
                dropped += 1
                reasons.append(f"{index}:line_over_word_budget")
                continue
            if not isinstance(offset_raw, (int, float)) or isinstance(offset_raw, bool):
                dropped += 1
                reasons.append(f"{index}:bad_offset")
                continue
            # The model's offset is a suggestion only: clamp it into the window.
            offset = min(max(float(offset_raw), scene_budget.window_start_s),
                         scene_budget.window_end_s)
            clamped = abs(offset - float(offset_raw)) > _EPSILON
            if clamped:
                repaired_count += 1
                reasons.append(f"{index}:offset_clamped")
            entries.append((offset, index, line, text, clamped))
        # Earliest first; ties keep draft order.
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        if len(entries) > scene_budget.max_lines:
            for *_, index, _line, _text in entries[scene_budget.max_lines:]:
                reasons.append(f"{index}:over_line_budget")
            dropped += len(entries) - scene_budget.max_lines
            entries = entries[: scene_budget.max_lines]
        total_words = 0
        at: float | None = None
        for position, (offset, index, line, text, clamped) in enumerate(entries):
            words = len(text.split())
            if total_words + words > scene_budget.max_total_words:
                dropped += 1
                reasons.append(f"{index}:over_total_word_budget")
                continue
            start = offset if at is None else at + gap_s
            end = start + line_duration_seconds(text)
            if end > scene_budget.window_end_s + _EPSILON:
                # Packed forward-flow means every later line overflows too.
                overflowing = entries[position:]
                dropped += len(overflowing)
                reasons.extend(f"{entry[1]}:window_overflow" for entry in overflowing)
                break
            total_words += words
            at = end
            block: dict[str, Any] = {key: line[key] for key in _PASSTHROUGH_KEYS if key in line}
            block["scene"] = scene
            block["text"] = text
            block["offset_seconds"] = start
            block["end_seconds"] = end
            block["provenance"] = PROVENANCE_FALLBACK if clamped else PROVENANCE_MODEL_DRAFT
            blocks.append(block)
    return PackedSchedule(blocks=blocks, dropped=dropped, repaired=repaired_count,
                          used_fallback=False, drop_reasons=tuple(reasons))


__all__ = [
    "LEAD_TAIL_SECONDS",
    "LineBudget",
    "MAX_LINE_SECONDS",
    "MIN_LINE_SECONDS",
    "PackedSchedule",
    "PROVENANCE_FALLBACK",
    "PROVENANCE_MODEL_DRAFT",
    "PublicationSlot",
    "ScheduleError",
    "WORDS_PER_SECOND",
    "first_publication_date",
    "iso_date",
    "iter_odd_days",
    "line_duration_seconds",
    "next_odd_days",
    "parse_iso_date",
    "publication_slots",
    "schedule_sequence",
    "validate_and_pack",
]
