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
    OPENCODE_DEFAULT_MODEL,
    OpencodeAdapter,
    phase_adapter,
    resolve_adapter,
)

AGY_DEFAULT_MODEL = "gemini-3.7-flash-high"
OPENCODE_EXECUTABLE = "/home/ubuntu/.opencode/bin/opencode"


def test_resolve_adapter_returns_typed_adapters() -> None:
    assert isinstance(resolve_adapter("opencode"), OpencodeAdapter)
    assert isinstance(resolve_adapter("codex"), CodexAdapter)
    assert isinstance(resolve_adapter("cmd"), CmdAdapter)
    assert isinstance(resolve_adapter("agy"), AgyAdapter)
    assert isinstance(resolve_adapter("hermes"), HermesAdapter)
    assert isinstance(resolve_adapter("HERMES"), HermesAdapter)  # case-insensitive
    assert isinstance(resolve_adapter("CODEX"), CodexAdapter)  # case-insensitive
    assert isinstance(resolve_adapter("OpenCode"), OpencodeAdapter)  # case-insensitive


def test_resolve_adapter_rejects_unknown_kind() -> None:
    with pytest.raises(HeadlessAgentError) as exc:
        resolve_adapter("gemini")
    assert exc.value.reason_code == "AGENT_UNKNOWN"


def test_phase_adapter_defaults_and_env_override(monkeypatch) -> None:
    # All four editorial phases default to OpenCode (ox-alpha); codex/cmd/agy/
    # hermes remain opt-in via SANDBOXER_<PHASE>_AGENT.
    assert isinstance(phase_adapter("commentary"), OpencodeAdapter)
    assert isinstance(phase_adapter("arena"), OpencodeAdapter)
    assert isinstance(phase_adapter("intro"), OpencodeAdapter)
    assert isinstance(phase_adapter("report_narrative"), OpencodeAdapter)
    monkeypatch.setenv("SANDBOXER_COMMENTARY_AGENT", "cmd")
    assert isinstance(phase_adapter("commentary"), CmdAdapter)
    monkeypatch.setenv("SANDBOXER_ARENA_AGENT", "codex")
    assert isinstance(phase_adapter("arena"), CodexAdapter)
    monkeypatch.setenv("SANDBOXER_INTRO_AGENT", "agy")
    assert isinstance(phase_adapter("intro"), AgyAdapter)
    monkeypatch.setenv("SANDBOXER_REPORT_NARRATIVE_AGENT", "hermes")
    assert isinstance(phase_adapter("report_narrative"), HermesAdapter)


def test_opencode_adapter_builds_command_and_strips_stdout() -> None:
    adapter = OpencodeAdapter()
    assert adapter.model == OPENCODE_DEFAULT_MODEL
    with mock.patch("sandboxer_v0.agents._run") as run:
        run.return_value = type("P", (), {"returncode": 0, "stdout": "  Hello from OpenCode\n", "stderr": ""})()
        assert adapter.complete("ping") == "Hello from OpenCode"
    (command,), kwargs = run.call_args
    # Absolute binary path (NOT on PATH), run subcommand, -m <model>, positional prompt.
    assert command[0] == OPENCODE_EXECUTABLE
    assert command[1:3] == ["run", "-m"]
    assert command[3] == OPENCODE_DEFAULT_MODEL
    assert command[4] == "ping"
    assert kwargs["binary_kind"] == "OPENCODE"


def test_opencode_adapter_custom_model_and_executable() -> None:
    adapter = OpencodeAdapter(model="opencode/big-pickle", executable=("/opt/opencode",))
    with mock.patch("sandboxer_v0.agents._run") as run:
        run.return_value = type("P", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
        assert adapter.complete("hi") == "ok"
    (command,), _ = run.call_args
    assert command[:4] == ["/opt/opencode", "run", "-m", "opencode/big-pickle"]


def test_opencode_adapter_fails_closed() -> None:
    adapter = OpencodeAdapter()
    # Non-zero exit -> OPENCODE_FAILED with the stderr tail as detail.
    with mock.patch("sandboxer_v0.agents._run") as run:
        run.return_value = type("P", (), {"returncode": 2, "stdout": "", "stderr": "\nboom\n"})()
        with pytest.raises(HeadlessAgentError) as exc:
            adapter.complete("ping")
        assert exc.value.reason_code == "OPENCODE_FAILED"
        assert exc.value.detail == "boom"
    # Empty output -> OPENCODE_EMPTY.
    with mock.patch("sandboxer_v0.agents._run") as run:
        run.return_value = type("P", (), {"returncode": 0, "stdout": "   \n", "stderr": ""})()
        with pytest.raises(HeadlessAgentError) as exc:
            adapter.complete("ping")
        assert exc.value.reason_code == "OPENCODE_EMPTY"
    # Whitespace-only prompt never reaches the binary.
    with mock.patch("sandboxer_v0.agents._run") as run:
        with pytest.raises(HeadlessAgentError) as exc:
            adapter.complete("   ")
        assert exc.value.reason_code == "OPENCODE_EMPTY_PROMPT"
    assert run.call_count == 0


def test_opencode_adapter_timeout_surfaces_from_run() -> None:
    # Timeout handling lives in _run (binary_kind="OPENCODE"); the adapter
    # propagates its stable reason code untouched.
    adapter = OpencodeAdapter()
    with mock.patch("sandboxer_v0.agents._run") as run:
        run.side_effect = HeadlessAgentError("OPENCODE_TIMEOUT")
        with pytest.raises(HeadlessAgentError) as exc:
            adapter.complete("ping")
        assert exc.value.reason_code == "OPENCODE_TIMEOUT"


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
    # opencode defaults to ox-alpha; codex (ChatGPT) to Luna 5.6; cmd to Muse
    # Spark Contributor.
    assert OPENCODE_DEFAULT_MODEL == "opencode/x-preview-f-free"
    assert CODEX_DEFAULT_MODEL == "gpt-5.6-luna"
    assert CMD_DEFAULT_MODEL == "meta/muse-spark-1.2-contributor"
    assert OpencodeAdapter().model == OPENCODE_DEFAULT_MODEL
    assert OpencodeAdapter()._executable == (OPENCODE_EXECUTABLE,)
    assert CodexAdapter().model == CODEX_DEFAULT_MODEL
    assert CmdAdapter().model == CMD_DEFAULT_MODEL


def test_phase_adapter_default_model(monkeypatch) -> None:
    # Without env overrides every phase runs OpenCode with ox-alpha.
    for key in ("COMMENTARY", "ARENA", "INTRO", "REPORT_NARRATIVE"):
        monkeypatch.delenv(f"SANDBOXER_{key}_AGENT", raising=False)
        monkeypatch.delenv(f"SANDBOXER_{key}_MODEL", raising=False)
    for phase in ("commentary", "arena", "intro", "report_narrative"):
        adapter = phase_adapter(phase)
        assert isinstance(adapter, OpencodeAdapter)
        assert adapter.model == OPENCODE_DEFAULT_MODEL


def test_phase_adapter_model_env_override(monkeypatch) -> None:
    # Model env override applies within the phase's default agent kind.
    monkeypatch.setenv("SANDBOXER_COMMENTARY_MODEL", "opencode/big-pickle")
    adapter = phase_adapter("commentary")
    assert isinstance(adapter, OpencodeAdapter)
    assert adapter.model == "opencode/big-pickle"
    # Explicit model argument still wins over the env default-less path.
    monkeypatch.setenv("SANDBOXER_ARENA_MODEL", "poolside/laguna-s-2.1-free")
    arena = phase_adapter("arena")
    assert isinstance(arena, OpencodeAdapter)
    assert arena.model == "poolside/laguna-s-2.1-free"
    # Overriding the agent kind back to codex carries its own default model.
    monkeypatch.setenv("SANDBOXER_INTRO_AGENT", "codex")
    intro = phase_adapter("intro")
    assert isinstance(intro, CodexAdapter)
    assert intro.model == CODEX_DEFAULT_MODEL


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
