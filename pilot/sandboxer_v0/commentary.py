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


# --- Deterministic dense play-by-play ----------------------------------------
#
# Episode-postmortem rule: drafted commentary covered only a fraction of the
# terminal events (v7: 11 lines for 26 events) and never narrated the blue
# phase.  ``build_commentary`` derives coverage straight from the replay
# frames instead: every meaningful action gets an immediate play-by-play line
# anchored at its own event frame, analyst lines interpret patterns using only
# observed events, and dead air longer than fifteen seconds is filled with a
# recap of actions already seen.  Nothing is invented: every sentence restates
# frame fields (event type, phase, pane identity, terminal text) or arithmetic
# over telemetry timestamps.

PROVENANCE_DETERMINISTIC = "deterministic"
"""Line content derived mechanically from telemetry (never model prose)."""

# Replay vocabulary treated as narratable action.  ``artifact_converter.convert``
# emits MATCH_STARTED / PHASE_TRANSITION / MODEL_RESPONSE / TOOL_CALL /
# MATCH_FINISHED; series-level producers add gate-opening and verified-submission
# events.  INTERVIEW_RECORDED is deliberately excluded: interviews get their own
# fullscreen scene rather than play-by-play.
ACTION_EVENT_TYPES = frozenset({
    "MATCH_STARTED", "TOOL_CALL", "MODEL_RESPONSE", "PHASE_TRANSITION",
    "MATCH_FINISHED", "SUBMISSION_VERIFIED", "PHASE_GATE_OPENED",
})

LINE_WINDOW_SECONDS = 8      # a line's [start_frame, end_frame) window cap
DEAD_AIR_SECONDS = 15        # silence longer than this earns a filler recap
LINE_TARGET_SECONDS = 4.0    # soft per-line speech budget (chars/15 TTS pace)
_CHARS_PER_SECOND = 15       # rough Fish TTS pace used for the estimate
_RECAP_PHRASES_MAX = 2       # dead-air filler concatenates at most this many
ANALYST_EVERY = 3            # every Nth consecutive action also earns an analyst beat
ANALYST_DELAY_SECONDS = 2    # analyst follow-up lands this long after its action
REPEATED_STREAK = 3          # consecutive same-actor offensive actions -> pattern note
SLOW_PROMOTION_RATIO = 2.0   # promotion elapsed-time gap worth narrating (~10x in v7)

_OFFENSIVE_KINDS = frozenset({"recon", "probe", "capture_attempt"})
_DEFENSIVE_KINDS = frozenset({"inspect", "deploy", "promote", "health_check"})

_KIND_PHRASE = {
    "match_start": "the match open",
    "inspect": "an inspect",
    "deploy": "a deploy",
    "promote": "a defense promotion",
    "health_check": "a self-health check",
    "capture_attempt": "a flag submission",
    "recon": "target recon",
    "probe": "an HTTP probe",
    "wrap": "a phase wrap-up",
    "phase": "a phase call",
    "gate": "a gate opening",
    "submission": "a verified submission",
    "finish": "the final whistle",
    "other": "a tool action",
}


def _action_kind(frame: Mapping[str, Any]) -> str:
    """Classify one replay frame from its event type and redacted text."""
    event_type = str(frame.get("event_type", ""))
    text = str(frame.get("text", "")).strip().lower()
    if event_type == "MATCH_STARTED":
        return "match_start"
    if event_type == "MATCH_FINISHED":
        return "finish"
    if event_type == "PHASE_TRANSITION":
        return "phase"
    if event_type == "PHASE_GATE_OPENED":
        return "gate"
    if event_type == "SUBMISSION_VERIFIED":
        return "submission"
    if text.startswith("defense promoted"):
        return "promote"
    for prefix, kind in (
        ("verify: self-service", "health_check"),
        ("verify: objective token", "capture_attempt"),
        ("inspect:", "inspect"),
        ("deploy:", "deploy"),
        ("target:", "recon"),
        ("probe:", "probe"),
        ("finish:", "wrap"),
    ):
        if text.startswith(prefix):
            return kind
    return "other"


def _pane_identity(frame: Mapping[str, Any], identities: Sequence[str]) -> str:
    try:
        pane = int(frame.get("pane"))
    except (TypeError, ValueError):
        return ""
    if 0 <= pane < len(identities):
        return identities[pane]
    return ""


def _sentence(text: str) -> str:
    cleaned = str(text).strip()
    if not cleaned:
        return ""
    sentence = cleaned[0].upper() + cleaned[1:]
    return sentence if sentence[-1] in ".!?" else sentence + "."


def _action_text(kind: str, name: str, raw_text: str) -> str:
    """A factual restatement of one action; only frame material is used."""
    if kind == "match_start":
        if name:
            return f"{name} brings its service up as the match starts."
        return "The match starts: two isolated services, one flag each."
    if kind == "inspect":
        return f"{name} inspects its declared service surface." if name else "Declared service surface inspected."
    if kind == "deploy":
        return f"{name} deploys its proposed service spec." if name else "Proposed service spec deployed."
    if kind == "health_check":
        return f"{name} runs a self-service health check." if name else "Self-service health check runs."
    if kind == "capture_attempt":
        return f"{name} goes for the flag - an objective token submission lands." if name else "Objective token submission - a capture attempt."
    if kind == "recon":
        return f"{name} pulls up the opponent's service contract." if name else "Opponent service contract described."
    if kind == "probe":
        return f"{name} probes the target over HTTP." if name else "HTTP probe hits the target."
    if kind == "wrap":
        return f"{name} wraps up the phase." if name else "Phase wrapped up."
    if kind == "promote":
        detail = raw_text.split(":", 1)[1].strip() if ":" in raw_text else raw_text.strip()
        return f"{name} promotes a live defense: {detail}." if name else f"Defense promoted: {detail}."
    if kind == "submission":
        if raw_text.strip():
            return _sentence(raw_text)
        return f"{name} has an objective submission verified." if name else "Objective submission verified."
    if raw_text.strip():
        return _sentence(raw_text)
    return f"{name} acts." if name else "Action logged."


def _recap_phrase(group: Mapping[str, Any]) -> str:
    kind = str(group.get("kind", "other"))
    name = str(group.get("name", ""))
    phrase = _KIND_PHRASE.get(kind, kind)
    return f"{phrase} from {name}" if name else phrase


def _shorten(text: str) -> str:
    """Deterministically tighten an over-budget line without losing facts.

    Episode-v8g: dense series commentary totalled ~361s of speech against a
    ~326s window.  The estimate is chars/15s; over-budget play-by-play lines
    drop trailing clauses after the second comma, filler prefixes are
    stripped, whitespace collapses.  Event grounding is never touched.
    """
    cleaned = " ".join(str(text).split())
    if len(cleaned) / _CHARS_PER_SECOND <= LINE_TARGET_SECONDS:
        return cleaned
    prefixes = ("While the arena holds quiet, the story so far: ",
                "Story so far: ")
    for prefix in prefixes:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    if len(cleaned) / _CHARS_PER_SECOND > LINE_TARGET_SECONDS:
        parts = cleaned.split(", ")
        if len(parts) > 2:
            cleaned = ", ".join(parts[:2]).rstrip(",") + "."
    return " ".join(cleaned.split())


def build_commentary(
    replay_frames: Sequence[Mapping[str, Any]],
    identities: Sequence[str],
    fps: int = 30,
    *,
    speak_boundary: int | None = None,
) -> list[dict[str, Any]]:
    """Dense deterministic two-voice commentary derived from replay frames.

    Coverage contract (episode v7 postmortem):

    - every action frame (:data:`ACTION_EVENT_TYPES`) is cited by at least one
      line's ``event_ids``, with ``start_frame`` aligned to the event frame;
    - every ~3rd consecutive action, any repeated-offense streak, and any
      lopsided match-start-to-defense-promotion elapsed time also earn an
      analyst line shortly after, interpreting only what was observed;
    - silence longer than :data:`DEAD_AIR_SECONDS` between lines is filled
      with an analyst recap of already-seen actions;
    - windows never overlap: ``end_frame = min(next start, start + fps*8)``;
    - identical inputs yield byte-identical output and empty input yields [].
    """
    fps = int(fps)
    names = [str(item) for item in identities]
    frames = [dict(frame) for frame in replay_frames]
    if not frames or fps <= 0:
        return []
    base_ns = min(int(frame.get("at_monotonic_ns", 0)) for frame in frames)

    def _rel(ns: Any) -> int:
        return max(0, round((int(ns) - base_ns) / 1_000_000_000 * fps))

    # Consecutive action frames landing on the same aligned frame merge into
    # one line citing all their ids (the converter emits the match-start pair
    # one nanosecond apart).
    groups: list[dict[str, Any]] = []
    for frame in frames:
        if str(frame.get("event_type", "")) not in ACTION_EVENT_TYPES:
            continue
        rel = _rel(frame.get("at_monotonic_ns", base_ns))
        if groups and groups[-1]["start"] == rel:
            groups[-1]["event_ids"].append(str(frame["event_id"]))
            continue
        groups.append({
            "start": rel,
            "ns": int(frame.get("at_monotonic_ns", base_ns)),
            "kind": _action_kind(frame),
            "name": _pane_identity(frame, names),
            "raw": str(frame.get("text", "")),
            "event_ids": [str(frame["event_id"])],
        })
    if not groups:
        return []

    open_ns = groups[0]["ns"]
    promotion_ns: dict[str, tuple[int, list[str]]] = {}
    slow_note_done = False
    streak_actor: str | None = None
    streak_ids: list[str] = []
    streak_phrases: list[str] = []

    pending: dict[int, list[tuple[int, dict[str, Any]]]] = {}

    def _queue(index: int, priority: int, *, text: str, ids: list[str], line_type: str) -> None:
        pending.setdefault(index, []).append(
            (priority, {"role": "analyst", "model": "", "text": text,
                        "ids": list(ids), "type": line_type}))

    for index, group in enumerate(groups):
        kind, name = group["kind"], group["name"]

        # Repeated same-actor offense: a notable pattern worth interpreting.
        if kind in _OFFENSIVE_KINDS and name:
            if name != streak_actor:
                streak_actor, streak_ids, streak_phrases = name, [], []
            streak_ids.extend(group["event_ids"])
            streak_phrases.append(_KIND_PHRASE.get(kind, kind))
            if len(streak_phrases) == REPEATED_STREAK:
                _queue(index, 1,
                       text=(f"That is {REPEATED_STREAK} offensive actions in a row from {name} - "
                             f"{', '.join(streak_phrases)}. It looks like sustained pressure on the defender."),
                       ids=streak_ids, line_type="interpreted")
                streak_actor, streak_ids, streak_phrases = name, [], []
        else:
            streak_actor, streak_ids, streak_phrases = None, [], []

        # Lopsided defense promotion: compare elapsed times from telemetry.
        if kind == "promote" and name and name not in promotion_ns:
            promotion_ns[name] = (group["ns"], list(group["event_ids"]))
            if len(promotion_ns) == 2 and not slow_note_done:
                (fast_name, fast_data), (slow_name, slow_data) = sorted(
                    promotion_ns.items(), key=lambda item: item[1][0])
                fast_s = (fast_data[0] - open_ns) / 1_000_000_000
                slow_s = (slow_data[0] - open_ns) / 1_000_000_000
                if fast_s > 0 and slow_s / fast_s >= SLOW_PROMOTION_RATIO:
                    slow_note_done = True
                    _queue(index, 0,
                           text=(f"From the telemetry clock: {slow_name} needed {slow_s:.0f} seconds from "
                                 f"match start to a promoted defense; {fast_name} did it in {fast_s:.0f} - "
                                 f"roughly {slow_s / fast_s:.0f} times slower."),
                           ids=list(fast_data[1]) + list(slow_data[1]),
                           line_type="observed")

        # Every Nth consecutive action earns an analyst recap of the run.
        if (index + 1) % ANALYST_EVERY == 0:
            window = groups[max(0, index - ANALYST_EVERY + 1): index + 1]
            ids = [event_id for entry in window for event_id in entry["event_ids"]]
            phrases = "; ".join(_recap_phrase(entry) for entry in window)
            offense = sum(1 for entry in window if entry["kind"] in _OFFENSIVE_KINDS)
            defense = sum(1 for entry in window if entry["kind"] in _DEFENSIVE_KINDS)
            if offense == 0 and defense > 0:
                closing = "It appears the defenses are still taking shape."
            elif offense >= defense:
                closing = "It looks like the attack is setting the tempo."
            else:
                closing = "It seems both sides are trading blows."
            _queue(index, 2, text=f"Reading back the last few beats: {phrases}. {closing}",
                   ids=ids, line_type="interpreted")

    entries: list[dict[str, Any]] = []
    play_group_at: dict[int, int] = {}

    def _emit(role: str, model: str, start: int, text: str,
              ids: list[str], line_type: str) -> None:
        entries.append({"role": role, "model": model, "start": int(start),
                        "text": text, "ids": [str(item) for item in ids],
                        "type": line_type})

    for index, group in enumerate(groups):
        start = group["start"]
        _emit("play_by_play", group["name"], start,
              _action_text(group["kind"], group["name"], group["raw"]),
              group["event_ids"], "observed")
        play_group_at[len(entries) - 1] = index
        next_start = groups[index + 1]["start"] if index + 1 < len(groups) else None
        limit = next_start if next_start is not None else start + fps * LINE_WINDOW_SECONDS
        cursor = start + 1  # the play-by-play line keeps at least one frame
        for _, candidate in sorted(pending.get(index, ()), key=lambda item: item[0]):
            begin = max(start + fps * ANALYST_DELAY_SECONDS, cursor)
            if begin + fps > limit:
                middle = start + max(1, (limit - start) // 2)
                begin = max(cursor, min(begin, middle))
            if begin + fps > limit:
                continue  # no room for a spoken window before the next action
            cursor = begin + fps  # reserve at least one spoken second
            _emit(candidate["role"], candidate["model"], begin,
                  candidate["text"], candidate["ids"], candidate["type"])

    # Dead-air filler: real gaps between windows become analyst recaps that
    # cite only actions the viewer has already seen.
    filled: list[dict[str, Any]] = []
    for position, entry in enumerate(entries):
        filled.append(entry)
        if position + 1 >= len(entries):
            continue
        gap = entries[position + 1]["start"] - entry["start"]
        recent_index = play_group_at.get(position)
        if gap <= fps * DEAD_AIR_SECONDS or recent_index is None:
            continue
        window = groups[max(0, recent_index - 2): recent_index + 1]
        ids = [event_id for item in window for event_id in item["event_ids"]]
        phrases = "; ".join(_recap_phrase(item) for item in window[:_RECAP_PHRASES_MAX])
        filler_start = max(entry["start"] + 1,
                           entry["start"] + fps * LINE_WINDOW_SECONDS)
        filled.append({"role": "analyst", "model": "",
                       "start": min(filler_start, entries[position + 1]["start"] - fps),
                       "text": f"While the arena holds quiet, the story so far: {phrases}.",
                       "ids": ids, "type": "observed"})

    lines: list[dict[str, Any]] = []
    for position, entry in enumerate(filled):
        next_start = filled[position + 1]["start"] if position + 1 < len(filled) else None
        end = entry["start"] + fps * LINE_WINDOW_SECONDS
        if next_start is not None:
            end = min(end, next_start)
        lines.append({
            "voice_role": entry["role"],
            "model": entry["model"],
            "start_frame": entry["start"],
            "end_frame": max(end, entry["start"] + 1),
            "text": _shorten(entry["text"]),
            "event_ids": list(entry["ids"]),
            "line_type": entry["type"],
            "provenance": PROVENANCE_DETERMINISTIC,
        })
    if speak_boundary is not None:
        boundary = int(speak_boundary)
        lines = [line for line in lines if line["start_frame"] < boundary]
        for line in lines:
            line["end_frame"] = min(line["end_frame"], max(boundary - 1, line["start_frame"] + 1))
    return lines


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
            "scene": "model_cards_and_rules",
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
INTRO_SCENES = frozenset({"model_cards_and_rules"})
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
                    "offset_seconds": "non-negative float, RELATIVE TO THE START of its scene; model_cards_and_rules spans 0-20s",
                    "length": "produce at most 6 lines total in model_cards_and_rules; keep each line under 20 words",
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
    "ACTION_EVENT_TYPES",
    "CommentaryDrafter",
    "CommentaryError",
    "HeadlessCommentaryDrafter",
    "HeadlessIntroCommentaryDrafter",
    "IntroCommentaryDrafter",
    "PROVENANCE_DETERMINISTIC",
    "build_commentary",
    "draft_commentary",
    "draft_intro_commentary",
    "fallback_intro_commentary",
    "repair_line_types",
    "validate_commentary",
    "validate_intro_commentary",
]
