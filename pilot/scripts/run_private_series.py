#!/usr/bin/env python3
"""Execute a private, non-publishable Best-of-3 Series between Laguna and Muse Spark Contributor.

Protocol:
- Models: poolside/laguna-s-2.1-free (Laguna) and meta/muse-spark-1.2-contributor (Muse Spark Contributor)
- Provider: Command Code (headless CLI via non-IP MCP bridge)
- Best-of-3 series:
  * Match 1: Random initial order (alpha/beta)
  * Match 2: Inverted initial order
  * Match 3 (if needed): Initial order starts with the loser of Match 2
- Distinct seed and Blue Brief family for each match
- Symmetric phase budgets for both models:
  * Blue: 4 turns, 4,096 tokens, 8 tools
  * Interview: 1,024 tokens, 0 tools
  * Red: 5 turns, 4,096 tokens, 10 tools
- No retry on failure: a failure is a recorded data point
- Safe auditor monitoring only (no reasoning traces, flags, or secret tokens exposed)
- All evidence bundles are marked private and non-publishable
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

PILOT_ROOT = Path(__file__).resolve().parents[1]
if str(PILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PILOT_ROOT))

from sandboxer_v0.blue_briefs import BLUE_BRIEFS, select_blue_briefs
from sandboxer_v0.series import MatchOutcome, resolve_match_outcome
from scripts.run_command_code_match import (
    _runner_name,
    _safe_codes,
    _safe_failures,
    _sha256,
    _write_json,
    execute_match,
)

MODEL_LAGUNA = "poolside/laguna-s-2.1-free"
MODEL_MUSE = "meta/muse-spark-1.2-contributor"
SERIES_MODELS = (MODEL_LAGUNA, MODEL_MUSE)


def determine_match_order(
    match_number: int,
    initial_order: tuple[str, str],
    match_2_winner: str | None,
    match_2_models: tuple[str, str] | None,
) -> tuple[str, str]:
    """Determine model order for matches 1, 2, and 3."""
    if match_number == 1:
        return initial_order
    elif match_number == 2:
        return (initial_order[1], initial_order[0])
    elif match_number == 3:
        if match_2_winner and match_2_models:
            loser = match_2_models[1] if match_2_winner == match_2_models[0] else match_2_models[0]
            winner = match_2_winner
            return (loser, winner)
        # In case of tie in match 2, revert to initial order
        return initial_order
    raise ValueError(f"Invalid match number: {match_number}")


async def execute_private_series(args: argparse.Namespace) -> dict[str, Any]:
    """Orchestrate the private best-of-3 series."""
    series_id = args.series_id or f"private-series-{int(time.time())}-{secrets.token_hex(4)}"
    seed_prefix = args.seed_prefix or f"seed-{series_id}"
    args.evidence_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    series_telemetry_path = args.evidence_dir / f"{series_id}.telemetry.jsonl"
    series_result_path = args.evidence_dir / f"{series_id}.result.json"

    descriptor = os.open(series_telemetry_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    series_telemetry = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)

    def emit_series(kind: str, **fields: Any) -> None:
        payload = {"kind": kind, "series_id": series_id, "monotonic_ns": time.monotonic_ns(), **fields}
        series_telemetry.write(json.dumps(payload, sort_keys=True) + "\n")

    # Match 1: Random initial order
    if args.initial_order == "laguna_first":
        initial_order = (MODEL_LAGUNA, MODEL_MUSE)
    elif args.initial_order == "muse_first":
        initial_order = (MODEL_MUSE, MODEL_LAGUNA)
    else:
        initial_order = secrets.choice([(MODEL_LAGUNA, MODEL_MUSE), (MODEL_MUSE, MODEL_LAGUNA)])

    emit_series(
        "series_started",
        protocol="best_of_3_private",
        models=SERIES_MODELS,
        initial_order=list(initial_order),
        publication_enabled=False,
        is_calibration=True,
        budgets={
            "blue": {"turns": args.blue_turns, "tokens": args.blue_tokens, "tools": args.blue_tools},
            "interview": {"turns": 1, "tokens": args.interview_tokens, "tools": 0},
            "red": {"turns": args.red_turns, "tokens": args.red_tokens, "tools": args.red_tools},
        },
    )

    wins = {MODEL_LAGUNA: 0, MODEL_MUSE: 0}
    matches_executed: list[dict[str, Any]] = []
    match_2_winner: str | None = None
    match_2_models: tuple[str, str] | None = None
    distinct_families_used: set[str] = set()

    for match_num in range(1, 4):
        # Check termination condition
        if wins[MODEL_LAGUNA] >= 2 or wins[MODEL_MUSE] >= 2:
            break

        current_order = determine_match_order(match_num, initial_order, match_2_winner, match_2_models)
        match_id = f"{series_id}-m{match_num}"
        match_seed = f"{seed_prefix}-m{match_num}"

        # Choose a brief with a distinct family if possible
        candidate_briefs = select_blue_briefs(match_seed, count=len(BLUE_BRIEFS))
        selected_brief = next(
            (b for b in candidate_briefs if b.family not in distinct_families_used),
            candidate_briefs[0],
        )
        distinct_families_used.add(selected_brief.family)

        match_args = argparse.Namespace(
            image=args.image,
            profile=args.profile,
            match_id=match_id,
            seed=match_seed,
            models=list(current_order),
            runner_root=args.runner_root,
            evidence_dir=args.evidence_dir,
            ttl_seconds=args.ttl_seconds,
            phase_timeout=args.phase_timeout,
            blue_tokens=args.blue_tokens,
            red_tokens=args.red_tokens,
            interview_tokens=args.interview_tokens,
            blue_turns=args.blue_turns,
            red_turns=args.red_turns,
            blue_tools=args.blue_tools,
            red_tools=args.red_tools,
        )

        emit_series(
            "series_match_initiated",
            match_number=match_num,
            match_id=match_id,
            seed=match_seed,
            models=list(current_order),
            blue_brief_family=selected_brief.family,
        )

        match_result: dict[str, Any]
        try:
            match_result = await execute_match(match_args)
            winner = match_result.get("winner")
            outcome = match_result.get("outcome", "VALID_NO_CAPTURE")
            reason_code = match_result.get("reason_code", "COMPLETED")
        except BaseException as exc:
            codes = _safe_codes(exc)
            failures = _safe_failures(exc)
            winner = None
            outcome = MatchOutcome.INVALID.value
            reason_code = codes[0]
            match_result = {
                "result": "failed",
                "outcome": outcome,
                "reason_code": reason_code,
                "models": list(current_order),
                "winner": None,
                "failures": failures,
                "error_codes": codes,
                "seed": match_seed,
                "match_id": match_id,
            }

        matches_executed.append(match_result)
        if winner in wins:
            wins[winner] += 1

        if match_num == 2:
            match_2_winner = winner
            match_2_models = current_order

        emit_series(
            "series_match_completed",
            match_number=match_num,
            match_id=match_id,
            winner=winner,
            outcome=outcome,
            reason_code=reason_code,
            current_standings=dict(wins),
        )

    # Determine series winner
    if wins[MODEL_LAGUNA] > wins[MODEL_MUSE]:
        series_winner = MODEL_LAGUNA
    elif wins[MODEL_MUSE] > wins[MODEL_LAGUNA]:
        series_winner = MODEL_MUSE
    else:
        series_winner = None

    series_summary = {
        "schema_version": "sandboxer.private-series-result.v1",
        "series_id": series_id,
        "protocol": "best_of_3_private",
        "publication_enabled": False,
        "is_calibration": True,
        "publication_eligible": False,
        "models": list(SERIES_MODELS),
        "initial_order": list(initial_order),
        "wins": wins,
        "series_winner": series_winner,
        "matches_count": len(matches_executed),
        "matches": matches_executed,
        "telemetry_path": str(series_telemetry_path),
        "evidence_dir": str(args.evidence_dir),
    }

    emit_series("series_finished", winner=series_winner, wins=wins, matches_count=len(matches_executed))
    series_telemetry.close()
    _write_json(series_result_path, series_summary)
    return series_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=Path("/var/lib/sandboxer/images/runner-alpine-3.24.1-r67.qcow2"))
    parser.add_argument("--profile", type=Path, default=Path("/var/lib/sandboxer/images/runner-alpine-3.24.1-r67.profile.json"))
    parser.add_argument("--series-id", help="unique series identifier")
    parser.add_argument("--seed-prefix", help="deterministic seed prefix for match seeds")
    parser.add_argument("--initial-order", choices=["random", "laguna_first", "muse_first"], default="random")
    parser.add_argument("--runner-root", type=Path, default=Path("/var/lib/sandboxer/runners"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("/var/lib/sandboxer/evidence"))
    parser.add_argument("--ttl-seconds", type=int, default=600)
    parser.add_argument("--phase-timeout", type=float, default=180.0)
    parser.add_argument("--blue-tokens", type=int, default=4096)
    parser.add_argument("--red-tokens", type=int, default=4096)
    parser.add_argument("--interview-tokens", type=int, default=1024)
    parser.add_argument("--blue-turns", type=int, default=4)
    parser.add_argument("--red-turns", type=int, default=5)
    parser.add_argument("--blue-tools", type=int, default=8)
    parser.add_argument("--red-tools", type=int, default=10)
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        print("SERIES_REQUIRES_ORCHESTRATOR_ROOT (run with sudo)", file=sys.stderr)
        return 2

    try:
        summary = asyncio.run(execute_private_series(args))
    except BaseException as error:
        codes = _safe_codes(error)
        print(f"SERIES_FAILED: {','.join(codes)}", file=sys.stderr)
        return 2

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
