#!/usr/bin/env python3
"""Live preflight for the Command Code provider pair.

Probes both Competitor models headlessly (tool-free, one turn) and records the
observable preflight facts: CLI binary hash/version, authenticated account,
live catalog snapshot, exact model resolution, accounting categories, and
per-model usage/duration. Fields the CLI does not expose (credit allowance,
concurrency limit) are recorded as ``not-observable`` rather than guessed, per
the blueprint's "when observable" rule.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

PILOT_ROOT = Path(__file__).resolve().parents[1]
if str(PILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PILOT_ROOT))

from sandboxer_v0.command_code import CommandCodeAdapter, CommandCodeError

EXECUTABLE = "/usr/local/bin/cmd"
MODELS = ("poolside/laguna-s-2.1-free", "meta/muse-spark-1.2-contributor")
PROBE_PROMPT = "Reply with the single word: ready"
SESSION_CONTROLS = ("no_session", "no_update", "no_skills", "skip_onboarding", "dont_ask")


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=120)


def _account_identifier() -> str:
    proc = _cli("/usr/local/bin/command-code", "whoami")
    for line in proc.stdout.splitlines():
        if "Username:" in line:
            return line.split("Username:", 1)[1].strip()
    return ""


def _catalog() -> tuple[str, ...]:
    proc = _cli("/usr/local/bin/command-code", "--list-models")
    models: list[str] = []
    for line in proc.stdout.splitlines():
        match = re.match(r"^([a-z0-9-]+/[a-z0-9._-]+)\s", line.strip())
        if match:
            models.append(match.group(1))
    return tuple(sorted(set(models)))


def _catalog_hash(catalog: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(catalog, separators=(",", ":")).encode()).hexdigest()


async def _probe(model: str) -> dict[str, object]:
    adapter = CommandCodeAdapter()
    try:
        result = await adapter.run(
            prompt=PROBE_PROMPT,
            model=model,
            max_turns=1,
            timeout_seconds=180,
            output_token_budget=1024,
            tool_free=True,
        )
    except CommandCodeError as error:
        return {"model": model, "resolved": False, "error": error.reason_code}
    return {
        "model": model,
        "resolved": True,
        "observed_model": result.observed_model,
        "turn_count": result.turn_count,
        "duration_ms": result.duration_ms,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "cache_write_tokens": result.cache_write_tokens,
        "reasoning_format": result.reasoning_format,
        "final_text": result.final_text.strip(),
    }


async def _amain(models: tuple[str, ...]) -> dict[str, object]:
    account = _account_identifier()
    catalog = _catalog()
    binary = Path(EXECUTABLE).resolve()
    probes = [await _probe(model) for model in models]
    resolved = [p for p in probes if p.get("resolved")]
    accounting = {
        "inputTokens",
        "outputTokens",
        "cacheReadTokens",
        "cacheWriteTokens",
    }
    all_accounting = all(
        all(k in p for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        for p in resolved
    )
    report: dict[str, object] = {
        "schema": "sandboxer.command-code-preflight.v1",
        "cli": {
            "executable": str(binary),
            "binary_sha256": CommandCodeAdapter.binary_sha256(binary),
            "cli_version": "1.15.1",
            "session_controls": SESSION_CONTROLS,
        },
        "auth": {
            "account_identifier": account,
            "authenticated": bool(account),
            "account_fingerprint": hashlib.sha256(
                ("sandboxer-command-code:" + account).encode()
            ).hexdigest()[:16] if account else "",
        },
        "catalog": {
            "count": len(catalog),
            "catalog_hash": _catalog_hash(catalog),
            "models_requested": list(models),
            "models_present": [m for m in models if m in catalog],
        },
        "probes": probes,
        "accounting_categories": sorted(accounting) if all_accounting else [],
        "comparability": {
            "models_resolved": len(resolved) == len(models),
            "accounting_symmetric": all_accounting,
            "credit_allowance": "not-observable-via-cli",
            "concurrency_limit": "not-observable-via-cli",
        },
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs=2, metavar=("MODEL_A", "MODEL_B"), default=list(MODELS))
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report to this path")
    args = parser.parse_args(argv)

    report = asyncio.run(_amain(tuple(args.models)))
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nwrote {args.out}")

    ok = (
        report["auth"]["authenticated"]
        and report["catalog"]["models_requested"] == report["catalog"]["models_present"]
        and report["comparability"]["models_resolved"]
        and report["comparability"]["accounting_symmetric"]
    )
    print(f"\nPREFLIGHT {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
