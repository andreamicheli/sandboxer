from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sandboxer_v0.blue_briefs import select_blue_briefs
from sandboxer_v0.command_code import CommandCodeError
from scripts.run_private_series import (
    MODEL_LAGUNA,
    MODEL_MUSE,
    determine_match_order,
    execute_private_series,
)


def test_determine_match_order_match_1_and_2() -> None:
    initial = (MODEL_LAGUNA, MODEL_MUSE)
    assert determine_match_order(1, initial, None, None) == (MODEL_LAGUNA, MODEL_MUSE)
    assert determine_match_order(2, initial, None, None) == (MODEL_MUSE, MODEL_LAGUNA)


def test_determine_match_order_match_3_loser_first() -> None:
    initial = (MODEL_LAGUNA, MODEL_MUSE)
    # If Laguna won match 2 (which was [MUSE, LAGUNA]), Muse lost -> Muse is first in match 3
    match_2_models = (MODEL_MUSE, MODEL_LAGUNA)
    assert determine_match_order(3, initial, MODEL_LAGUNA, match_2_models) == (MODEL_MUSE, MODEL_LAGUNA)

    # If Muse won match 2 (which was [MUSE, LAGUNA]), Laguna lost -> Laguna is first in match 3
    assert determine_match_order(3, initial, MODEL_MUSE, match_2_models) == (MODEL_LAGUNA, MODEL_MUSE)


def test_determine_match_order_invalid_number() -> None:
    initial = (MODEL_LAGUNA, MODEL_MUSE)
    with pytest.raises(ValueError, match="Invalid match number"):
        determine_match_order(4, initial, None, None)


@pytest.mark.anyio
async def test_execute_private_series_two_consecutive_wins(tmp_path: Path) -> None:
    call_args_list = []

    async def fake_execute_match(match_args: argparse.Namespace) -> dict[str, object]:
        call_args_list.append(match_args)
        # Laguna wins both match 1 and 2
        return {
            "result": "passed",
            "outcome": "VALID_CAPTURE",
            "reason_code": "SOLE_CAPTURE",
            "winner": MODEL_LAGUNA,
            "match_id": match_args.match_id,
            "models": match_args.models,
        }

    series_args = argparse.Namespace(
        image=Path("/fake/image.qcow2"),
        profile=Path("/fake/profile.json"),
        series_id="test-series-2-0",
        seed_prefix="test-seed",
        initial_order="laguna_first",
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

    with patch("scripts.run_private_series.execute_match", side_effect=fake_execute_match):
        summary = await execute_private_series(series_args)

    assert summary["series_winner"] == MODEL_LAGUNA
    assert summary["wins"] == {MODEL_LAGUNA: 2, MODEL_MUSE: 0}
    assert summary["matches_count"] == 2  # Match 3 was not needed!
    assert summary["publication_enabled"] is False
    assert summary["is_calibration"] is True
    assert summary["publication_eligible"] is False

    # Check match 1 and 2 models order
    assert call_args_list[0].models == [MODEL_LAGUNA, MODEL_MUSE]
    assert call_args_list[1].models == [MODEL_MUSE, MODEL_LAGUNA]

    # Verify telemetry file and result file were created
    assert (tmp_path / "evidence" / "test-series-2-0.telemetry.jsonl").exists()
    assert (tmp_path / "evidence" / "test-series-2-0.result.json").exists()


@pytest.mark.anyio
async def test_execute_private_series_three_matches_with_loser_order(tmp_path: Path) -> None:
    call_args_list = []

    async def fake_execute_match(match_args: argparse.Namespace) -> dict[str, object]:
        call_args_list.append(match_args)
        if len(call_args_list) == 1:
            # Match 1: Laguna wins
            return {"result": "passed", "outcome": "VALID_CAPTURE", "reason_code": "SOLE_CAPTURE", "winner": MODEL_LAGUNA}
        elif len(call_args_list) == 2:
            # Match 2: Muse wins (models were [MUSE, LAGUNA], Laguna lost)
            return {"result": "passed", "outcome": "VALID_CAPTURE", "reason_code": "SOLE_CAPTURE", "winner": MODEL_MUSE}
        else:
            # Match 3: Laguna wins
            return {"result": "passed", "outcome": "VALID_CAPTURE", "reason_code": "SOLE_CAPTURE", "winner": MODEL_LAGUNA}

    series_args = argparse.Namespace(
        image=Path("/fake/image.qcow2"),
        profile=Path("/fake/profile.json"),
        series_id="test-series-3-match",
        seed_prefix="test-seed-3",
        initial_order="laguna_first",
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

    with patch("scripts.run_private_series.execute_match", side_effect=fake_execute_match):
        summary = await execute_private_series(series_args)

    assert summary["series_winner"] == MODEL_LAGUNA
    assert summary["wins"] == {MODEL_LAGUNA: 2, MODEL_MUSE: 1}
    assert summary["matches_count"] == 3

    # Match 1: [LAGUNA, MUSE]
    assert call_args_list[0].models == [MODEL_LAGUNA, MODEL_MUSE]
    # Match 2: [MUSE, LAGUNA] (inverted) -> Muse won, Laguna lost
    assert call_args_list[1].models == [MODEL_MUSE, MODEL_LAGUNA]
    # Match 3: [LAGUNA, MUSE] -> Laguna (loser of Match 2) gets initial order!
    assert call_args_list[2].models == [MODEL_LAGUNA, MODEL_MUSE]


@pytest.mark.anyio
async def test_execute_private_series_handles_failures_without_crashing(tmp_path: Path) -> None:
    call_args_list = []

    async def fake_execute_match(match_args: argparse.Namespace) -> dict[str, object]:
        call_args_list.append(match_args)
        if len(call_args_list) == 1:
            raise CommandCodeError("AUDITOR_STOP_NATIVE_TOOL_REJECTED")
        return {"result": "passed", "outcome": "VALID_NO_CAPTURE", "reason_code": "NO_CAPTURE_AVAILABILITY", "winner": None}

    series_args = argparse.Namespace(
        image=Path("/fake/image.qcow2"),
        profile=Path("/fake/profile.json"),
        series_id="test-series-failure",
        seed_prefix="test-seed-f",
        initial_order="laguna_first",
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

    with patch("scripts.run_private_series.execute_match", side_effect=fake_execute_match):
        summary = await execute_private_series(series_args)

    assert summary["matches_count"] == 3
    # Match 1 failed and was recorded as INVALID with its reason code
    assert summary["matches"][0]["outcome"] == "INVALID"
    assert summary["matches"][0]["reason_code"] == "AUDITOR_STOP_NATIVE_TOOL_REJECTED"
