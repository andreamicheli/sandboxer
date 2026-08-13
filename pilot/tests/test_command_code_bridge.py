from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from sandboxer_v0.command_code_bridge import BridgeError, _forward, _handle


def test_bridge_exposes_only_explicit_runner_tools(monkeypatch):
    monkeypatch.setenv("SANDBOXER_RUNNER_TOOLS", "read_note,submit_flag")
    result=asyncio.run(_handle({"jsonrpc":"2.0","id":1,"method":"tools/list"}))
    assert [tool["name"] for tool in result["result"]["tools"]]==["read_note","submit_flag"]
    submit = result["result"]["tools"][1]
    assert submit["inputSchema"]["required"] == ["flag"]
    assert submit["inputSchema"]["additionalProperties"] is False


def test_bridge_renders_target_contract_tool_without_input_fields(monkeypatch):
    monkeypatch.setenv("SANDBOXER_RUNNER_TOOLS", "describe_target_service")
    result = asyncio.run(_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    tool = result["result"]["tools"][0]
    assert tool["name"] == "describe_target_service"
    assert tool["inputSchema"] == {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def test_bridge_exposes_structured_deploy_service_schema(monkeypatch):
    monkeypatch.setenv("SANDBOXER_RUNNER_TOOLS", "deploy_service")
    result = asyncio.run(_handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
    tool = result["result"]["tools"][0]
    service = tool["inputSchema"]["properties"]["service"]
    assert tool["inputSchema"]["required"] == ["service"]
    assert service["additionalProperties"] is False
    assert set(service["required"]) == {
        "schema_version", "health_path", "public_path", "protected_path", "protected_policy",
        "access_header", "access_token", "recovery_path", "recovery_posture",
    }


def test_bridge_denies_unlisted_tool_without_touching_runner(monkeypatch):
    monkeypatch.setenv("SANDBOXER_RUNNER_TOOLS", "read_note")
    monkeypatch.setenv("SANDBOXER_RUNNER_SOCKET", "/must/not/connect")
    with pytest.raises(BridgeError,match="RUNNER_BRIDGE_TOOL_DENIED"):
        asyncio.run(_handle({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"shell","arguments":{}}}))


def test_bridge_forwards_and_validates_runner_response(tmp_path, monkeypatch):
    socket=tmp_path/"runner.sock"
    async def scenario():
        async def client(reader,writer):
            request=json.loads(await reader.readline())
            assert request=={"arguments":{"path":"note"},"tool":"read_note"}
            writer.write(b'{"content":[{"type":"text","text":"safe"}],"isError":false}\n')
            await writer.drain(); writer.close(); await writer.wait_closed()
        server=await asyncio.start_unix_server(client,socket)
        async with server:
            return await _forward(socket,{"tool":"read_note","arguments":{"path":"note"}})
    assert asyncio.run(scenario())["content"][0]["text"]=="safe"
