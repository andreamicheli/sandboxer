"""Narrow process adapter for Command Code's documented NDJSON interface."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence


class AdapterError(RuntimeError):
    """Raised when the provider process violates the adapter contract."""


@dataclass(frozen=True)
class CommandCodeResult:
    subtype: str
    model: str
    stop_reason: str | None
    turn_count: int
    usage: dict[str, int]
    duration_ms: int
    final_text: str
    reasoning_text: str | None
    reasoning_format: str
    event_types: tuple[str, ...]
    process_exit_code: int


class CommandCodeAdapter:
    """Execute one fail-closed headless Command Code call.

    The prototype deliberately rejects every tool event. A later integration
    must translate allowlisted requests into Sandboxer's Runner tool contract;
    it must never enable Command Code's native unrestricted tool execution.
    """

    def __init__(self, executable: Sequence[str] = ("/usr/local/bin/cmd",)) -> None:
        if not executable:
            raise ValueError("an explicit executable is required")
        self._executable = tuple(executable)

    def command(self, *, prompt: str, model: str, max_turns: int) -> tuple[str, ...]:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if "/" not in model:
            raise ValueError("model must be a full provider/model id")
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        return (
            *self._executable,
            "-p",
            prompt,
            "--model",
            model,
            "--max-turns",
            str(max_turns),
            "--output-format",
            "json",
            "--no-session",
            "--no-auto-update",
            "--no-skills",
            "--skip-onboarding",
            "--permission-mode",
            "dont-ask",
        )

    async def run(
        self,
        *,
        prompt: str,
        model: str,
        max_turns: int,
        cwd: Path,
        timeout_seconds: float,
        on_frame: Callable[[dict[str, Any]], None] | None = None,
    ) -> CommandCodeResult:
        if not cwd.is_dir():
            raise ValueError("cwd must be an existing disposable directory")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        process = await asyncio.create_subprocess_exec(
            *self.command(prompt=prompt, model=model, max_turns=max_turns),
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stderr_task = asyncio.create_task(process.stderr.read())
        frames: list[dict[str, Any]] = []
        try:
            await asyncio.wait_for(
                self._read_stdout(process, frames=frames, on_frame=on_frame),
                timeout_seconds,
            )
            await process.wait()
        except TimeoutError as error:
            await _terminate(process)
            await stderr_task
            raise AdapterError("command-code timeout") from error
        except AdapterError:
            await _terminate(process)
            await stderr_task
            raise
        stderr = (await stderr_task).decode(errors="replace")

        return self.parse_frames(
            frames=frames,
            stderr=stderr,
            process_exit_code=process.returncode,
            requested_model=model,
        )

    async def _read_stdout(
        self,
        process: asyncio.subprocess.Process,
        *,
        frames: list[dict[str, Any]],
        on_frame: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        assert process.stdout is not None
        line_number = 0
        while line := await process.stdout.readline():
            line_number += 1
            try:
                frame = json.loads(line.decode(errors="replace"))
            except json.JSONDecodeError as error:
                process.terminate()
                raise AdapterError(f"malformed NDJSON at line {line_number}") from error
            self._validate_envelope(frame, line_number)
            frames.append(frame)
            if on_frame is not None:
                on_frame(frame)
            if _is_native_tool_frame(frame):
                process.terminate()
                raise AdapterError("unexpected native tool call")

    @staticmethod
    def _validate_envelope(frame: object, line_number: int) -> None:
        if not isinstance(frame, dict) or frame.get("type") not in {"event", "result"}:
            raise AdapterError(f"unknown frame envelope at line {line_number}")

    def parse(
        self,
        *,
        stdout: str,
        stderr: str,
        process_exit_code: int,
        requested_model: str,
    ) -> CommandCodeResult:
        frames: list[dict[str, Any]] = []
        for line_number, line in enumerate(stdout.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as error:
                raise AdapterError(f"malformed NDJSON at line {line_number}") from error
            self._validate_envelope(frame, line_number)
            frames.append(frame)

        return self.parse_frames(
            frames=frames,
            stderr=stderr,
            process_exit_code=process_exit_code,
            requested_model=requested_model,
        )

    def parse_frames(
        self,
        *,
        frames: list[dict[str, Any]],
        stderr: str,
        process_exit_code: int,
        requested_model: str,
    ) -> CommandCodeResult:

        if not frames:
            raise AdapterError(f"empty NDJSON stream (exit={process_exit_code}, stderr={stderr[-500:]!r})")
        result_frames = [frame for frame in frames if frame.get("type") == "result"]
        if len(result_frames) != 1 or frames[-1].get("type") != "result":
            raise AdapterError("stream must end with exactly one result frame")

        event_types: list[str] = []
        observed_models: set[str] = set()
        structured_reasoning: list[str] = []
        turn_count = 0
        had_tool_calls = False
        for frame in frames[:-1]:
            event = frame.get("event")
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise AdapterError("event frame is missing event.type")
            event_type = event["type"]
            event_types.append(event_type)
            if event_type in {"tool_running", "tool_start", "tool_call", "tool_use"}:
                had_tool_calls = True
            if event_type == "thinking_end" and isinstance(event.get("text"), str):
                structured_reasoning.append(event["text"])
            if event_type in {"model_request_start", "model_request_end"}:
                observed = event.get("model")
                if isinstance(observed, str):
                    observed_models.add(observed)
            if event_type == "turn_end":
                turn_count = max(turn_count, _required_int(event, "turnNumber"))
                had_tool_calls = had_tool_calls or bool(event.get("hadToolCalls"))

        if had_tool_calls:
            raise AdapterError("unexpected native tool call")
        if observed_models and observed_models != {requested_model}:
            raise AdapterError(f"model mismatch: requested {requested_model}, observed {sorted(observed_models)}")

        result = result_frames[0]
        subtype = result.get("subtype")
        if subtype not in {"success", "error", "max_turns"}:
            raise AdapterError(f"unknown result subtype: {subtype!r}")
        expected_exit = {"success": 0, "max_turns": 8}
        if subtype in expected_exit and process_exit_code != expected_exit[subtype]:
            raise AdapterError(f"subtype {subtype} conflicts with exit {process_exit_code}")
        if subtype == "error" or process_exit_code not in {0, 8}:
            error_value = result.get("error") or stderr[-500:] or f"exit {process_exit_code}"
            raise AdapterError(f"command-code error: {error_value}")

        usage = result.get("usage")
        if not isinstance(usage, dict):
            raise AdapterError("result usage is missing")
        normalized_usage = {
            key: _required_int(usage, key)
            for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
        }
        final_text = result.get("finalText")
        if not isinstance(final_text, str):
            raise AdapterError("result finalText is missing")

        clean_text, reasoning_text, reasoning_format = _normalize_reasoning(
            final_text,
            structured_reasoning,
        )
        return CommandCodeResult(
            subtype=subtype,
            model=requested_model,
            stop_reason=result.get("stopReason") if isinstance(result.get("stopReason"), str) else None,
            turn_count=turn_count,
            usage=normalized_usage,
            duration_ms=_required_int(result, "durationMs"),
            final_text=clean_text,
            reasoning_text=reasoning_text,
            reasoning_format=reasoning_format,
            event_types=tuple(event_types),
            process_exit_code=process_exit_code,
        )


def _required_int(value: dict[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise AdapterError(f"{key} must be a non-negative integer")
    return item


THINK_BLOCK = re.compile(r"^\s*<think>(.*?)</think>\s*", re.DOTALL | re.IGNORECASE)


def _normalize_reasoning(
    final_text: str,
    structured_reasoning: list[str],
) -> tuple[str, str | None, str]:
    """Separate provider reasoning from the publishable final response."""

    if structured_reasoning:
        return final_text, "\n\n".join(structured_reasoning), "structured-events"
    tagged = THINK_BLOCK.match(final_text)
    if tagged:
        return final_text[tagged.end() :], tagged.group(1), "tagged-final-text"
    return final_text, None, "none"


def _is_native_tool_frame(frame: dict[str, Any]) -> bool:
    if frame.get("type") != "event" or not isinstance(frame.get("event"), dict):
        return False
    event = frame["event"]
    return event.get("type") in {"tool_running", "tool_start", "tool_call", "tool_use"} or (
        event.get("type") == "turn_end" and bool(event.get("hadToolCalls"))
    )


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        process.kill()
        await process.wait()
