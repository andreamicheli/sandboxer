from __future__ import annotations

import asyncio
import json

from sandboxer_v0.runner_tool_server import RunnerToolServer, ToolDecision


def _call(path, payload):
    async def scenario():
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    return asyncio.run(scenario())


def test_runner_tool_server_is_phase_scoped_audited_and_stoppable(tmp_path):
    socket_path = tmp_path / "competitor.sock"
    decisions: list[ToolDecision] = []

    async def exercise():
        server = RunnerToolServer(
            socket_path,
            competitor="deepseek/deepseek-v4-pro",
            phase=lambda: "blue",
            execute=lambda tool, arguments: f"executed:{tool}:{arguments.get('path', '')}",
            audit=decisions.append,
        )
        await server.start()
        try:
            allowed = await asyncio.to_thread(
                _call,
                socket_path,
                {"tool": "write_service_file", "arguments": {"path": "app.py", "content": "safe"}},
            )
            denied = await asyncio.to_thread(
                _call,
                socket_path,
                {"tool": "submit_flag", "arguments": {"flag": "SANDBOXER-example"}},
            )
            server.stop("AUDITOR_STOP")
            stopped = await asyncio.to_thread(
                _call, socket_path, {"tool": "inspect_service", "arguments": {}}
            )
        finally:
            await server.close()
        return allowed, denied, stopped

    allowed, denied, stopped = asyncio.run(exercise())
    assert allowed == {"content": [{"type": "text", "text": "executed:write_service_file:app.py"}], "isError": False}
    assert denied["isError"] is True and denied["content"][0]["text"] == "RUNNER_TOOL_PHASE_DENIED"
    assert stopped["isError"] is True and stopped["content"][0]["text"] == "AUDITOR_STOP"
    assert [(item.tool, item.allowed, item.reason_code) for item in decisions] == [
        ("write_service_file", True, None),
        ("submit_flag", False, "RUNNER_TOOL_PHASE_DENIED"),
        ("inspect_service", False, "AUDITOR_STOP"),
    ]


def test_runner_tool_server_rejects_unknown_fields_and_unsafe_paths(tmp_path):
    socket_path = tmp_path / "competitor.sock"

    async def exercise():
        server = RunnerToolServer(
            socket_path,
            competitor="xiaomi/mimo-v2.5-pro",
            phase=lambda: "blue",
            execute=lambda tool, arguments: "must-not-run",
            audit=lambda decision: None,
        )
        await server.start()
        try:
            unknown = await asyncio.to_thread(
                _call, socket_path, {"tool": "shell", "arguments": {"command": "id"}}
            )
            traversal = await asyncio.to_thread(
                _call,
                socket_path,
                {"tool": "write_service_file", "arguments": {"path": "../escape", "content": "x"}},
            )
        finally:
            await server.close()
        return unknown, traversal

    unknown, traversal = asyncio.run(exercise())
    assert unknown["content"][0]["text"] == "RUNNER_TOOL_DENIED"
    assert traversal["content"][0]["text"] == "RUNNER_TOOL_ARGUMENTS_INVALID"


def test_runner_tool_execution_failure_is_returned_as_a_tool_error(tmp_path):
    socket_path = tmp_path / "competitor.sock"
    async def exercise():
        server = RunnerToolServer(
            socket_path, competitor="model", phase=lambda: "red",
            execute=lambda tool, arguments: (_ for _ in ()).throw(RuntimeError("private detail")),
            audit=lambda decision: None,
        )
        await server.start()
        try:
            return await asyncio.to_thread(
                _call, socket_path, {"tool": "run_service_command", "arguments": {"command": "false"}}
            )
        finally:
            await server.close()
    response = asyncio.run(exercise())
    assert response == {"content": [{"type": "text", "text": "RUNNER_TOOL_EXECUTION_FAILED"}], "isError": True}
