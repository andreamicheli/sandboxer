"""Emotional emphasis for TTS commentary via Fish Audio inline cues.

Fish Audio S2/S2.1 models (the pipeline default is ``s2.1-pro-free``) read
free-form inline cues written in square brackets directly in the ``text``
field of ``POST /v1/tts``; sentence-level cues work best at the start of a
line (https://docs.fish.audio/developer-guide/core-features/emotions —
"[excited] This is amazing news!").  The legacy S1 family used fixed
``(parenthesis)`` tags instead.

The manifest commentary text stays CLEAN (it captions the video), so this
module is applied only in the TTS render path, immediately before synthesis;
annotation never mutates the scheduled lines.
"""

from __future__ import annotations

from typing import Any, Mapping

from .commentary import _action_kind

# Recorded in the broadcast ``tts`` section as ``emotion_version``; bump when
# cue wording or placement changes.
EMOTION_ANNOTATION_VERSION = "fish-emotion-v1"

# The single knob for the annotation format: S2/S2.1 speak "[cue]" cues.  A
# swap to another syntax (e.g. legacy S1 "(%s)") is one edit here.
FISH_CUE_TEMPLATE = "[{cue}]"

# Event kinds (see ``sandboxer_v0.commentary._action_kind``) that carry the
# match's emotional beats: flag attempts and verified captures are the hype
# moments; the defensive kinds are the steady, analytic material.
CAPTURE_KINDS = frozenset({"capture_attempt", "submission"})
DEFENSIVE_KINDS = frozenset({"inspect", "deploy", "promote", "health_check"})

# Conservative free-form cues within Fish's documented natural-language tag
# system ("[excited]" is a documented example; multi-word cues follow the
# documented free-form usage such as "[warm, reassuring voice]").
_CUE_BY_RULE = {
    "excitement": "excited",
    "tension": "tense, urgent",
    "analytic": "calm, analytical",
    "warm": "warm, conversational",
}


def _rule_for(voice_role: str, context: Mapping[str, Any]) -> str | None:
    """One emotional beat per line; the most specific signal wins."""
    phase = str(context.get("phase", "")).strip().lower()
    kind = str(context.get("event_kind", "")).strip().lower()
    role = str(voice_role or "").strip().lower()
    if kind in CAPTURE_KINDS:
        # Excitement on capture beats, even mid-red-phase tension.
        return "excitement"
    if phase == "interview":
        return "warm"
    if phase == "red":
        return "tension"
    if phase == "recap":
        return "analytic"
    if phase == "blue" and (role == "analyst" or kind in DEFENSIVE_KINDS):
        return "analytic"
    return None


def annotate(text: str, voice_role: str, context: Mapping[str, Any] | None = None) -> str:
    """Prepend the Fish Audio emotion cue matching how a line must be spoken.

    ``context`` carries the line's editorial situation: ``phase`` (one of
    blue/red/interview/recap) and ``event_kind`` (the classified replay action,
    e.g. ``capture_attempt``, ``inspect``).  Unmatched contexts come back
    untouched so neutral lines stay verbatim, and annotation is deterministic:
    identical inputs always yield identical spoken text.
    """
    clean = str(text)
    if not clean.strip():
        return clean
    rule = _rule_for(voice_role, context or {})
    if rule is None:
        return clean
    # Sentence-level cues belong at the beginning of the line (Fish docs).
    return f"{FISH_CUE_TEMPLATE.format(cue=_CUE_BY_RULE[rule])} {clean.lstrip()}"


def line_context(
    line: Mapping[str, Any],
    *,
    terminal_events: Mapping[str, Mapping[str, Any]],
    recap_start_frame: int | None = None,
) -> dict[str, Any]:
    """Derive ``{"phase", "event_kind"}`` for one scheduled commentary line.

    ``terminal_events`` maps replay event_id -> terminal entry (the manifest's
    ``terminal`` section carries every frame's ``event_type`` and ``phase``);
    lines anchored at or after ``recap_start_frame`` are recap narration.
    Interview frames dominate their citations, then red, then blue phases.
    """
    cited = [
        terminal_events[str(event_id)]
        for event_id in (line.get("event_ids") or ())
        if str(event_id) in terminal_events
    ]
    phase = ""
    if any(
        str(entry.get("phase", "")) == "interview"
        or str(entry.get("event_type", "")) == "INTERVIEW_RECORDED"
        for entry in cited
    ):
        phase = "interview"
    elif any(str(entry.get("phase", "")) == "red" for entry in cited):
        phase = "red"
    elif any(str(entry.get("phase", "")) == "blue" for entry in cited):
        phase = "blue"
    elif recap_start_frame is not None and line.get("start_frame") is not None:
        try:
            if int(line["start_frame"]) >= int(recap_start_frame):
                phase = "recap"
        except (TypeError, ValueError):
            pass
    kinds = [_action_kind(entry) for entry in cited]
    event_kind = next((kind for kind in kinds if kind in CAPTURE_KINDS), None) or next(
        (kind for kind in kinds if kind != "other"), "other"
    )
    return {"phase": phase, "event_kind": event_kind}
