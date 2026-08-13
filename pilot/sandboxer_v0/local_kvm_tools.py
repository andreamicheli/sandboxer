"""Strict single-line tool protocol for the local KVM control channel."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Mapping


MAX_TOOL_MESSAGE_BYTES = 64 * 1024
_NONCE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_RESPONSE = re.compile(r"TOOL_RESULT nonce=([A-Za-z0-9_-]+) status=([01]) output=([A-Za-z0-9+/]*={0,2})\n\Z")
_SAFE_EXECUTION_FAILURES = frozenset({
    "HTTP_REQUEST_APPLICATION_UNAVAILABLE", "HTTP_REQUEST_ARGUMENTS_INVALID",
})


class RunnerToolExecutionError(RuntimeError):
    """A finite, non-sensitive Runner tool failure surfaced to the Orchestrator."""


def _encoded(value: str) -> str:
    # Shell tokenization in the Runner deliberately treats whitespace as a
    # separator.  Reserve '-' as the only empty-value sentinel (it is not a
    # Base64 character) so optional HTTP header/body fields retain arity.
    return base64.b64encode(value.encode()).decode("ascii") or "-"


def encode_tool_request(nonce: str, tool: str, arguments: Mapping[str, object]) -> str:
    if not _NONCE.fullmatch(nonce):
        raise RuntimeError("RUNNER_TOOL_REQUEST_INVALID")
    values: tuple[str, ...]
    if tool == "inspect_service" and not arguments:
        values = ()
    elif tool == "orchestrator_deploy_service" and set(arguments) == {"config"}:
        values = (str(arguments["config"]),)
    elif tool == "orchestrator_http_request" and set(arguments) == {"peer", "method", "path", "headers", "body"}:
        values = tuple(str(arguments[key]) for key in ("peer", "method", "path", "headers", "body"))
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
        failure = output.strip()
        if failure in _SAFE_EXECUTION_FAILURES:
            raise RunnerToolExecutionError(failure)
        raise RunnerToolExecutionError("RUNNER_TOOL_EXECUTION_FAILED")
    return output
