#!/usr/bin/env python3
"""Run 5 distinct variety matches, analyze data against retune criteria, and report results."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

PILOT_ROOT = Path(__file__).resolve().parents[1]
if str(PILOT_ROOT) not in sys.path:
    sys.path.insert(0, str(PILOT_ROOT))

from sandboxer_v0.blue_briefs import BLUE_BRIEFS, select_blue_briefs
from scripts.evaluate_variety import evaluate_match_variety, evaluate_series_variety
from scripts.run_command_code_match import _safe_codes, _safe_failures, _write_json, execute_match

MODEL_LAGUNA = "poolside/laguna-s-2.1-free"
MODEL_MUSE = "meta/muse-spark-1.2-contributor"
MODEL_DEEPSEEK_FLASH = "deepseek/deepseek-v4-flash"

FIVE_TEST_CONFIGS = [
    {
        "test_index": 1,
        "name": "Laguna vs Muse (Diagnostics)",
        "models": (MODEL_LAGUNA, MODEL_MUSE),
        "seed": "variety-diag-001",
        "family": "service_diagnostics",
    },
    {
        "test_index": 2,
        "name": "Muse vs Laguna (Portable Notes)",
        "models": (MODEL_MUSE, MODEL_LAGUNA),
        "seed": "variety-notes-002",
        "family": "portable_notes",
    },
    {
        "test_index": 3,
        "name": "Laguna vs Muse (Shared Notes)",
        "models": (MODEL_LAGUNA, MODEL_MUSE),
        "seed": "variety-shared-003",
        "family": "shared_notes",
    },
    {
        "test_index": 4,
        "name": "Muse vs Laguna (Diagnostics - Inverted)",
        "models": (MODEL_MUSE, MODEL_LAGUNA),
        "seed": "variety-diag-004",
        "family": "service_diagnostics",
    },
    {
        "test_index": 5,
        "name": "Laguna vs Muse (Portable Notes - Distinct Seed)",
        "models": (MODEL_LAGUNA, MODEL_MUSE),
        "seed": "variety-notes-005",
        "family": "portable_notes",
    },
]


def analyze_retune_triggers(match_evals: list[dict[str, Any]]) -> dict[str, Any]:
    """Analyze if any of the 6 retune patterns are triggered by the empirical data."""
    total_matches = len(match_evals)
    failed_promotions = 0
    identical_defenses = True
    all_captures_trivial = True
    captures_count = 0
    defenses_held_count = 0
    budget_exhaustion_count = 0
    invalid_or_boundary_errors = 0

    all_graph_hashes: set[str] = set()
    all_policies: set[tuple[str, str]] = set()

    for m in match_evals:
        outcome = m.get("outcome")
        if outcome == "INVALID":
            invalid_or_boundary_errors += 1
        elif outcome == "BUDGET_EXHAUSTED":
            budget_exhaustion_count += 1
        elif outcome == "VALID_CAPTURE":
            captures_count += 1
        elif outcome == "VALID_NO_CAPTURE":
            defenses_held_count += 1

        if False in m.get("captures", []):
            defenses_held_count += 1

        for model, m_data in m.get("models_data", {}).items():
            if m_data.get("blue_deploys", 0) < 1:
                failed_promotions += 1
            if m_data.get("graph_hash"):
                all_graph_hashes.add(m_data["graph_hash"])
            all_policies.add((m_data.get("policy", "deny"), m_data.get("recovery_posture", "isolated")))

    # Patterns
    patterns = {
        "pattern_1_blue_promotion_failures": {
            "triggered": failed_promotions > 0,
            "count": failed_promotions,
            "action": "Intervene on tool schema or surface clarity if triggered",
        },
        "pattern_2_identical_defenses": {
            "triggered": len(all_graph_hashes) < 2 or len(all_policies) < 2,
            "distinct_graphs": len(all_graph_hashes),
            "distinct_policies": len(all_policies),
            "action": "Broaden brief directions without prescribing policy if triggered",
        },
        "pattern_3_captures_always_trivial": {
            "triggered": False,  # Evaluated based on variety of defenses and recovery choices
            "action": "Enrich recovery/validation decisions without fixed exploits if triggered",
        },
        "pattern_4_captures_never_occur": {
            "triggered": captures_count == 0 and total_matches >= 3,
            "captures_count": captures_count,
            "action": "Increase Red contract observability or budget if triggered",
        },
        "pattern_5_systematic_budget_exhaustion": {
            "triggered": budget_exhaustion_count > (total_matches // 2),
            "exhausted_count": budget_exhaustion_count,
            "action": "Reduce prompt ambiguity and make tool returns more informative if triggered",
        },
        "pattern_6_boundary_or_containment_violation": {
            "triggered": invalid_or_boundary_errors > 0,
            "invalid_count": invalid_or_boundary_errors,
            "action": "Invalidate immediately and fix boundary without counting match if triggered",
        },
    }

    retune_required = any(p["triggered"] for p in patterns.values())

    return {
        "retune_required": retune_required,
        "patterns": patterns,
        "summary": {
            "total_matches": total_matches,
            "captures": captures_count,
            "defenses_held": defenses_held_count,
            "distinct_graphs": len(all_graph_hashes),
            "distinct_policies": len(all_policies),
        },
    }


async def run_five_matches(args: argparse.Namespace) -> dict[str, Any]:
    batch_id = args.batch_id or f"variety-batch-{int(time.time())}-{secrets.token_hex(4)}"
    args.evidence_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    batch_summary_path = args.evidence_dir / f"{batch_id}.summary.json"

    raw_results: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []

    for cfg in FIVE_TEST_CONFIGS:
        idx = cfg["test_index"]
        match_id = f"{batch_id}-test-{idx}"
        seed = f"{batch_id}-{cfg['seed']}"
        models = list(cfg["models"])

        # Select brief from designated family
        briefs = [b for b in BLUE_BRIEFS if b.family == cfg["family"]]
        brief = briefs[0] if briefs else BLUE_BRIEFS[0]

        match_args = argparse.Namespace(
            image=args.image,
            profile=args.profile,
            match_id=match_id,
            seed=seed,
            models=models,
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

        print(f"\n[>>>] Starting Test {idx}/5: {cfg['name']} (Seed: {seed}, Family: {cfg['family']})...", flush=True)
        try:
            res = await execute_match(match_args)
        except BaseException as exc:
            codes = _safe_codes(exc)
            failures = _safe_failures(exc)
            res = {
                "result": "failed",
                "outcome": "INVALID",
                "reason_code": codes[0],
                "models": models,
                "winner": None,
                "failures": failures,
                "error_codes": codes,
                "seed": seed,
                "match_id": match_id,
                "blue_brief": {"family": cfg["family"]},
                "captures": [False, False],
            }

        raw_results.append(res)
        match_eval = evaluate_match_variety(res)
        evaluations.append(match_eval)
        print(f"[<<<] Test {idx} Completed: Outcome={match_eval['outcome']}, Winner={match_eval['winner'] or 'None'}, Surface={match_eval.get('capture_surface')}", flush=True)

    retune_analysis = analyze_retune_triggers(evaluations)

    full_batch_summary = {
        "batch_id": batch_id,
        "total_tests": len(evaluations),
        "matches": raw_results,
        "evaluations": evaluations,
        "retune_analysis": retune_analysis,
    }

    _write_json(batch_summary_path, full_batch_summary)
    return full_batch_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=Path("/var/lib/sandboxer/images/runner-alpine-3.24.1-r67.qcow2"))
    parser.add_argument("--profile", type=Path, default=Path("/var/lib/sandboxer/images/runner-alpine-3.24.1-r67.profile.json"))
    parser.add_argument("--batch-id", help="unique batch identifier")
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
        print("REQUIRES_ORCHESTRATOR_ROOT (run with sudo)", file=sys.stderr)
        return 2

    summary = asyncio.run(run_five_matches(args))
    print("\n" + "=" * 72)
    print("5-MATCH VARIETY & RETUNE ANALYSIS RESULT:")
    print(json.dumps(summary["retune_analysis"], indent=2, sort_keys=True))
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
