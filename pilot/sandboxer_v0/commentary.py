"""Narrative commentary drafting for the broadcast layer.

The editorial manifest carries a two-voice dialogue schedule: a primary
play-by-play voice explains immediate events, a less frequent analyst voice
connects them to broader strategy.  Commentary is *drafted* — by a model in
production, or authored directly for a rehearsal — then validated before it is
scheduled into the manifest.  Human review remains mandatory before
publication; this module only produces and validates the draft.

Every line is typed so observed facts stay distinct from editorial
interpretation:

- ``observed``      — a direct restatement of an event, grounded to event IDs.
- ``interpreted``   — a reading of behaviour/intent; must carry a hedge marker
  ("appears", "seems", …) so it stays recognisably interpretive.
- ``editorial``     — a labelled metaphor or colour remark, grounded but not
  asserting a fact.

This module drafts and content-validates; it deliberately holds no scheduling
authority.  Placement, budgets, clamping and fallbacks are owned by
``schedule.validate_and_pack`` and enforced on every consumption path in
``video.build_video_manifest`` — draft ``offset_seconds`` values here are
suggestions the scheduler may clamp or ignore.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol, Sequence

from .agents import HeadlessAgentAdapter, phase_adapter


class CommentaryError(ValueError):
    pass


def _repair_line_types(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Downgrade interpreted lines that forgot their hedge marker.

    ``interpreted`` is defined as requiring a hedge ("seems", "likely", …);
    some backends emit the type but plain prose.  Downgrading such a line to
    ``editorial`` (grounded, but not asserting a fact) is a safe, text-preserving
    repair that keeps validation strict instead of silently dropping the line.
    """
    for line in lines:
        if line.get("line_type") == "interpreted" and not any(
            marker in str(line.get("text", "")).lower() for marker in HEDGE_MARKERS
        ):
            line["line_type"] = "editorial"
    return lines


# Public alias: every path that consumes an LLM draft (not just the drafter
# parsers) applies this repair before scheduling, so the downgrade is
# structural rather than prompt-dependent.
repair_line_types = _repair_line_types


def fallback_intro_commentary(identities: Sequence[str]) -> list[dict[str, Any]]:
    """Deterministic greeting/model-intro rundown for when an LLM intro is unusable.

    Authored here so the schedule authority can fall back to valid scene-anchored
    content without ever trusting model output; every block it produces is marked
    ``deterministic_fallback`` downstream.
    """
    first, second = (str(item) for item in identities[:2])
    return [
        {
            "voice_role": "play_by_play",
            "line_type": "editorial",
            "scene": "cold_open",
            "offset_seconds": 2.0,
            "text": f"Welcome to Sandboxer: {first} against {second}.",
        },
        {
            "voice_role": "analyst",
            "line_type": "editorial",
            "scene": "model_cards_and_rules",
            "offset_seconds": 1.0,
            "text": f"Two isolated models, one flag. On paper {first} and {second} look close.",
        },
    ]


COMMENTARY_LINE_TYPES = frozenset({"observed", "interpreted", "editorial"})
COMMENTARY_ROLES = frozenset({"play_by_play", "analyst"})
# Intro/greeting lines anchor to named scenes instead of event IDs.
INTRO_SCENES = frozenset({"cold_open", "model_cards_and_rules"})
# Hedge markers keep interpreted lines recognisably interpretive (issue #2).
HEDGE_MARKERS = (
    "appear", "seem", "looks like", "probably", "maybe", "likely",
    "suggests", "reading", "guess", "as if", "possibly",
)


def validate_commentary(
    lines: Sequence[Mapping[str, Any]], frames: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    """Return a list of human-readable failures (empty when the draft is valid).

    Checks: grounding to real event IDs, known voice roles, known line types,
    non-empty text, and a hedge marker on interpreted lines.
    """
    event_ids = {str(frame["event_id"]) for frame in frames}
    failures: list[str] = []
    for index, line in enumerate(lines):
        eids = [str(item) for item in line.get("event_ids", ())]
        if not eids:
            failures.append(f"{index}: ungrounded (no event_ids)")
        elif any(item not in event_ids for item in eids):
            missing = sorted(set(eids) - event_ids)
            failures.append(f"{index}: unknown event_ids {missing}")
        role = line.get("voice_role")
        if role not in COMMENTARY_ROLES:
            failures.append(f"{index}: bad voice_role {role!r}")
        line_type = line.get("line_type", "observed")
        if line_type not in COMMENTARY_LINE_TYPES:
            failures.append(f"{index}: bad line_type {line_type!r}")
        text = str(line.get("text", "")).strip()
        if not text:
            failures.append(f"{index}: empty text")
        if line_type == "interpreted" and not any(marker in text.lower() for marker in HEDGE_MARKERS):
            failures.append(f"{index}: interpreted line lacks a hedge marker")
    return tuple(failures)


class CommentaryDrafter(Protocol):
    """Produces a two-voice commentary draft from frozen replay + report."""

    def draft(
        self, replay: Mapping[str, Any], report: Mapping[str, Any]
    ) -> list[dict[str, Any]]: ...


class HeadlessCommentaryDrafter:
    """Drafts commentary with a headless coding agent (default ``codex``).

    The adapter is injectable for tests; without it the phase default from
    ``phase_adapter("commentary")`` (env-overridable) is used.  The returned
    lines are validated before being handed back, so an ungrounded or untyped
    draft fails closed rather than reaching the manifest.
    """

    def __init__(self, *, adapter: HeadlessAgentAdapter | None = None) -> None:
        self._adapter = adapter or phase_adapter("commentary")

    def draft(
        self, replay: Mapping[str, Any], report: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        text = self._adapter.complete(self._prompt(replay, report))
        return self._parse(text, replay)

    def _prompt(self, replay: Mapping[str, Any], report: Mapping[str, Any]) -> str:
        identities = [str(pane["identity"]) for pane in replay.get("panes", ())]
        frames = [
            {
                "event_id": frame["event_id"],
                "phase": frame.get("phase", ""),
                "pane": frame.get("pane", 0),
                "text": frame.get("text", ""),
            }
            for frame in replay.get("frames", ())
        ]
        return json.dumps(
            {
                "task": (
                    "Draft a natural, conversational two-voice commentary for this "
                    "simulated capture-the-flag match. Two voices share one rundown: "
                    "a primary play-by-play voice explains immediate events clearly, "
                    "with occasional bounded opinion; a less frequent analyst voice "
                    "connects events to broader strategy. Do not read terminal text "
                    "verbatim. Silence and natural pauses are welcome."
                ),
                "rules": {
                    "voices": ["play_by_play", "analyst"],
                    "line_types": ["observed", "interpreted", "editorial"],
                    "grounding": "every line's event_ids must reference real event IDs below",
                    "hedging": "interpreted lines must use 'appears', 'seems', or similar",
                    "tone": "human, curious, engaging; a simulated CTF, never glorify real harm",
                    "length": "produce at most 10 lines total; keep each line under 25 words; let the analyst voice speak less often (roughly one analyst line per two play-by-play lines)",
                },
                "identities": identities,
                "outcome": report.get("outcome", {}),
                "frames": frames,
                "output": '{"lines":[{"voice_role","line_type","event_ids","text"}, ...]}',
            },
            indent=2,
        )

    def _parse(self, text: str, replay: Mapping[str, Any]) -> list[dict[str, Any]]:
        cleaned = text.strip()
        # Tolerate a ```json fence or surrounding prose the backend may wrap
        # around the object (same leniency the intro parser already has).
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.lstrip().startswith("json"):
                cleaned = cleaned.lstrip()[4:]
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start : end + 1]
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as error:
            raise CommentaryError("COMMENTARY_PARSE_FAILED") from error
        lines = payload.get("lines") if isinstance(payload, dict) else None
        if not isinstance(lines, list) or not lines:
            raise CommentaryError("COMMENTARY_EMPTY")
        lines = [dict(line) for line in lines]
        _repair_line_types(lines)
        failures = validate_commentary(lines, replay.get("frames", ()))
        if failures:
            raise CommentaryError(f"COMMENTARY_INVALID: {'; '.join(failures)}")
        return lines


def draft_commentary(
    replay: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    drafter: CommentaryDrafter | None = None,
) -> list[dict[str, Any]]:
    """Draft and validate a two-voice commentary; raises if the draft is invalid."""
    lines = (drafter or HeadlessCommentaryDrafter()).draft(replay, report)
    failures = validate_commentary(lines, replay.get("frames", ()))
    if failures:
        raise CommentaryError(f"COMMENTARY_INVALID: {'; '.join(failures)}")
    return [dict(line) for line in lines]


def validate_intro_commentary(
    lines: Sequence[Mapping[str, Any]],
    *,
    identities: Sequence[str],
) -> tuple[str, ...]:
    """Validate scene-anchored greeting/model-intro lines (no event grounding)."""
    failures: list[str] = []
    known = {str(item) for item in identities}
    for index, line in enumerate(lines):
        role = line.get("voice_role")
        if role not in COMMENTARY_ROLES:
            failures.append(f"{index}: bad voice_role {role!r}")
        line_type = line.get("line_type", "editorial")
        if line_type not in COMMENTARY_LINE_TYPES:
            failures.append(f"{index}: bad line_type {line_type!r}")
        text = str(line.get("text", "")).strip()
        if not text:
            failures.append(f"{index}: empty text")
        scene = line.get("scene")
        if scene not in INTRO_SCENES:
            failures.append(f"{index}: bad scene {scene!r}")
        offset = line.get("offset_seconds")
        if not isinstance(offset, (int, float)) or isinstance(offset, bool) or offset < 0:
            failures.append(f"{index}: bad offset_seconds {offset!r}")
        if line_type == "interpreted" and not any(marker in text.lower() for marker in HEDGE_MARKERS):
            failures.append(f"{index}: interpreted line lacks a hedge marker")
        model = line.get("model")
        if model not in (None, "") and str(model) not in known:
            failures.append(f"{index}: unknown model {model!r}")
    return tuple(failures)


class IntroCommentaryDrafter(Protocol):
    """Produces a greeting + model-intro rundown from competitor facts."""

    def draft(self, identities: Sequence[str], facts: Mapping[str, Any]) -> list[dict[str, Any]]: ...


class HeadlessIntroCommentaryDrafter:
    """Drafts the greeting/model intro with a headless coding agent.

    ``facts`` carries the researched, citable material (producer, architecture,
    benchmark highlights) so the model writes the *prose* but never invents the
    underlying facts; the draft is validated fail-closed and the caller keeps a
    deterministic fallback rundown.
    """

    def __init__(self, *, adapter: HeadlessAgentAdapter | None = None) -> None:
        self._adapter = adapter or phase_adapter("intro")

    def draft(self, identities: Sequence[str], facts: Mapping[str, Any]) -> list[dict[str, Any]]:
        text = self._adapter.complete(self._prompt(identities, facts))
        return self._parse(text, identities)

    def _prompt(self, identities: Sequence[str], facts: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "task": (
                    "Write a short, voiced opening for a broadcast of a simulated "
                    "capture-the-flag match between two AI models. Two voices share "
                    "the rundown: a play-by-play voice and a less frequent analyst. "
                    "Include a greeting toward the end of the cold open, then a short "
                    "hype-laden-but-hedged introduction of each competitor during the "
                    "model-cards scene. Use only the facts below; frame expectations "
                    "as opinion with hedges ('we expect', 'it seems'). Keep it human, "
                    "curious and fun — never glorify real harm."
                ),
                "rules": {
                    "scenes": sorted(INTRO_SCENES),
                    "voice_roles": ["play_by_play", "analyst"],
                    "line_types": ["observed", "interpreted", "editorial"],
                    "hedging": "interpreted lines must use 'seems', 'we expect', 'likely', or similar",
                    "offset_seconds": "non-negative float, RELATIVE TO THE START of its scene; cold_open spans 0-8s and model_cards_and_rules spans 0-20s; place the greeting near the end of cold_open",
                    "length": "produce at most 6 lines total (1-2 in cold_open, 4-5 in model_cards_and_rules); keep each line under 20 words",
                },
                "identities": [str(item) for item in identities],
                "facts": facts,
                "output_schema": {
                    "lines": ["voice_role", "line_type", "scene", "offset_seconds", "text", "model?"]
                },
            },
            indent=2,
        )

    def _parse(self, text: str, identities: Sequence[str]) -> list[dict[str, Any]]:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.lstrip().startswith("json"):
                cleaned = cleaned.lstrip()[4:]
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start != -1 and end > start:
            cleaned = cleaned[start : end + 1]
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as error:
            raise CommentaryError("INTRO_COMMENTARY_PARSE_FAILED") from error
        if isinstance(payload, dict):
            payload = payload.get("lines")
        if not isinstance(payload, list) or not payload:
            raise CommentaryError("INTRO_COMMENTARY_EMPTY")
        lines = [dict(line) for line in payload]
        _repair_line_types(lines)
        failures = validate_intro_commentary(lines, identities=identities)
        if failures:
            raise CommentaryError(f"INTRO_COMMENTARY_INVALID: {'; '.join(failures)}")
        return lines


def draft_intro_commentary(
    identities: Sequence[str],
    facts: Mapping[str, Any],
    *,
    drafter: IntroCommentaryDrafter | None = None,
) -> list[dict[str, Any]]:
    """Draft and validate a greeting/model intro; raises if the draft is invalid."""
    lines = (drafter or HeadlessIntroCommentaryDrafter()).draft(identities, facts)
    failures = validate_intro_commentary(lines, identities=identities)
    if failures:
        raise CommentaryError(f"INTRO_COMMENTARY_INVALID: {'; '.join(failures)}")
    return [dict(line) for line in lines]


__all__ = [
    "CommentaryDrafter",
    "CommentaryError",
    "HeadlessCommentaryDrafter",
    "HeadlessIntroCommentaryDrafter",
    "IntroCommentaryDrafter",
    "draft_commentary",
    "draft_intro_commentary",
    "fallback_intro_commentary",
    "repair_line_types",
    "validate_commentary",
    "validate_intro_commentary",
]
