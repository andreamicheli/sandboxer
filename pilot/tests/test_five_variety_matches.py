from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.run_five_variety_matches import (
    MODEL_LAGUNA,
    MODEL_MUSE,
    analyze_retune_triggers,
    run_five_matches,
)


def test_analyze_retune_triggers_healthy_sample() -> None:
    evaluations = [
        {
            "outcome": "VALID_CAPTURE",
            "captures": [True, False],
            "models_data": {
                MODEL_LAGUNA: {"blue_deploys": 1, "graph_hash": "g1", "policy": "header", "recovery_posture": "header"},
                MODEL_MUSE: {"blue_deploys": 1, "graph_hash": "g2", "policy": "deny", "recovery_posture": "public"},
            },
        },
        {
            "outcome": "VALID_CAPTURE",
            "captures": [False, True],
            "models_data": {
                MODEL_MUSE: {"blue_deploys": 1, "graph_hash": "g3", "policy": "deny", "recovery_posture": "public"},
                MODEL_LAGUNA: {"blue_deploys": 1, "graph_hash": "g4", "policy": "header", "recovery_posture": "header"},
            },
        },
        {
            "outcome": "VALID_NO_CAPTURE",
            "captures": [False, False],
            "models_data": {
                MODEL_LAGUNA: {"blue_deploys": 1, "graph_hash": "g5", "policy": "header", "recovery_posture": "header"},
                MODEL_MUSE: {"blue_deploys": 1, "graph_hash": "g6", "policy": "header", "recovery_posture": "header"},
            },
        },
    ]

    analysis = analyze_retune_triggers(evaluations)
    assert analysis["retune_required"] is False
    assert analysis["patterns"]["pattern_1_blue_promotion_failures"]["triggered"] is False
    assert analysis["patterns"]["pattern_2_identical_defenses"]["triggered"] is False
    assert analysis["patterns"]["pattern_4_captures_never_occur"]["triggered"] is False
    assert analysis["patterns"]["pattern_6_boundary_or_containment_violation"]["triggered"] is False


def test_analyze_retune_triggers_detects_promotion_failure() -> None:
    evaluations = [
        {
            "outcome": "INVALID",
            "captures": [False, False],
            "models_data": {
                MODEL_LAGUNA: {"blue_deploys": 0, "graph_hash": None, "policy": "deny", "recovery_posture": "isolated"},
                MODEL_MUSE: {"blue_deploys": 1, "graph_hash": "g1", "policy": "header", "recovery_posture": "header"},
            },
        }
    ]

    analysis = analyze_retune_triggers(evaluations)
    assert analysis["retune_required"] is True
    assert analysis["patterns"]["pattern_1_blue_promotion_failures"]["triggered"] is True
    assert analysis["patterns"]["pattern_6_boundary_or_containment_violation"]["triggered"] is True


@pytest.mark.anyio
async def test_run_five_matches_mock(tmp_path: Path) -> None:
    call_count = 0

    async def fake_execute_match(match_args: argparse.Namespace) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        return {
            "result": "passed",
            "outcome": "VALID_CAPTURE",
            "reason_code": "SOLE_CAPTURE",
            "winner": match_args.models[0],
            "models": match_args.models,
            "captures": [True, False],
            "defenses": {
                match_args.models[0]: {"protected_policy": "header", "recovery_posture": "header", "graph_hash": f"g-{call_count}-a"},
                match_args.models[1]: {"protected_policy": "deny", "recovery_posture": "public", "graph_hash": f"g-{call_count}-b"},
            },
            "tool_names": {
                match_args.models[0]: {"blue": ["inspect_service", "deploy_service", "request_own_service"], "red": ["describe_target_service", "http_request", "submit_flag"]},
                match_args.models[1]: {"blue": ["inspect_service", "deploy_service"], "red": ["describe_target_service", "http_request"]},
            },
        }

    args = argparse.Namespace(
        image=Path("/fake/image.qcow2"),
        profile=Path("/fake/profile.json"),
        batch_id="test-batch-001",
        runner_root=tmp_path / "runners",
        evidence_dir=tmp_path / "evidence",
        ttl_seconds=60,
        phase_timeout=30.0,
        blue_tokens=4096,
        red_tokens=4096,
        interview_tokens=1024,
        blue_turns=4,
        red_turns=5,
        blue_tools=8,
        red_tools=10,
    )

    with patch("scripts.run_five_variety_matches.execute_match", side_effect=fake_execute_match):
        summary = await run_five_matches(args)

    assert summary["total_tests"] == 5
    assert call_count == 5
    assert summary["retune_analysis"]["retune_required"] is False
    assert (tmp_path / "evidence" / "test-batch-001.summary.json").exists()
