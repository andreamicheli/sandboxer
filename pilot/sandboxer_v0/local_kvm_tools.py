"""Strict single-line tool protocol for the local KVM control channel."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Mapping


MAX_TOOL_MESSAGE_BYTES = 64 * 1024
_NONCE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_RESPONSE = re.compile(r"TOOL_RESULT nonce=([A-Za-z0-9_-]+) status=([01]) output=([A-Za-z0-9+/]*={0,2})\n\Z")


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode()).decode("ascii")


def encode_tool_request(nonce: str, tool: str, arguments: Mapping[str, object]) -> str:
    if not _NONCE.fullmatch(nonce):
        raise RuntimeError("RUNNER_TOOL_REQUEST_INVALID")
    values: tuple[str, ...]
    if tool == "inspect_service" and not arguments:
        values = ()
    elif tool == "write_service_file" and set(arguments) == {"path", "content"}:
        values = (str(arguments["path"]), str(arguments["content"]))
    elif tool == "run_service_command" and set(arguments) == {"command"}:
        values = (str(arguments["command"]),)
    elif tool == "submit_flag" and set(arguments) == {"flag"}:
        values = (str(arguments["flag"]),)
    elif tool == "orchestrator_place_flag" and set(arguments) == {"flag"}:
        values = (str(arguments["flag"]),)
    elif tool == "orchestrator_read_submission" and not arguments:
        values = ()
    elif tool == "orchestrator_workspace_digest" and not arguments:
        values = ()
    elif tool == "orchestrator_peer_flag_witness" and set(arguments) == {"peer"}:
        values = (str(arguments["peer"]),)
    else:
        raise RuntimeError("RUNNER_TOOL_REQUEST_INVALID")
    request = " ".join(("TOOL", nonce, tool, *(_encoded(value) for value in values))) + "\n"
    if len(request.encode()) > MAX_TOOL_MESSAGE_BYTES:
        raise RuntimeError("RUNNER_TOOL_REQUEST_TOO_LARGE")
    return request


def parse_tool_response(response: str, nonce: str) -> str:
    if len(response.encode()) > MAX_TOOL_MESSAGE_BYTES:
        raise RuntimeError("RUNNER_TOOL_RESPONSE_INVALID")
    match = _RESPONSE.fullmatch(response)
    if match is None or match.group(1) != nonce:
        raise RuntimeError("RUNNER_TOOL_RESPONSE_INVALID")
    try:
        output = base64.b64decode(match.group(3), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as error:
        raise RuntimeError("RUNNER_TOOL_RESPONSE_INVALID") from error
    if match.group(2) != "0":
        raise RuntimeError("RUNNER_TOOL_EXECUTION_FAILED")
    return output
