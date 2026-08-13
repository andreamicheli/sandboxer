"""Authoritative, phase-scoped tool boundary between a Model Adapter and Runner."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping


_MAX_MESSAGE_BYTES = 64 * 1024
_SERVICE_SPEC_FIELDS = frozenset({
    "schema_version", "health_path", "public_path", "protected_path",
    "protected_policy", "access_header", "access_token", "recovery_path",
    "recovery_posture",
})
_PHASE_TOOLS = {
    "blue": frozenset({"inspect_service", "deploy_service", "request_own_service", "finish_phase"}),
    "red": frozenset({"inspect_service", "describe_target_service", "http_request", "submit_flag", "finish_phase"}),
}


@dataclass(frozen=True)
class ToolDecision:
    competitor: str
    phase: str
    tool: str
    allowed: bool
    reason_code: str | None


Executor = Callable[[str, Mapping[str, object]], str | Awaitable[str]]


class RunnerToolServer:
    """Expose a finite Runner tool protocol over a private Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        *,
        competitor: str,
        phase: Callable[[], str],
        execute: Executor,
        audit: Callable[[ToolDecision], None],
        socket_owner: tuple[int, int] | None = None,
        max_tool_calls: int | None = None,
    ) -> None:
        if not socket_path.is_absolute() or not competitor:
            raise ValueError("absolute socket path and Competitor identity are required")
        if max_tool_calls is not None and max_tool_calls < 0:
            raise ValueError("max_tool_calls must be non-negative")
        self.socket_path = socket_path
        self._competitor = competitor
        self._phase = phase
        self._execute = execute
        self._audit = audit
        self._socket_owner = socket_owner
        self._max_tool_calls = max_tool_calls
        self._tool_calls = 0
        self._lock = asyncio.Lock()
        self._server: asyncio.AbstractServer | None = None
        self._stop_reason: str | None = None

    @property
    def max_tool_calls(self) -> int | None:
        return self._max_tool_calls

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            raise RuntimeError("RUNNER_TOOL_SOCKET_EXISTS")
        self._server = await asyncio.start_unix_server(self._handle_client, self.socket_path)
        if self._socket_owner is not None:
            os.chown(self.socket_path, *self._socket_owner)
        os.chmod(self.socket_path, 0o600)

    def stop(self, reason_code: str) -> None:
        self._stop_reason = reason_code

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self.socket_path.unlink(missing_ok=True)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), 2)
            if not raw or len(raw) > _MAX_MESSAGE_BYTES:
                response = self._error("RUNNER_TOOL_REQUEST_INVALID")
            else:
                response = await self._dispatch(json.loads(raw))
        except (asyncio.TimeoutError, json.JSONDecodeError):
            response = self._error("RUNNER_TOOL_REQUEST_INVALID")
        except Exception:
            response = self._error("RUNNER_TOOL_EXECUTION_FAILED")
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > _MAX_MESSAGE_BYTES:
            encoded = json.dumps(self._error("RUNNER_TOOL_RESPONSE_TOO_LARGE"), separators=(",", ":")).encode() + b"\n"
        writer.write(encoded)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _dispatch(self, request: object) -> dict[str, object]:
        async with self._lock:
            phase = self._phase()
            tool = request.get("tool") if isinstance(request, dict) else ""
            arguments = request.get("arguments") if isinstance(request, dict) else None
            reason = self._stop_reason
            if reason is None and (not isinstance(request, dict) or set(request) != {"tool", "arguments"}):
                reason = "RUNNER_TOOL_REQUEST_INVALID"
            if reason is None and (not isinstance(tool, str) or tool not in set().union(*_PHASE_TOOLS.values())):
                reason = "RUNNER_TOOL_DENIED"
            if reason is None and (phase not in _PHASE_TOOLS or tool not in _PHASE_TOOLS[phase]):
                reason = "RUNNER_TOOL_PHASE_DENIED"
            if reason is None and not self._valid_arguments(tool, arguments):
                reason = "RUNNER_TOOL_ARGUMENTS_INVALID"
            if reason is None and self._max_tool_calls is not None and self._tool_calls >= self._max_tool_calls and tool != "finish_phase":
                reason = "RUNNER_TOOL_CEILING_EXCEEDED"
            decision = ToolDecision(self._competitor, phase, str(tool), reason is None, reason)
            self._audit(decision)
            if reason is not None:
                return self._error(reason)
            if tool != "finish_phase":
                self._tool_calls += 1
        try:
            result = self._execute(tool, arguments)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            return self._error("RUNNER_TOOL_EXECUTION_FAILED")
        if not isinstance(result, str):
            return self._error("RUNNER_TOOL_RESPONSE_INVALID")
        return {"content": [{"type": "text", "text": result}], "isError": False}

    @staticmethod
    def _valid_arguments(tool: str, arguments: object) -> bool:
        if not isinstance(arguments, dict):
            return False
        if tool == "deploy_service":
            service = arguments.get("service")
            if set(arguments) != {"service"} or not isinstance(service, dict):
                return False
            if set(service) != _SERVICE_SPEC_FIELDS:
                return False
            if not all(isinstance(key, str) and (isinstance(value, str) or value is None)
                       for key, value in service.items()):
                return False
            try:
                return len(json.dumps(service, separators=(",", ":")).encode()) <= _MAX_MESSAGE_BYTES // 2
            except (TypeError, ValueError):
                return False
        expected = {
            "inspect_service": frozenset(),
            "describe_target_service": frozenset(),
            "request_own_service": frozenset({"method", "path", "headers", "body"}),
            "http_request": frozenset({"method", "path", "headers", "body"}),
            "submit_flag": frozenset({"flag"}),
            "finish_phase": frozenset({"summary"}),
        }[tool]
        if set(arguments) != expected or not all(isinstance(value, str) for value in arguments.values()):
            return False
        if any(len(value.encode()) > _MAX_MESSAGE_BYTES // 2 for value in arguments.values()):
            return False
        if tool in {"request_own_service", "http_request"}:
            return arguments["method"] in {"GET", "POST"} and arguments["path"].startswith("/")
        return all(value.strip() for value in arguments.values()) if arguments else True

    @staticmethod
    def _error(reason: str) -> dict[str, object]:
        return {"content": [{"type": "text", "text": reason}], "isError": True}
