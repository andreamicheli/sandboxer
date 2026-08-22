"""Headless coding-agent adapters for content-producing phases.

The broadcast and report layers draft prose and structured JSON (commentary,
arena choreography, narrative, intro, review) with the locally-installed coding
agents — ``codex``, ``cmd`` and ``agy`` — invoked as headless subprocesses.
Each adapter is a thin, fail-closed seam: no native tools cross it (only the
final text/JSON answer is returned), timeouts / non-zero exits / empty output
raise a stable ``HeadlessAgentError`` reason code, and callers keep a
deterministic fallback so a failed draft never blocks publication.

Selection is env-driven per phase (``SANDBOXER_<PHASE>_AGENT``); defaults live
in ``PHASE_DEFAULTS``.  ``agy`` (Antigravity Gemini) is implemented for
completeness but is geo-blocked on hosts that return "User location is not
supported for the API use"; such phases fall back to ``cmd`` or ``codex``, or
to their deterministic drafters.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol, Sequence

DEFAULT_TIMEOUT_SECONDS = 240.0

# Explicit default models per agent kind (verified against the live catalogs):
#   codex -> gpt-5.6-luna  ("Luna 5.6", OpenAI)
#   cmd   -> meta/muse-spark-1.2-contributor  (Muse Spark Contributor)
# Override per phase via SANDBOXER_<PHASE>_MODEL, or per kind via the
# adapter's ``model`` argument.
CODEX_DEFAULT_MODEL = "gpt-5.6-luna"
CMD_DEFAULT_MODEL = "meta/muse-spark-1.2-contributor"


class HeadlessAgentError(RuntimeError):
    """A stable, non-sensitive failure at the agent subprocess boundary."""

    def __init__(self, reason_code: str, *, detail: str | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.detail = detail


class HeadlessAgentAdapter(Protocol):
    """Runs a single prompt headlessly and returns the model's final text.

    The returned text may be prose or JSON; the caller (a drafter) is
    responsible for parsing and validating it against the phase schema.
    """

    def complete(self, prompt: str) -> str: ...


def _run(
    command: Sequence[str],
    *,
    binary_kind: str,
    timeout_seconds: float,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd) if cwd is not None else None,
        )
    except FileNotFoundError as error:
        raise HeadlessAgentError(f"{binary_kind}_BINARY_MISSING") from error
    except subprocess.TimeoutExpired as error:
        raise HeadlessAgentError(f"{binary_kind}_TIMEOUT") from error


class CodexAdapter:
    """OpenAI Codex in headless exec mode (``codex exec … -s read-only``).

    ``-o/--output-last-message`` writes only the final assistant message, which
    is what we return; the transcript and usage banner on stdout are ignored.
    """

    def __init__(
        self,
        *,
        model: str | None = CODEX_DEFAULT_MODEL,
        executable: Sequence[str] = ("codex",),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self._executable = tuple(executable)
        self._timeout = timeout_seconds

    def complete(self, prompt: str) -> str:
        if not prompt.strip():
            raise HeadlessAgentError("CODEX_EMPTY_PROMPT")
        with tempfile.TemporaryDirectory(prefix="sandboxer-codex-") as temporary:
            out = Path(temporary) / "last-message.txt"
            command: list[str] = [*self._executable, "exec", prompt, "-m", self.model, "-s", "read-only", "-o", str(out)]
            proc = _run(command, binary_kind="CODEX", timeout_seconds=self._timeout)
            if proc.returncode != 0:
                raise HeadlessAgentError("CODEX_FAILED", detail=(proc.stderr or "")[-400:].strip())
            if not out.exists():
                raise HeadlessAgentError("CODEX_EMPTY")
            text = out.read_text(encoding="utf-8").strip()
            if not text:
                raise HeadlessAgentError("CODEX_EMPTY")
            return text


class CmdAdapter:
    """Command Code in headless print mode (``cmd -p … --output-format json``).

    Parses the NDJSON stream and returns the final result's ``finalText``,
    stripping any leading ``<think>…</think>`` block.  Error subtypes are mapped
    to stable reason codes.
    """

    def __init__(
        self,
        *,
        model: str | None = CMD_DEFAULT_MODEL,
        executable: Sequence[str] = ("/usr/local/bin/cmd",),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self._executable = tuple(executable)
        self._timeout = timeout_seconds

    def complete(self, prompt: str) -> str:
        if not prompt.strip():
            raise HeadlessAgentError("CMD_EMPTY_PROMPT")
        command: list[str] = [
            *self._executable,
            "-p",
            prompt,
            "--model",
            self.model,
            "--output-format",
            "json",
            "--no-session",
            "--no-auto-update",
            "--no-skills",
            "--skip-onboarding",
            "--permission-mode",
            "dont-ask",
        ]
        proc = _run(command, binary_kind="CMD", timeout_seconds=self._timeout)
        if proc.returncode != 0:
            raise HeadlessAgentError("CMD_FAILED", detail=(proc.stderr or "")[-400:].strip())
        return self._final_text(proc.stdout)

    @staticmethod
    def _final_text(stdout: str) -> str:
        result: dict[str, Any] | None = None
        for raw in stdout.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict) and frame.get("type") == "result":
                result = frame
        if result is None:
            raise HeadlessAgentError("CMD_STREAM_EMPTY")
        if result.get("subtype") == "error":
            message = str(result.get("error", "")).lower()
            if any(token in message for token in ("auth", "login", "authenticated")):
                raise HeadlessAgentError("CMD_AUTH_REQUIRED")
            if any(token in message for token in ("credit", "balance", "quota")):
                raise HeadlessAgentError("CMD_CREDITS_INSUFFICIENT")
            if any(token in message for token in ("rate", "concurrency", "too many")):
                raise HeadlessAgentError("CMD_CAPACITY_UNAVAILABLE")
            raise HeadlessAgentError("CMD_PROVIDER_FAILURE")
        text = result.get("finalText")
        if not isinstance(text, str) or not text.strip():
            raise HeadlessAgentError("CMD_FINAL_TEXT_MISSING")
        tagged = re.match(r"^\s*<think>(.*?)</think>\s*", text, re.S | re.I)
        if tagged:
            text = text[tagged.end() :]
        text = text.strip()
        if not text:
            raise HeadlessAgentError("CMD_FINAL_TEXT_MISSING")
        return text


class AgyAdapter:
    """Antigravity Gemini (``agy``) in headless print mode.

    Emits a single JSON object on ``--output-format json``; the ``response``
    field is returned, and ``status: ERROR`` is surfaced (a geo-block shows up
    here as ``User location is not supported``).
    """

    def __init__(
        self,
        *,
        model: str = "gemini-3.7-flash-high",
        executable: Sequence[str] = ("agy",),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self._executable = tuple(executable)
        self._timeout = timeout_seconds

    def complete(self, prompt: str) -> str:
        if not prompt.strip():
            raise HeadlessAgentError("AGY_EMPTY_PROMPT")
        command: list[str] = [
            *self._executable,
            "--print",
            prompt,
            "--model",
            self.model,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
        ]
        proc = _run(command, binary_kind="AGY", timeout_seconds=self._timeout)
        if proc.returncode != 0:
            raise HeadlessAgentError("AGY_FAILED", detail=(proc.stderr or "")[-400:].strip())
        try:
            payload = json.loads(proc.stdout.strip())
        except json.JSONDecodeError as error:
            raise HeadlessAgentError("AGY_PARSE_FAILED") from error
        if not isinstance(payload, dict):
            raise HeadlessAgentError("AGY_PARSE_FAILED")
        if payload.get("status") == "ERROR":
            error = str(payload.get("error", "")).strip()
            stderr = (proc.stderr or "").strip()
            detail = error or stderr
            if "location" in f"{error} {stderr}".lower():
                raise HeadlessAgentError("AGY_GEOBLOCKED", detail=detail)
            raise HeadlessAgentError("AGY_FAILED", detail=detail)
        text = payload.get("response")
        if not isinstance(text, str) or not text.strip():
            raise HeadlessAgentError("AGY_EMPTY")
        return text.strip()


class HermesAdapter:
    """Hermes CLI (Nous Research) in headless single-query mode.

    Temporary substitute for ``codex``/``agy`` while those backends are
    unavailable on this host: runs ``hermes chat -Q -q <prompt>`` and returns
    the final response text.  The default model comes from the Hermes config
    (``~/.hermes/config.yaml`` -> ``orcarouter/free``), overridable per
    adapter via ``model``/``-m``.

    ``retries``/``retry_base_seconds`` retry the whole query on transient
    failures (timeout, non-zero exit) — the free-tier backend behind Hermes
    occasionally stalls or rate-limits a single heavy prompt, so a bounded
    retry is what keeps content phases from falling back to authored text.
    """

    _TRANSIENT = ("HERMES_TIMEOUT", "HERMES_FAILED")

    def __init__(
        self,
        *,
        model: str | None = None,
        executable: Sequence[str] = ("hermes",),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        retries: int = 2,
        retry_base_seconds: float = 3.0,
    ) -> None:
        self.model = model
        self._executable = tuple(executable)
        self._timeout = timeout_seconds
        self._retries = max(0, int(retries))
        self._retry_base = retry_base_seconds

    def complete(self, prompt: str) -> str:
        if not prompt.strip():
            raise HeadlessAgentError("HERMES_EMPTY_PROMPT")
        attempt = 0
        while True:
            try:
                return self._complete_once(prompt)
            except HeadlessAgentError as error:
                if error.reason_code not in self._TRANSIENT or attempt >= self._retries:
                    raise
                attempt += 1
                time.sleep(self._retry_base * attempt)

    def _complete_once(self, prompt: str) -> str:
        command: list[str] = [*self._executable, "chat", "-Q", "-q", prompt]
        if self.model:
            command += ["-m", self.model]
        proc = _run(command, binary_kind="HERMES", timeout_seconds=self._timeout)
        if proc.returncode != 0:
            raise HeadlessAgentError("HERMES_FAILED", detail=(proc.stderr or "")[-400:].strip())
        text = proc.stdout.strip()
        # Strip any trailing session_id / info lines hermes prints after the answer.
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise HeadlessAgentError("HERMES_EMPTY")
        return "\n".join(lines).strip()


AGENT_KINDS = ("codex", "cmd", "agy", "hermes")

# Phase → default agent kind.  ``agy`` (Antigravity Gemini, default model
# gemini-3.7-flash-high) is the preferred backend for prose phases — intro and
# report narrative — per the editorial brief.  It is geo-blocked on some hosts
# ("User location is not supported"); the deterministc drafters then take
# over, so a geo-block never blocks publication.
PHASE_DEFAULTS = {
    "commentary": "codex",
    "arena": "cmd",
    "report_narrative": "agy",
    "intro": "agy",
}


def resolve_adapter(kind: str, *, model: str | None = None) -> HeadlessAgentAdapter:
    """Resolve an agent kind (``codex``/``cmd``/``agy``) to an adapter.

    A ``None`` model leaves the adapter's own default in place (e.g. codex
    defaults to ``gpt-5.6-luna``, cmd to Muse Spark Contributor), so an
    explicit value is only passed through when one is given.
    """
    normalized = (kind or "").strip().lower()
    # Only pass an explicit model through; None keeps the adapter's own default
    # (codex -> gpt-5.6-luna, cmd -> Muse Spark Contributor).
    if normalized == "codex":
        return CodexAdapter(model=model) if model else CodexAdapter()
    if normalized == "cmd":
        return CmdAdapter(model=model) if model else CmdAdapter()
    if normalized == "agy":
        return AgyAdapter(model=model) if model else AgyAdapter()
    if normalized == "hermes":
        return HermesAdapter(model=model) if model else HermesAdapter()
    raise HeadlessAgentError("AGENT_UNKNOWN")


def phase_adapter(phase: str, *, model: str | None = None) -> HeadlessAgentAdapter:
    """Return the adapter for a content phase, overridable via env.

    ``SANDBOXER_COMMENTARY_AGENT=cmd``, for example, switches the commentary
    drafter to Command Code; ``SANDBOXER_COMMENTARY_MODEL=...`` overrides the
    model within that agent kind.  The phase name is normalized to
    ``[a-z0-9_]`` before the env lookup, so ``report_narrative`` reads
    ``SANDBOXER_REPORT_NARRATIVE_AGENT`` / ``SANDBOXER_REPORT_NARRATIVE_MODEL``.
    """
    key = re.sub(r"[^a-z0-9]", "_", (phase or "").strip().lower()).strip("_")
    kind = os.environ.get(f"SANDBOXER_{key.upper()}_AGENT") or PHASE_DEFAULTS.get(key) or "codex"
    resolved_model = os.environ.get(f"SANDBOXER_{key.upper()}_MODEL") or model
    return resolve_adapter(kind, model=resolved_model)


__all__ = [
    "AGENT_KINDS",
    "AgyAdapter",
    "CMD_DEFAULT_MODEL",
    "CmdAdapter",
    "CODEX_DEFAULT_MODEL",
    "CodexAdapter",
    "HeadlessAgentAdapter",
    "HeadlessAgentError",
    "HermesAdapter",
    "PHASE_DEFAULTS",
    "phase_adapter",
    "resolve_adapter",
]
