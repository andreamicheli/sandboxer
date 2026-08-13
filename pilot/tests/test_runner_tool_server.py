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
                {"tool": "deploy_service", "arguments": {"service": {
                    "schema_version": "sandboxer.service-spec.v1",
                    "health_path": "/cgi-bin/service.cgi?route=alive",
                    "public_path": "/cgi-bin/service.cgi?route=notes",
                    "protected_path": "/cgi-bin/service.cgi?route=vault",
                    "protected_policy": "header",
                    "access_header": "X-Sandboxer-Key",
                    "access_token": "safe-test-token",
                    "recovery_path": "/cgi-bin/service.cgi?route=recover",
                    "recovery_posture": "header",
                }}},
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
    assert allowed == {"content": [{"type": "text", "text": "executed:deploy_service:"}], "isError": False}
    assert denied["isError"] is True and denied["content"][0]["text"] == "RUNNER_TOOL_PHASE_DENIED"
    assert stopped["isError"] is True and stopped["content"][0]["text"] == "AUDITOR_STOP"
    assert [(item.tool, item.allowed, item.reason_code) for item in decisions] == [
        ("deploy_service", True, None),
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
            malformed = await asyncio.to_thread(
                _call,
                socket_path,
                {"tool": "deploy_service", "arguments": {"service": {}, "extra": "x"}},
            )
        finally:
            await server.close()
        return unknown, malformed

    unknown, malformed = asyncio.run(exercise())
    assert unknown["content"][0]["text"] == "RUNNER_TOOL_DENIED"
    assert malformed["content"][0]["text"] == "RUNNER_TOOL_ARGUMENTS_INVALID"


def test_runner_tool_server_requires_a_complete_structured_service_object(tmp_path):
    socket_path = tmp_path / "competitor.sock"
    service = {
        "schema_version": "sandboxer.service-spec.v1",
        "health_path": "/cgi-bin/service.cgi?route=alive",
        "public_path": "/cgi-bin/service.cgi?route=notes",
        "protected_path": "/cgi-bin/service.cgi?route=vault",
        "protected_policy": "public",
        "access_header": None,
        "access_token": None,
        "recovery_path": "/cgi-bin/service.cgi?route=recover",
        "recovery_posture": "public",
    }

    async def exercise():
        server = RunnerToolServer(socket_path, competitor="model", phase=lambda: "blue",
                                  execute=lambda tool, arguments: "promoted", audit=lambda decision: None)
        await server.start()
        try:
            accepted = await asyncio.to_thread(_call, socket_path, {"tool": "deploy_service", "arguments": {"service": service}})
            missing = await asyncio.to_thread(
                _call, socket_path,
                {"tool": "deploy_service", "arguments": {"service": {
                    key: value for key, value in service.items() if key != "access_token"
                }}},
            )
            extra = await asyncio.to_thread(_call, socket_path, {"tool": "deploy_service", "arguments": {"service": {**service, "unexpected": "x"}}})
        finally:
            await server.close()
        return accepted, missing, extra

    accepted, missing, extra = asyncio.run(exercise())
    assert accepted["isError"] is False
    assert missing["content"][0]["text"] == "RUNNER_TOOL_ARGUMENTS_INVALID"
    assert extra["content"][0]["text"] == "RUNNER_TOOL_ARGUMENTS_INVALID"


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
                _call, socket_path, {"tool": "http_request", "arguments": {
                    "method": "GET", "path": "/cgi-bin/service.cgi?route=health", "headers": "", "body": ""
                }}
            )
        finally:
            await server.close()
    response = asyncio.run(exercise())
    assert response == {"content": [{"type": "text", "text": "RUNNER_TOOL_EXECUTION_FAILED"}], "isError": True}


def test_runner_tool_server_exposes_target_contract_only_during_red(tmp_path):
    socket_path = tmp_path / "competitor.sock"
    decisions: list[ToolDecision] = []

    async def exercise():
        server = RunnerToolServer(
            socket_path, competitor="model", phase=lambda: "red",
            execute=lambda tool, arguments: "target-contract" if tool == "describe_target_service" else "unexpected",
            audit=decisions.append,
        )
        await server.start()
        try:
            return await asyncio.to_thread(
                _call, socket_path, {"tool": "describe_target_service", "arguments": {}}
            )
        finally:
            await server.close()

    response = asyncio.run(exercise())
    assert response == {"content": [{"type": "text", "text": "target-contract"}], "isError": False}
    assert decisions == [ToolDecision("model", "red", "describe_target_service", True, None)]


def test_runner_tool_server_enforces_ceiling_with_reason_and_allows_voluntary_finish(tmp_path):
    socket_path = tmp_path / "competitor.sock"
    decisions: list[ToolDecision] = []

    async def exercise():
        server = RunnerToolServer(
            socket_path,
            competitor="deepseek/deepseek-v4-pro",
            phase=lambda: "red",
            execute=lambda tool, arguments: f"executed:{tool}",
            audit=decisions.append,
            max_tool_calls=2,
        )
        await server.start()
        try:
            first = await asyncio.to_thread(
                _call, socket_path, {"tool": "describe_target_service", "arguments": {}}
            )
            second = await asyncio.to_thread(
                _call, socket_path, {"tool": "http_request", "arguments": {
                    "method": "GET", "path": "/cgi-bin/service.cgi?route=health", "headers": "", "body": ""
                }}
            )
            third_exceeded = await asyncio.to_thread(
                _call, socket_path, {"tool": "http_request", "arguments": {
                    "method": "GET", "path": "/cgi-bin/service.cgi?route=health", "headers": "", "body": ""
                }}
            )
            voluntary_finish = await asyncio.to_thread(
                _call, socket_path, {"tool": "finish_phase", "arguments": {"summary": "done"}}
            )
        finally:
            await server.close()
        return first, second, third_exceeded, voluntary_finish

    first, second, third_exceeded, voluntary_finish = asyncio.run(exercise())
    assert first == {"content": [{"type": "text", "text": "executed:describe_target_service"}], "isError": False}
    assert second == {"content": [{"type": "text", "text": "executed:http_request"}], "isError": False}
    assert third_exceeded == {"content": [{"type": "text", "text": "RUNNER_TOOL_CEILING_EXCEEDED"}], "isError": True}
    assert voluntary_finish == {"content": [{"type": "text", "text": "executed:finish_phase"}], "isError": False}

    assert [(item.tool, item.allowed, item.reason_code) for item in decisions] == [
        ("describe_target_service", True, None),
        ("http_request", True, None),
        ("http_request", False, "RUNNER_TOOL_CEILING_EXCEEDED"),
        ("finish_phase", True, None),
    ]


def test_runner_tool_server_cap_is_symmetric_and_independent_per_instance(tmp_path):
    socket1 = tmp_path / "model1.sock"
    socket2 = tmp_path / "model2.sock"
    decisions1: list[ToolDecision] = []
    decisions2: list[ToolDecision] = []

    async def exercise():
        server1 = RunnerToolServer(
            socket1,
            competitor="model1",
            phase=lambda: "red",
            execute=lambda tool, arguments: "s1",
            audit=decisions1.append,
            max_tool_calls=1,
        )
        server2 = RunnerToolServer(
            socket2,
            competitor="model2",
            phase=lambda: "red",
            execute=lambda tool, arguments: "s2",
            audit=decisions2.append,
            max_tool_calls=1,
        )
        await server1.start()
        await server2.start()
        try:
            m1_first = await asyncio.to_thread(_call, socket1, {"tool": "describe_target_service", "arguments": {}})
            m1_second = await asyncio.to_thread(_call, socket1, {"tool": "describe_target_service", "arguments": {}})
            m2_first = await asyncio.to_thread(_call, socket2, {"tool": "describe_target_service", "arguments": {}})
            m1_finish = await asyncio.to_thread(_call, socket1, {"tool": "finish_phase", "arguments": {"summary": "m1"}})
            m2_finish = await asyncio.to_thread(_call, socket2, {"tool": "finish_phase", "arguments": {"summary": "m2"}})
        finally:
            await server1.close()
            await server2.close()
        return m1_first, m1_second, m2_first, m1_finish, m2_finish

    m1_first, m1_second, m2_first, m1_finish, m2_finish = asyncio.run(exercise())
    assert m1_first["isError"] is False
    assert m1_second["isError"] is True and m1_second["content"][0]["text"] == "RUNNER_TOOL_CEILING_EXCEEDED"
    assert m2_first["isError"] is False
    assert m1_finish["isError"] is False
    assert m2_finish["isError"] is False


def test_runner_tool_server_concurrent_requests_strictly_enforce_atomic_ceiling(tmp_path):
    socket_path = tmp_path / "adversarial.sock"
    decisions: list[ToolDecision] = []
    executed_tools: list[str] = []

    async def _async_call(path, payload):
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        return response

    async def delayed_execute(tool: str, arguments: dict[str, object]) -> str:
        executed_tools.append(tool)
        await asyncio.sleep(0.01)
        return f"result:{tool}:{arguments.get('path', '')}"

    async def exercise():
        server = RunnerToolServer(
            socket_path,
            competitor="model/adversarial-concurrency",
            phase=lambda: "red",
            execute=delayed_execute,
            audit=decisions.append,
            max_tool_calls=5,
        )
        await server.start()
        try:
            payload = {
                "tool": "http_request",
                "arguments": {
                    "method": "GET",
                    "path": "/cgi-bin/service.cgi?route=test",
                    "headers": "",
                    "body": "",
                },
            }
            tasks = [asyncio.create_task(_async_call(socket_path, payload)) for _ in range(20)]
            responses = await asyncio.gather(*tasks)
            finish_resp = await _async_call(
                socket_path,
                {"tool": "finish_phase", "arguments": {"summary": "phase complete"}},
            )
            return responses, finish_resp, server.tool_calls
        finally:
            await server.close()

    responses, finish_resp, final_tool_calls = asyncio.run(exercise())

    allowed_responses = [r for r in responses if not r.get("isError")]
    denied_responses = [r for r in responses if r.get("isError")]

    assert len(responses) == 20
    assert len(allowed_responses) == 5
    assert len(denied_responses) == 15
    assert executed_tools.count("http_request") == 5
    assert executed_tools.count("finish_phase") == 1
    assert len(executed_tools) == 6
    assert final_tool_calls == 5

    for r in allowed_responses:
        assert r["content"][0]["text"] == "result:http_request:/cgi-bin/service.cgi?route=test"

    for r in denied_responses:
        assert r["content"][0]["text"] == "RUNNER_TOOL_CEILING_EXCEEDED"

    assert finish_resp["isError"] is False
    assert finish_resp["content"][0]["text"] == "result:finish_phase:"

    allowed_decisions = [d for d in decisions if d.allowed and d.tool == "http_request"]
    denied_decisions = [d for d in decisions if not d.allowed and d.tool == "http_request"]
    finish_decisions = [d for d in decisions if d.tool == "finish_phase"]

    assert len(allowed_decisions) == 5
    assert all(d.reason_code is None for d in allowed_decisions)
    assert len(denied_decisions) == 15
    assert all(d.reason_code == "RUNNER_TOOL_CEILING_EXCEEDED" for d in denied_decisions)
    assert len(finish_decisions) == 1
    assert finish_decisions[0].allowed is True
