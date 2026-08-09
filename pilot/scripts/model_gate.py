#!/usr/bin/env python3
"""Fail closed unless the repository is in its credential-free setup mode."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def model_connections_disabled() -> tuple[bool, str]:
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    mode = config.get("model_mode")
    provider_calls = config.get("allow_provider_calls")
    safe = mode == "disabled" and provider_calls is False
    return safe, f"model_mode={mode!r}, allow_provider_calls={provider_calls!r}"


def main() -> int:
    safe, detail = model_connections_disabled()
    print(f"{'PASS' if safe else 'FAIL'}  model connection gate: {detail}")
    if not safe:
        print("This setup command only runs with all real model connections disabled.", file=sys.stderr)
    return 0 if safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
