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
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping, Protocol, Sequence


class CommentaryError(ValueError):
    pass


COMMENTARY_LINE_TYPES = frozenset({"observed", "interpreted", "editorial"})
COMMENTARY_ROLES = frozenset({"play_by_play", "analyst"})
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


class GeminiCommentaryDrafter:
    """Drafts commentary with a Gemini text model.

    The client is injectable for tests; without it a control-plane
    ``GEMINI_API_KEY`` is used lazily, mirroring ``GeminiTtsAdapter``.  The
    returned lines are validated before being handed back, so an ungrounded or
    untyped draft fails closed rather than reaching the manifest.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._client = client

    def _genai_client(self) -> Any:
        if self._client is not None:
            return self._client
        key = self._api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise CommentaryError("COMMENTARY_KEY_MISSING")
        from google import genai  # lazy: tests and dry runs never import the SDK

        return genai.Client(api_key=key)

    def draft(
        self, replay: Mapping[str, Any], report: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        client = self._genai_client()
        response = client.models.generate_content(
            model=self.model, contents=self._prompt(replay, report)
        )
        return self._parse(response.text, replay)

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
                },
                "identities": identities,
                "outcome": report.get("outcome", {}),
                "frames": frames,
                "output": '{"lines":[{"voice_role","line_type","event_ids","text"}, ...]}',
            },
            indent=2,
        )

    def _parse(self, text: str, replay: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise CommentaryError("COMMENTARY_PARSE_FAILED") from error
        lines = payload.get("lines") if isinstance(payload, dict) else None
        if not isinstance(lines, list) or not lines:
            raise CommentaryError("COMMENTARY_EMPTY")
        failures = validate_commentary(lines, replay.get("frames", ()))
        if failures:
            raise CommentaryError(f"COMMENTARY_INVALID: {'; '.join(failures)}")
        return [dict(line) for line in lines]
