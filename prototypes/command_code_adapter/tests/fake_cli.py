#!/usr/bin/env python3
"""Deterministic fake of the Command Code NDJSON process boundary."""

from __future__ import annotations

import json
import os
import sys
import time


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value), flush=True)


mode = os.environ.get("SANDBOXER_FAKE_CLI_MODE", "success")
model = sys.argv[sys.argv.index("--model") + 1]

if mode == "timeout":
    time.sleep(5)
elif mode == "malformed":
    print("{not-json", flush=True)
elif mode == "auth":
    emit({"type": "result", "subtype": "error", "usage": {}, "durationMs": 1, "finalText": "", "error": "not authenticated"})
    raise SystemExit(3)
else:
    emit({"type": "event", "event": {"type": "run_start", "sessionId": "fake"}})
    emit({"type": "event", "event": {"type": "turn_start", "turnNumber": 1}})
    emit({"type": "event", "event": {"type": "model_request_start", "model": model}})
    if mode == "structured_reasoning":
        emit({"type": "event", "event": {"type": "thinking_end", "text": "private analysis"}})
    if mode == "tool":
        emit({"type": "event", "event": {"type": "tool_running", "toolName": "shell"}})
    emit({"type": "event", "event": {"type": "model_request_end", "model": model}})
    emit({"type": "event", "event": {"type": "turn_end", "turnNumber": 1, "hadToolCalls": mode == "tool"}})
    subtype = "max_turns" if mode == "max_turns" else "success"
    emit(
        {
            "type": "result",
            "subtype": subtype,
            "stopReason": "max_turns" if mode == "max_turns" else "end_turn",
            "usage": {"inputTokens": 100, "outputTokens": 5, "cacheReadTokens": 20, "cacheWriteTokens": 0},
            "durationMs": 25,
            "finalText": (
                "partial"
                if mode == "max_turns"
                else "<think>private analysis</think>ok"
                if mode == "tagged_reasoning"
                else "ok"
            ),
        }
    )
    raise SystemExit(8 if mode == "max_turns" else 0)
