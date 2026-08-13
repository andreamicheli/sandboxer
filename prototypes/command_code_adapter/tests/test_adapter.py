from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from prototypes.command_code_adapter import AdapterError, CommandCodeAdapter


FAKE = Path(__file__).with_name("fake_cli.py")
MODEL = "example/model"


def adapter() -> CommandCodeAdapter:
    return CommandCodeAdapter((sys.executable, str(FAKE)))


def run(tmp_path: Path, mode: str = "success", timeout: float = 2):
    previous = os.environ.get("SANDBOXER_FAKE_CLI_MODE")
    os.environ["SANDBOXER_FAKE_CLI_MODE"] = mode
    try:
        return asyncio.run(
            adapter().run(
                prompt="test",
                model=MODEL,
                max_turns=1,
                cwd=tmp_path,
                timeout_seconds=timeout,
            )
        )
    finally:
        if previous is None:
            os.environ.pop("SANDBOXER_FAKE_CLI_MODE", None)
        else:
            os.environ["SANDBOXER_FAKE_CLI_MODE"] = previous


def test_success_is_parsed_incrementally(tmp_path: Path) -> None:
    result = run(tmp_path)
    assert result.subtype == "success"
    assert result.model == MODEL
    assert result.final_text == "ok"
    assert result.turn_count == 1
    assert result.reasoning_text is None
    assert result.reasoning_format == "none"
    assert result.usage == {
        "inputTokens": 100,
        "outputTokens": 5,
        "cacheReadTokens": 20,
        "cacheWriteTokens": 0,
    }


def test_command_is_fail_closed() -> None:
    command = adapter().command(prompt="test", model=MODEL, max_turns=2)
    assert "--no-session" in command
    assert "--no-auto-update" in command
    assert "--no-skills" in command
    assert "dont-ask" in command
    assert "--yolo" not in command
    assert "--auto-accept" not in command
    assert "--trust" not in command


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("malformed", "malformed NDJSON"),
        ("tool", "unexpected native tool call"),
        ("auth", "not authenticated"),
    ],
)
def test_failure_modes_are_rejected(tmp_path: Path, mode: str, message: str) -> None:
    with pytest.raises(AdapterError, match=message):
        run(tmp_path, mode)


def test_max_turns_is_a_structured_result(tmp_path: Path) -> None:
    result = run(tmp_path, "max_turns")
    assert result.subtype == "max_turns"
    assert result.process_exit_code == 8
    assert result.final_text == "partial"


def test_outer_timeout_terminates_process(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="timeout"):
        run(tmp_path, "timeout", timeout=0.05)


def test_full_model_id_is_required() -> None:
    with pytest.raises(ValueError, match="full provider/model id"):
        adapter().command(prompt="test", model="short-name", max_turns=1)


@pytest.mark.parametrize(
    ("mode", "reasoning_format"),
    [
        ("structured_reasoning", "structured-events"),
        ("tagged_reasoning", "tagged-final-text"),
    ],
)
def test_reasoning_is_normalized(tmp_path: Path, mode: str, reasoning_format: str) -> None:
    result = run(tmp_path, mode)
    assert result.final_text == "ok"
    assert result.reasoning_text == "private analysis"
    assert result.reasoning_format == reasoning_format


def test_frames_are_exposed_while_streaming(tmp_path: Path) -> None:
    observed: list[str] = []
    previous = os.environ.get("SANDBOXER_FAKE_CLI_MODE")
    os.environ["SANDBOXER_FAKE_CLI_MODE"] = "success"
    try:
        result = asyncio.run(
            adapter().run(
                prompt="test",
                model=MODEL,
                max_turns=1,
                cwd=tmp_path,
                timeout_seconds=2,
                on_frame=lambda frame: observed.append(frame["type"]),
            )
        )
    finally:
        if previous is None:
            os.environ.pop("SANDBOXER_FAKE_CLI_MODE", None)
        else:
            os.environ["SANDBOXER_FAKE_CLI_MODE"] = previous
    assert observed[-1] == "result"
    assert observed.count("event") == len(result.event_types)


def test_two_sessions_run_concurrently(tmp_path: Path) -> None:
    alpha_dir = tmp_path / "alpha"
    beta_dir = tmp_path / "beta"
    alpha_dir.mkdir()
    beta_dir.mkdir()

    async def concurrent_run():
        return await asyncio.gather(
            adapter().run(
                prompt="alpha",
                model="example/alpha",
                max_turns=1,
                cwd=alpha_dir,
                timeout_seconds=2,
            ),
            adapter().run(
                prompt="beta",
                model="example/beta",
                max_turns=1,
                cwd=beta_dir,
                timeout_seconds=2,
            ),
        )

    alpha, beta = asyncio.run(concurrent_run())
    assert alpha.model == "example/alpha"
    assert beta.model == "example/beta"
    assert alpha.final_text == beta.final_text == "ok"
