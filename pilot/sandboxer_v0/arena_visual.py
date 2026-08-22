"""Arena visualization planning for the broadcast layer.

The Remotion composition renders a small animated "arena" per Match: two
competitor avatars, a set of defense artifacts (simple colored shapes) that
each model builds during Blue Phase, and attack beats that animate an enemy
avatar approaching and shaking (or breaching) a target defense during Red
Phase.  This module *drafts* that choreography — with a text model in
production, or deterministically for a rehearsal — then validates it before it
is attached to the video manifest.

The LLM decides the semantic content (which defenses exist, their labels,
shapes and colors, and how attacks are choreographed).  Geometry, timing
safety, and rendering remain deterministic in the Remotion composition, so a
bad draft fails closed instead of producing a broken video.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Protocol, Sequence

from .agents import HeadlessAgentAdapter, phase_adapter

AVATAR_SHAPES = frozenset({"circle", "diamond", "hexagon", "triangle"})
DEFENSE_SHAPES = frozenset({"cube", "sphere", "pyramid", "hex", "shield"})
ATTACK_KINDS = frozenset({"path_traversal", "header_forgery", "parameter_probe", "token_probe", "direct"})
ATTACK_OUTCOMES = frozenset({"blocked", "breached", "partial"})
BEAT_TYPES = frozenset({"build", "attack"})


class ArenaVisualError(ValueError):
    pass


def validate_arena_plan(
    plan: Mapping[str, Any],
    frames: Sequence[Mapping[str, Any]],
    identities: Sequence[str],
) -> tuple[str, ...]:
    """Return human-readable failures (empty when the draft is valid)."""
    failures: list[str] = []
    expected = set(identities)
    avatars = plan.get("avatars")
    if not isinstance(avatars, list) or {str(a.get("competitor")) for a in avatars if isinstance(a, Mapping)} != expected:
        failures.append("avatars must cover exactly the two competitor identities")
    for index, avatar in enumerate(avatars if isinstance(avatars, list) else ()):
        if not isinstance(avatar, Mapping):
            failures.append(f"avatar {index}: not an object")
            continue
        if str(avatar.get("competitor")) not in expected:
            failures.append(f"avatar {index}: unknown competitor")
        if avatar.get("shape") not in AVATAR_SHAPES:
            failures.append(f"avatar {index}: bad shape {avatar.get('shape')!r}")
        if not str(avatar.get("label", "")).strip():
            failures.append(f"avatar {index}: empty label")

    defenses = plan.get("defenses")
    defense_ids: set[str] = set()
    defense_by_id: dict[str, Mapping[str, Any]] = {}
    if not isinstance(defenses, list) or not defenses:
        failures.append("defenses must be a non-empty list")
    else:
        for index, defense in enumerate(defenses):
            if not isinstance(defense, Mapping):
                failures.append(f"defense {index}: not an object")
                continue
            did = str(defense.get("id", ""))
            if not did or did in defense_ids:
                failures.append(f"defense {index}: missing or duplicate id")
            defense_ids.add(did)
            if str(defense.get("competitor")) not in expected:
                failures.append(f"defense {index}: unknown competitor")
            if defense.get("shape") not in DEFENSE_SHAPES:
                failures.append(f"defense {index}: bad shape {defense.get('shape')!r}")
            if not str(defense.get("label", "")).strip():
                failures.append(f"defense {index}: empty label")
            if isinstance(defense, Mapping) and did:
                defense_by_id[did] = defense

    event_ids = {str(frame["event_id"]) for frame in frames}
    red_ids = {str(frame["event_id"]) for frame in frames if frame.get("phase") == "red"}
    beats = plan.get("beats")
    if not isinstance(beats, list) or not beats:
        failures.append("beats must be a non-empty list")
    else:
        for index, beat in enumerate(beats):
            if not isinstance(beat, Mapping):
                failures.append(f"beat {index}: not an object")
                continue
            kind = beat.get("type")
            if kind not in BEAT_TYPES:
                failures.append(f"beat {index}: bad type {kind!r}")
                continue
            if kind == "build":
                if beat.get("defense") not in defense_ids:
                    failures.append(f"beat {index}: build references unknown defense")
            else:  # attack
                target = beat.get("target")
                target_defense = defense_by_id.get(str(target))
                if target_defense is None:
                    failures.append(f"beat {index}: attack references unknown target")
                else:
                    attacker = str(beat.get("attacker"))
                    if attacker not in expected:
                        failures.append(f"beat {index}: unknown attacker")
                    elif attacker == str(target_defense.get("competitor")):
                        failures.append(f"beat {index}: attacker cannot target its own defense")
                if beat.get("kind") not in ATTACK_KINDS:
                    failures.append(f"beat {index}: bad attack kind")
                if beat.get("outcome") not in ATTACK_OUTCOMES:
                    failures.append(f"beat {index}: bad outcome")
                eids = [str(item) for item in beat.get("event_ids", ())]
                if not eids or any(item not in event_ids for item in eids):
                    failures.append(f"beat {index}: attack must be grounded to real event_ids")
                elif any(item not in red_ids for item in eids):
                    failures.append(f"beat {index}: attack must be grounded to red-phase events")
            start = beat.get("start_frame")
            if not isinstance(start, int) or isinstance(start, bool) or start < 0:
                failures.append(f"beat {index}: bad start_frame")
    return tuple(failures)


class ArenaVisualDrafter(Protocol):
    """Produces an arena-visual plan from frozen replay + report."""

    def draft(self, replay: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]: ...


class FakeArenaVisualDrafter:
    """Deterministic rehearsal drafter: no model, no credentials.

    Each Blue hardening event becomes a defense artifact; each Red event
    becomes an attack beat.  Shapes cycle deterministically and labels are
    derived from the event text, so the same replay always yields the same
    plan.
    """

    SHAPES = ("cube", "sphere", "pyramid", "hex", "shield")

    def draft(self, replay: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
        panes = [str(pane["identity"]) for pane in replay.get("panes", ())]
        avatars = [
            {"competitor": panes[0], "label": panes[0].split()[0].upper(), "shape": "hexagon", "accent": "#9a65e8"},
            {"competitor": panes[1], "label": panes[1].split()[0].upper(), "shape": "diamond", "accent": "#58a9ff"},
        ]
        frames = list(replay.get("frames", ()))
        defenses: list[dict[str, Any]] = []
        beats: list[dict[str, Any]] = []
        for index, frame in enumerate(frames):
            if frame.get("phase") != "blue":
                continue
            competitor = panes[int(frame.get("pane", 0) or 0)]
            did = f"d{len(defenses)}"
            text = str(frame.get("text", ""))
            label = self._label(text, index)
            defenses.append(
                {
                    "id": did,
                    "competitor": competitor,
                    "label": label,
                    "shape": self.SHAPES[len(defenses) % len(self.SHAPES)],
                    "color": "#9a65e8" if frame.get("pane", 0) == 0 else "#58a9ff",
                    "event_ids": [str(frame["event_id"])],
                }
            )
            beats.append({"type": "build", "defense": did, "start_frame": self._frame(frame, frames)})
        for frame in frames:
            if frame.get("phase") != "red":
                continue
            attacker = panes[int(frame.get("pane", 0) or 0)]
            target = next((d["id"] for d in defenses if d["competitor"] != attacker), None)
            if target is None:
                continue
            text = str(frame.get("text", "")).lower()
            kind = self._kind(text)
            outcome = "breached" if "retrieved" in text else "blocked"
            beats.append(
                {
                    "type": "attack",
                    "attacker": attacker,
                    "target": target,
                    "kind": kind,
                    "outcome": outcome,
                    "start_frame": self._frame(frame, frames),
                    "duration_frames": 120,
                    "event_ids": [str(frame["event_id"])],
                }
            )
        return {"schema": "sandboxer.arena-visual-plan.v1", "avatars": avatars, "defenses": defenses, "beats": beats}

    def _frame(self, frame: Mapping[str, Any], frames: Sequence[Mapping[str, Any]]) -> int:
        match_start = min(int(f["at_monotonic_ns"]) for f in frames)
        return max(0, round((int(frame["at_monotonic_ns"]) - match_start) / 1_000_000_000 * 30))

    def _label(self, text: str, index: int) -> str:
        keywords = ("cipher", "fail2ban", "directory", "header", "query", "parameter", "token", "shell", "web", "service")
        lowered = text.lower()
        for keyword in keywords:
            if keyword in lowered:
                return keyword
        return f"defense {index + 1}"

    def _kind(self, text: str) -> str:
        if "path" in text or "traversal" in text:
            return "path_traversal"
        if "header" in text:
            return "header_forgery"
        if "parameter" in text or "user=" in text:
            return "parameter_probe"
        if "token" in text or "timing" in text:
            return "token_probe"
        return "direct"


class HeadlessArenaVisualDrafter:
    """Drafts the arena plan with a headless coding agent (default ``cmd``).

    The adapter is injectable for tests; without it the phase default from
    ``phase_adapter("arena")`` (env-overridable) is used.  The returned plan is
    validated before being handed back, so an ungrounded draft fails closed.
    """

    def __init__(self, *, adapter: HeadlessAgentAdapter | None = None) -> None:
        self._adapter = adapter or phase_adapter("arena")

    def draft(self, replay: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
        text = self._adapter.complete(self._prompt(replay, report))
        return self._parse(text, replay)

    def _prompt(self, replay: Mapping[str, Any], report: Mapping[str, Any]) -> str:
        identities = [str(pane["identity"]) for pane in replay.get("panes", ())]
        frames = [
            {"event_id": frame["event_id"], "phase": frame.get("phase", ""), "pane": frame.get("pane", 0), "text": frame.get("text", "")}
            for frame in replay.get("frames", ())
        ]
        return json.dumps(
            {
                "task": (
                    "Design the animation for a simulated capture-the-flag match. "
                    "Two competitor avatars each build simple defense artifacts (colored "
                    "shapes) during the blue phase; during the red phase, attack beats "
                    "animate an enemy avatar approaching and shaking (or breaching) a "
                    "target defense, with path traversal shown as movement. Keep it "
                    "simple, abstract, and grounded to the real events below."
                ),
                "rules": {
                    "avatar_shapes": sorted(AVATAR_SHAPES),
                    "defense_shapes": sorted(DEFENSE_SHAPES),
                    "attack_kinds": sorted(ATTACK_KINDS),
                    "outcomes": sorted(ATTACK_OUTCOMES),
                    "grounding": "attack beats must cite real red-phase event_ids below",
                    "frame_timing": "start_frame is an integer frame number (0 = first frame, 30 fps); use event_ids for grounding, never put an event id in start_frame",
                },
                "identities": identities,
                "outcome": report.get("outcome", {}),
                "frames": frames,
                "output_schema": {
                    "avatars": ["competitor", "label", "shape", "accent"],
                    "defenses": ["id", "competitor", "label", "shape", "color", "event_ids"],
                    "beats": "a list where each item is either {type=build, defense, start_frame} or {type=attack, attacker, target, kind, outcome, start_frame, duration_frames, event_ids}",
                },
            },
            indent=2,
        )

    def _parse(self, text: str, replay: Mapping[str, Any]) -> dict[str, Any]:
        cleaned = text.strip()
        # The model sometimes wraps the payload in ```json fences or adds a
        # preamble; extract the first JSON object defensively.
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.lstrip().startswith("json"):
                cleaned = cleaned.lstrip()[4:]
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ArenaVisualError("ARENA_VISUAL_PARSE_FAILED")
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as error:
            raise ArenaVisualError("ARENA_VISUAL_PARSE_FAILED") from error
        if not isinstance(payload, dict):
            raise ArenaVisualError("ARENA_VISUAL_EMPTY")
        self._normalize(payload)
        identities = [str(pane["identity"]) for pane in replay.get("panes", ())]
        failures = validate_arena_plan(payload, replay.get("frames", ()), identities)
        if failures:
            raise ArenaVisualError(f"ARENA_VISUAL_INVALID: {'; '.join(failures)}")
        return dict(payload)

    @staticmethod
    def _normalize(payload: dict[str, Any]) -> None:
        """Coerce model-friendly numbers (float/str) to the ints the renderer needs.

        The text model legitimately emits ``start_frame: 12.0`` or ``"12"``; the
        Remotion composition consumes integers.  Booleans and non-numeric values
        are left untouched so validation still fails closed.
        """
        for beat in payload.get("beats", ()) if isinstance(payload.get("beats"), list) else ():
            if not isinstance(beat, dict):
                continue
            for key in ("start_frame", "duration_frames"):
                value = beat.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    continue
                if isinstance(value, float) and value.is_integer():
                    beat[key] = int(value)
                elif isinstance(value, str):
                    stripped = value.strip()
                    if stripped.lstrip("-").isdigit():
                        beat[key] = int(stripped)
                    else:
                        # Non-numeric (e.g. an event id the model confused with a
                        # frame number): the renderer grounds timing via event_ids,
                        # so a zero fallback is safe and validation stays strict.
                        beat[key] = 0
                elif value is None:
                    beat[key] = 0


def draft_arena_plan(
    replay: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    drafter: ArenaVisualDrafter | None = None,
) -> dict[str, Any]:
    """Draft and validate an arena plan; falls back to the deterministic drafter."""
    identities = [str(pane["identity"]) for pane in replay.get("panes", ())]
    frames = list(replay.get("frames", ()))
    plan = (drafter or FakeArenaVisualDrafter()).draft(replay, report)
    failures = validate_arena_plan(plan, frames, identities)
    if failures:
        raise ArenaVisualError(f"ARENA_VISUAL_INVALID: {'; '.join(failures)}")
    return plan


__all__ = [
    "ArenaVisualDrafter",
    "ArenaVisualError",
    "FakeArenaVisualDrafter",
    "HeadlessArenaVisualDrafter",
    "draft_arena_plan",
    "validate_arena_plan",
]
