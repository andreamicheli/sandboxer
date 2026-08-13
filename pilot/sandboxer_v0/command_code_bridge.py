"""Minimal fail-closed MCP-to-Runner Unix-socket bridge."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, TextIO

MAX_MESSAGE_BYTES = 64 * 1024


class BridgeError(RuntimeError):
    pass


def _tools() -> tuple[str, ...]:
    values = tuple(filter(None, os.environ.get("SANDBOXER_RUNNER_TOOLS", "").split(",")))
    if not values or len(values) != len(set(values)):
        raise BridgeError("RUNNER_BRIDGE_ALLOWLIST_INVALID")
    return values


async def _forward(socket_path: Path, request: Mapping[str, Any]) -> Mapping[str, Any]:
    if not socket_path.is_absolute():
        raise BridgeError("RUNNER_BRIDGE_SOCKET_INVALID")
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(socket_path), 2)
    try:
        payload = json.dumps(request, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        if len(payload) > MAX_MESSAGE_BYTES:
            raise BridgeError("RUNNER_BRIDGE_REQUEST_TOO_LARGE")
        writer.write(payload)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), 10)
        if not raw or len(raw) > MAX_MESSAGE_BYTES:
            raise BridgeError("RUNNER_BRIDGE_RESPONSE_INVALID")
        response = json.loads(raw)
        if not isinstance(response, dict) or set(response) - {"content", "isError"}:
            raise BridgeError("RUNNER_BRIDGE_RESPONSE_INVALID")
        content = response.get("content")
        if not isinstance(content, list) or not all(
            isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            for item in content
        ):
            raise BridgeError("RUNNER_BRIDGE_RESPONSE_INVALID")
        return response
    except (OSError, TimeoutError, json.JSONDecodeError) as error:
        raise BridgeError("RUNNER_BRIDGE_UNAVAILABLE") from error
    finally:
        writer.close()
        await writer.wait_closed()


async def _handle(message: Mapping[str, Any]) -> Mapping[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
            "serverInfo": {"name": "sandboxer-runner", "version": "1"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
            {"name": name, "description": f"Execute {name} inside the isolated Sandboxer Runner.",
             "inputSchema": {"type": "object", "additionalProperties": True}}
            for name in _tools()
        ]}}
    if method == "tools/call":
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments", {}) if isinstance(params, dict) else None
        if name not in _tools() or not isinstance(arguments, dict):
            raise BridgeError("RUNNER_BRIDGE_TOOL_DENIED")
        socket_path = Path(os.environ.get("SANDBOXER_RUNNER_SOCKET", ""))
        result = await _forward(socket_path, {"tool": name, "arguments": arguments})
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    raise BridgeError("RUNNER_BRIDGE_METHOD_DENIED")


async def serve(source: TextIO = sys.stdin, sink: TextIO = sys.stdout) -> None:
    for line in source:
        message: Any = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise BridgeError("RUNNER_BRIDGE_MESSAGE_INVALID")
            response = await _handle(message)
        except (BridgeError, json.JSONDecodeError) as error:
            request_id = message.get("id") if isinstance(message, dict) else None
            response = {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32000, "message": str(error)}}
        if response is not None:
            sink.write(json.dumps(response, sort_keys=True, separators=(",", ":")) + "\n")
            sink.flush()


if __name__ == "__main__":
    asyncio.run(serve())
