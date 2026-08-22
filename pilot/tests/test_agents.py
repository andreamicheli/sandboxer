"""Tests for sandboxer_v0.agents (headless coding-agent adapters)."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from sandboxer_v0.agents import (
    AgyAdapter,
    CMD_DEFAULT_MODEL,
    CODEX_DEFAULT_MODEL,
    CmdAdapter,
    CodexAdapter,
    HeadlessAgentError,
    HermesAdapter,
    phase_adapter,
    resolve_adapter,
)

AGY_DEFAULT_MODEL = "gemini-3.7-flash-high"


def test_resolve_adapter_returns_typed_adapters() -> None:
    assert isinstance(resolve_adapter("codex"), CodexAdapter)
    assert isinstance(resolve_adapter("cmd"), CmdAdapter)
    assert isinstance(resolve_adapter("agy"), AgyAdapter)
    assert isinstance(resolve_adapter("hermes"), HermesAdapter)
    assert isinstance(resolve_adapter("HERMES"), HermesAdapter)  # case-insensitive
    assert isinstance(resolve_adapter("CODEX"), CodexAdapter)  # case-insensitive


def test_resolve_adapter_rejects_unknown_kind() -> None:
    with pytest.raises(HeadlessAgentError) as exc:
        resolve_adapter("gemini")
    assert exc.value.reason_code == "AGENT_UNKNOWN"


def test_phase_adapter_defaults_and_env_override(monkeypatch) -> None:
    # Prose phases default to agy (Gemini 3.7); arena to cmd; commentary to codex.
    assert isinstance(phase_adapter("commentary"), CodexAdapter)
    assert isinstance(phase_adapter("arena"), CmdAdapter)
    assert isinstance(phase_adapter("intro"), AgyAdapter)
    assert isinstance(phase_adapter("report_narrative"), AgyAdapter)
    monkeypatch.setenv("SANDBOXER_COMMENTARY_AGENT", "cmd")
    assert isinstance(phase_adapter("commentary"), CmdAdapter)
    monkeypatch.setenv("SANDBOXER_ARENA_AGENT", "codex")
    assert isinstance(phase_adapter("arena"), CodexAdapter)
    monkeypatch.setenv("SANDBOXER_INTRO_AGENT", "codex")
    assert isinstance(phase_adapter("intro"), CodexAdapter)


def test_hermes_adapter_builds_command_and_parses_stdout() -> None:
    adapter = HermesAdapter(model="orcarouter/free")
    with mock.patch("sandboxer_v0.agents._run") as run:
        run.return_value = type("P", (), {"returncode": 0, "stdout": "Hello from Hermes\n", "stderr": ""})()
        assert adapter.complete("ping") == "Hello from Hermes"
    (command,), kwargs = run.call_args
    assert command[:2] == ["hermes", "chat"]
    assert "-m" in command and command[command.index("-m") + 1] == "orcarouter/free"
    assert kwargs["binary_kind"] == "HERMES"


def test_hermes_adapter_fails_closed() -> None:
    adapter = HermesAdapter(retries=0)
    with mock.patch("sandboxer_v0.agents._run") as run:
        run.side_effect = HeadlessAgentError("HERMES_FAILED", detail="boom")
        with pytest.raises(HeadlessAgentError) as exc:
            adapter.complete("ping")
        assert exc.value.reason_code == "HERMES_FAILED"
    with pytest.raises(HeadlessAgentError) as exc:
        adapter.complete("   ")
    assert exc.value.reason_code == "HERMES_EMPTY_PROMPT"


def test_hermes_adapter_retries_transient_failures() -> None:
    adapter = HermesAdapter(retries=2, retry_base_seconds=0.001)
    with mock.patch("sandboxer_v0.agents._run") as run:
        run.side_effect = [
            HeadlessAgentError("HERMES_TIMEOUT"),
            type("P", (), {"returncode": 0, "stdout": "recovered\n", "stderr": ""})(),
        ]
        assert adapter.complete("ping") == "recovered"
    assert run.call_count == 2


def test_hermes_adapter_exhausts_retries() -> None:
    adapter = HermesAdapter(retries=1, retry_base_seconds=0.001)
    with mock.patch("sandboxer_v0.agents._run") as run:
        run.side_effect = HeadlessAgentError("HERMES_TIMEOUT")
        with pytest.raises(HeadlessAgentError) as exc:
            adapter.complete("ping")
        assert exc.value.reason_code == "HERMES_TIMEOUT"
    assert run.call_count == 2


def test_default_models_are_explicit() -> None:
    # codex (ChatGPT) defaults to Luna 5.6; cmd defaults to Muse Spark Contributor.
    assert CODEX_DEFAULT_MODEL == "gpt-5.6-luna"
    assert CMD_DEFAULT_MODEL == "meta/muse-spark-1.2-contributor"
    assert CodexAdapter().model == CODEX_DEFAULT_MODEL
    assert CmdAdapter().model == CMD_DEFAULT_MODEL


def test_phase_adapter_default_model(monkeypatch) -> None:
    # Without env overrides the phase default agent carries its default model.
    monkeypatch.delenv("SANDBOXER_COMMENTARY_MODEL", raising=False)
    adapter = phase_adapter("commentary")
    assert isinstance(adapter, CodexAdapter)
    assert adapter.model == CODEX_DEFAULT_MODEL
    arena = phase_adapter("arena")
    assert isinstance(arena, CmdAdapter)
    assert arena.model == CMD_DEFAULT_MODEL
    # intro and report_narrative default to agy with Gemini 3.7.
    intro = phase_adapter("intro")
    assert isinstance(intro, AgyAdapter)
    assert intro.model == AGY_DEFAULT_MODEL
    narrative = phase_adapter("report_narrative")
    assert isinstance(narrative, AgyAdapter)
    assert narrative.model == AGY_DEFAULT_MODEL


def test_phase_adapter_model_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SANDBOXER_COMMENTARY_MODEL", "gpt-5.5")
    adapter = phase_adapter("commentary")
    assert isinstance(adapter, CodexAdapter)
    assert adapter.model == "gpt-5.5"
    # Explicit model argument still wins over the env default-less path.
    monkeypatch.setenv("SANDBOXER_ARENA_MODEL", "poolside/laguna-s-2.1-free")
    arena = phase_adapter("arena")
    assert isinstance(arena, CmdAdapter)
    assert arena.model == "poolside/laguna-s-2.1-free"


def test_cmd_final_text_parses_success_and_strips_think() -> None:
    stream = "\n".join(
        [
            json.dumps({"type": "event", "event": {"type": "turn_end"}}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "finalText": "<think>plan</think>\nThe drafted text.",
                    "usage": {},
                }
            ),
        ]
    )
    assert CmdAdapter._final_text(stream) == "The drafted text."


def test_cmd_final_text_maps_error_subtypes() -> None:
    stream = json.dumps(
        {"type": "result", "subtype": "error", "error": "authentication required"}
    )
    with pytest.raises(HeadlessAgentError) as exc:
        CmdAdapter._final_text(stream)
    assert exc.value.reason_code == "CMD_AUTH_REQUIRED"


def test_cmd_final_text_rejects_empty_stream() -> None:
    with pytest.raises(HeadlessAgentError) as exc:
        CmdAdapter._final_text('{"type":"event","event":{"type":"turn_end"}}')
    assert exc.value.reason_code == "CMD_STREAM_EMPTY"
