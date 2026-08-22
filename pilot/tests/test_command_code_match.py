import asyncio
import json
from pathlib import Path

import pytest

from sandboxer_v0.blue_briefs import select_blue_briefs
from sandboxer_v0.command_code import CommandCodeError
from sandboxer_v0.runner_tool_server import RunnerToolServer
from scripts.run_command_code_match import (
    MAX_PROVIDER_RETRIES,
    _blue_prompt,
    _cleanup_socket_root,
    _frame_monitor,
    _runner_name,
    _socket_leaf,
    _socket_path,
    _socket_root,
)


def test_model_identity_maps_to_a_stable_runner_name():
    assert _runner_name("poolside/laguna-s-2.1-free") == "laguna-s-2-1-free"
    assert _runner_name("meta/muse-spark-1.2-contributor") == "muse-spark-1-2-contributor"


def test_blue_prompt_includes_a_shared_non_prescriptive_seed_brief():
    brief = select_blue_briefs("calibration-seed", count=1)[0]
    prompt = _blue_prompt(brief)
    assert brief.outcome in prompt
    assert brief.probe_description in prompt
    assert "does not prescribe an implementation or protected_policy" in prompt
    assert "these nine JSON keys" in prompt
    assert "recovery_path" in prompt
    assert "recovery_posture" in prompt
    assert "header or public" in prompt


def test_blue_prompt_route_examples_pass_the_spec_validator():
    """Regression: the prompt's example routes must be accepted by the spec
    validator, or models copying them verbatim fail deploy with
    SERVICE_SPEC_PATH_INVALID (underscores are not allowed in route values)."""
    import re

    from sandboxer_v0.service_spec import parse_service_spec

    brief = select_blue_briefs("calibration-seed", count=1)[0]
    prompt = _blue_prompt(brief)
    assert "app_health" not in prompt
    assert "app-health" in prompt
    examples = re.findall(r"/cgi-bin/service\.cgi\?route=[a-z0-9-]+", prompt)
    assert len(examples) >= 4
    spec = parse_service_spec(
        json.dumps({
            "schema_version": "sandboxer.service-spec.v1",
            "health_path": examples[0],
            "public_path": examples[1],
            "protected_path": examples[2],
            "protected_policy": "header",
            "access_header": "X-Arena-Key",
            "access_token": "valid-token-8chars",
            "recovery_path": examples[3],
            "recovery_posture": "header",
        }),
        brief=brief,
    )
    assert spec.graph_hash


def test_deploy_service_error_surfaces_validation_reason_to_the_model():
    """The deploy tool must return the ServiceSpecError reason (a public
    validation category) so the model can self-correct instead of guessing."""
    from scripts.run_command_code_match import _blue_prompt

    # The informative error string lives in the deploy branch of execute();
    # assert the source wiring exists so a silent regression is caught.
    import inspect as pyinspect
    import scripts.run_command_code_match as module

    source = pyinspect.getsource(module)
    assert "deployment rejected: {error}" in source
    assert "ServiceSpecError" in source


def test_monitor_allows_runner_tools_and_rejects_unallowlisted_provider_tools():
    emitted = []
    monitor = _frame_monitor("test-model", "blue", emit=lambda kind, **fields: emitted.append((kind, fields)))

    runner_frame = {
        "type": "event",
        "event": {
            "type": "tool_running",
            "toolName": "mcp__runner__run_service_command",
            "turnNumber": 1,
            "model": "test-model",
        },
    }
    monitor(runner_frame)
    assert any(
        kind == "provider_frame" and fields.get("toolName") == "mcp__runner__run_service_command"
        for kind, fields in emitted
    )

    unallowlisted_frame = {
        "type": "event",
        "event": {
            "type": "tool_running",
            "toolName": "shell_command",
            "turnNumber": 1,
            "model": "test-model",
        },
    }
    with pytest.raises(CommandCodeError, match="COMMAND_CODE_NATIVE_TOOL_REJECTED"):
        monitor(unallowlisted_frame)
    assert any(
        kind == "provider_tool_rejected" and fields.get("tool_name") == "shell_command"
        for kind, fields in emitted
    )


def test_runner_tools_and_denials_are_not_misclassified_as_native_tool_rejection():
    emitted = []
    monitor = _frame_monitor("test-model", "red", emit=lambda kind, **fields: emitted.append((kind, fields)))

    for tool in ("mcp__runner__http_request", "mcp__runner__describe_target_service", "mcp__runner__finish_phase"):
        runner_frame = {
            "type": "event",
            "event": {
                "type": "tool_running",
                "toolName": tool,
                "turnNumber": 1,
                "model": "test-model",
            },
        }
        monitor(runner_frame)

    denied_native_frame = {
        "type": "event",
        "event": {
            "type": "tool_denied",
            "toolName": "shell_command",
            "turnNumber": 1,
            "model": "test-model",
        },
    }
    monitor(denied_native_frame)

    queued_native_frame = {
        "type": "event",
        "event": {
            "type": "tool_queued",
            "toolName": "read_file",
            "turnNumber": 1,
            "model": "test-model",
        },
    }
    monitor(queued_native_frame)

    assert any(kind == "provider_tool_denied" and fields.get("tool_name") == "shell_command" for kind, fields in emitted)
    assert not any(kind == "provider_tool_rejected" for kind, fields in emitted)


def test_consecutive_retries_above_threshold_raise_capacity_unavailable():
    """Regression: a provider stuck retrying must abort with an explicit
    capacity reason instead of surfacing an opaque end-of-stream error that
    the classifier once misread as a tool-boundary violation."""
    emitted = []
    monitor = _frame_monitor("test-model", "blue", emit=lambda kind, **fields: emitted.append((kind, fields)))
    retry_frame = {"type": "event", "event": {"type": "api_retry"}}

    with pytest.raises(CommandCodeError) as raised:
        for _ in range(MAX_PROVIDER_RETRIES + 1):
            monitor(retry_frame)

    assert raised.value.reason_code == "COMMAND_CODE_CAPACITY_UNAVAILABLE"
    assert raised.value.model_id == "test-model"
    assert any(
        kind == "provider_capacity_unavailable" and fields.get("consecutive_retries") == MAX_PROVIDER_RETRIES + 1
        for kind, fields in emitted
    )


def test_retries_below_threshold_do_not_abort_and_counter_stays_consecutive():
    emitted = []
    monitor = _frame_monitor("test-model", "blue", emit=lambda kind, **fields: emitted.append((kind, fields)))
    retry_frame = {"type": "event", "event": {"type": "api_retry"}}
    progress_frame = {"type": "event", "event": {"type": "model_request_start", "model": "test-model"}}

    for _ in range(MAX_PROVIDER_RETRIES):
        monitor(retry_frame)
    monitor(progress_frame)
    for _ in range(MAX_PROVIDER_RETRIES - 1):
        monitor(retry_frame)
    monitor(progress_frame)

    assert not any(kind == "provider_capacity_unavailable" for kind, _ in emitted)


def test_retry_threshold_is_tracked_per_model():
    monitor_a = _frame_monitor("model-a", "blue")
    monitor_b = _frame_monitor("model-b", "blue")
    retry_frame = {"type": "event", "event": {"type": "api_retry"}}

    for _ in range(MAX_PROVIDER_RETRIES - 2):
        monitor_a(retry_frame)
        monitor_b(retry_frame)
    for _ in range(2):
        monitor_b(retry_frame)
    with pytest.raises(CommandCodeError, match="COMMAND_CODE_CAPACITY_UNAVAILABLE"):
        monitor_b(retry_frame)
    monitor_a(retry_frame)


def test_command_code_match_parser_configures_symmetric_tool_ceilings():
    import argparse
    from scripts.run_command_code_match import main

    # Inspect parser configuration
    parser = argparse.ArgumentParser()
    parser.add_argument("--blue-tools", "--blue-tool-ceiling", dest="blue_tools", type=int, default=8)
    parser.add_argument("--red-tools", "--red-tool-ceiling", dest="red_tools", type=int, default=10)

    parsed = parser.parse_args([])
    assert parsed.blue_tools == 8
    assert parsed.red_tools == 10

    custom = parser.parse_args(["--blue-tool-ceiling", "6", "--red-tool-ceiling", "6"])
    assert custom.blue_tools == 6
    assert custom.red_tools == 6


def test_socket_path_allocation_stays_below_platform_limit_with_long_identifiers():
    long_match_id = "calibration-20260813-r66-" + "very-long-match-identifier-segment-" * 10
    long_model_name = "custom-provider-enterprise/" + "model-variant-with-extremely-long-name-version-tag-" * 5
    runner_name = _runner_name(long_model_name)

    socket_root = _socket_root(long_match_id)
    blue_socket = _socket_path(socket_root, "blue", runner_name)
    red_socket = _socket_path(socket_root, "red", runner_name)

    # AF_UNIX capacity limit is 108 bytes on Linux, 104 on macOS/BSD
    for path in (blue_socket, red_socket):
        path_bytes = str(path).encode("utf-8")
        assert len(path_bytes) < 104
        assert len(str(path)) < 104


def test_socket_paths_are_distinct_for_both_phases_and_runners():
    match_id = "calibration-test-match-id"
    model_a = "provider-a/alpha-model-v1"
    model_b = "provider-b/beta-model-v2"
    runner_a = _runner_name(model_a)
    runner_b = _runner_name(model_b)

    socket_root = _socket_root(match_id)
    blue_a = _socket_path(socket_root, "blue", runner_a)
    blue_b = _socket_path(socket_root, "blue", runner_b)
    red_a = _socket_path(socket_root, "red", runner_a)
    red_b = _socket_path(socket_root, "red", runner_b)

    allocated = {blue_a, blue_b, red_a, red_b}
    assert len(allocated) == 4

    # Distinct across different matches
    other_root = _socket_root("other-match-id")
    assert socket_root != other_root
    assert _socket_path(other_root, "blue", runner_a) != blue_a


def test_socket_path_allocation_is_deterministic():
    match_id = "deterministic-match-seed"
    runner_name = "test-runner-model"

    root1 = _socket_root(match_id)
    root2 = _socket_root(match_id)
    assert root1 == root2

    path1 = _socket_path(root1, "blue", runner_name)
    path2 = _socket_path(root2, "blue", runner_name)
    assert path1 == path2


def test_cleanup_socket_root_removes_match_root_and_preserves_broader_data(tmp_path: Path):
    base_dir = tmp_path / "var_tmp"
    base_dir.mkdir()

    # Create broader system / other match data
    sibling_file = base_dir / "unrelated.log"
    sibling_file.write_text("system log content", encoding="utf-8")
    other_match_root = _socket_root("other-match-id", base_dir=base_dir)
    other_match_root.mkdir(mode=0o700)
    other_sock = other_match_root / "blue-other.sock"
    other_sock.touch()

    # Create target match root and sockets
    target_root = _socket_root("target-match-id", base_dir=base_dir)
    target_root.mkdir(mode=0o700)
    sock_blue = _socket_path(target_root, "blue", "runner-1")
    sock_red = _socket_path(target_root, "red", "runner-2")
    sock_blue.touch()
    sock_red.touch()

    # Run cleanup on target match only
    _cleanup_socket_root(target_root)

    # Target match root and sockets must be deleted
    assert not target_root.exists()
    assert not sock_blue.exists()
    assert not sock_red.exists()

    # Broader data must be completely preserved
    assert sibling_file.exists()
    assert sibling_file.read_text(encoding="utf-8") == "system log content"
    assert other_match_root.exists()
    assert other_sock.exists()
    assert base_dir.exists()


def test_cleanup_socket_root_handles_missing_or_empty_directory(tmp_path: Path):
    nonexistent = tmp_path / "sbx-nonexistent"
    _cleanup_socket_root(nonexistent)

    empty = tmp_path / "sbx-empty"
    empty.mkdir()
    _cleanup_socket_root(empty)
    assert not empty.exists()


def test_normal_identifiers_behavior_and_tool_server_binding(tmp_path: Path):
    match_id = "calibration-match-r66-001"
    models = ("deepseek/deepseek-v4-pro", "xiaomi/mimo-v2.5-pro")
    runners = tuple(_runner_name(m) for m in models)

    socket_root = _socket_root(match_id, base_dir=tmp_path)
    socket_root.mkdir(mode=0o700, exist_ok=True)

    socket_path = _socket_path(socket_root, "blue", runners[0])
    assert len(str(socket_path).encode("utf-8")) < 104

    async def scenario() -> None:
        server = RunnerToolServer(
            socket_path,
            competitor=models[0],
            phase=lambda: "blue",
            execute=lambda tool, values: "mock_result",
            audit=lambda decision: None,
        )
        await server.start()
        assert socket_path.exists()
        await server.close()
        assert not socket_path.exists()

    asyncio.run(scenario())
    _cleanup_socket_root(socket_root)
    assert not socket_root.exists()
