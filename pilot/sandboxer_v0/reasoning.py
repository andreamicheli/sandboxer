"""Model-assisted sanitization of private model reasoning for publication.

Raw reasoning (chain-of-thought) is sensitive: it can leak credentials,
internal paths, Synthetic Flags, or attack detail.  It is never published raw.
A sanitizer produces a short, viewer-safe "thinking" line instead:

1. **Deterministic pre-pass** — ``replay.sanitize_terminal_text`` irreversibly
   removes credentials, secrets, flags, paths, URLs, and private network
   markers *before* any model sees the text (defense in depth: a model is never
   handed a secret to leak).
2. **Model rewrite (optional)** — a text model rewrites the remainder into a
   short, neutral, non-technical summary so dangerous or confusing detail is
   softened, not just masked.
3. **Validation** — the result must contain no remaining hazard and no explicit
   credential marker, and stays under a length cap.
4. **Human approval** — a sanitized line is ``approved=False`` until a reviewer
   signs it; publication gates refuse unapproved lines.

This module is intentionally not wired into the replay/video path by default:
raw reasoning stays out of public artifacts until an operator opts in and the
sanitized output is reviewed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .replay import sanitize_terminal_text


class ReasoningError(ValueError):
    pass


MAX_THOUGHT_CHARS = 280


@dataclass(frozen=True)
class SanitizedThought:
    """A publication-safe stand-in for a raw reasoning trace."""

    text: str
    method: str                       # "deterministic" or "model"
    model: str | None
    redacted_any: bool                # the deterministic pass removed content
    approved: bool                    # human approval is a separate gate

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "method": self.method,
            "model": self.model,
            "redacted_any": self.redacted_any,
            "approved": self.approved,
        }


def validate_sanitized_thought(text: str) -> tuple[str, ...]:
    """Return leak/quality failures (empty when the line is safe to publish).

    Credentials, secrets, flags, paths, URLs and private-network markers are all
    caught deterministically by ``sanitize_terminal_text``: if re-sanitizing the
    candidate changes it, a hazard survived (``LEAK_REMAINS``).
    """
    failures: list[str] = []
    stripped = str(text or "")
    if not stripped.strip():
        failures.append("EMPTY")
    if len(stripped) > MAX_THOUGHT_CHARS:
        failures.append("TOO_LONG")
    # A deterministic hazard survived: sanitize must be a fixpoint.
    if sanitize_terminal_text(stripped) != stripped:
        failures.append("LEAK_REMAINS")
    return tuple(failures)


class ReasoningSanitizer(Protocol):
    def sanitize(
        self, reasoning_text: str, *, context: Mapping[str, Any] | None = None
    ) -> SanitizedThought: ...


class GeminiReasoningSanitizer:
    """Deterministic pre-pass + optional Gemini rewrite, then validation.

    The client is injectable for tests; without it (or without
    ``GEMINI_API_KEY``) the sanitizer degrades to the deterministic pass, which
    still removes secrets but keeps the (redacted) wording closer to the raw
    trace.
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
            raise ReasoningError("REASONING_KEY_MISSING")
        from google import genai  # lazy: deterministic-only mode needs no SDK

        return genai.Client(api_key=key)

    def sanitize(
        self, reasoning_text: str, *, context: Mapping[str, Any] | None = None
    ) -> SanitizedThought:
        raw = str(reasoning_text or "")
        redacted = sanitize_terminal_text(raw)
        redacted_any = redacted != raw
        if not redacted.strip():
            return SanitizedThought(
                text="[thinking redacted]", method="deterministic",
                model=None, redacted_any=True, approved=False,
            )

        # Deterministic-only mode: no model, no rewrite.  The redacted text may
        # still be too detailed, so it stays unapproved until a human reviews it.
        try:
            client = self._genai_client()
        except ReasoningError:
            return SanitizedThought(
                text=redacted[:MAX_THOUGHT_CHARS], method="deterministic",
                model=None, redacted_any=redacted_any, approved=False,
            )

        # A model failure or a failing rewrite never reaches the public path:
        # fall back to the deterministic pass.
        try:
            rewritten = self._rewrite(client, redacted, context or {})
            failures = validate_sanitized_thought(rewritten)
        except Exception:
            failures = ("MODEL_FAILED",)
        if failures:
            return SanitizedThought(
                text=redacted[:MAX_THOUGHT_CHARS], method="deterministic",
                model=None, redacted_any=redacted_any, approved=False,
            )
        return SanitizedThought(
            text=rewritten, method="model", model=self.model,
            redacted_any=redacted_any, approved=False,
        )

    def _rewrite(self, client: Any, redacted: str, context: Mapping[str, Any]) -> str:
        prompt = json.dumps(
            {
                "task": (
                    "Rewrite this redacted model reasoning into ONE short, neutral, "
                    "viewer-friendly sentence for a simulated capture-the-flag video. "
                    "Never mention credentials, secrets, paths, flags, or any concrete "
                    "attack technique. Do not repeat the redaction markers. Keep it "
                    "under 280 characters."
                ),
                "context": dict(context),
                "reasoning": redacted,
            },
            indent=2,
        )
        response = client.models.generate_content(model=self.model, contents=prompt)
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise ReasoningError("REASONING_REWRITE_EMPTY")
        return text.strip()
