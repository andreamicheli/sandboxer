from __future__ import annotations

import base64

import pytest

from sandboxer_v0.local_kvm_tools import RunnerToolExecutionError, encode_tool_request, parse_tool_response


def test_local_kvm_tool_protocol_is_single_line_bounded_and_round_trips_output():
    request = encode_tool_request(
        "nonce-1", "orchestrator_deploy_service", {"config": "schema_version=sandboxer.service-spec.v1\n"}
    )
    assert request.count("\n") == 1 and request.endswith("\n")
    assert "hello" not in request
    output = base64.b64encode(b"saved").decode()
    assert parse_tool_response(f"TOOL_RESULT nonce=nonce-1 status=0 output={output}\n", "nonce-1") == "saved"


def test_http_tool_preserves_empty_optional_fields_as_explicit_tokens():
    request = encode_tool_request(
        "nonce-1", "orchestrator_http_request",
        {"peer": "10.77.0.12", "method": "GET", "path": "/cgi-bin/service.cgi?route=health", "headers": "", "body": ""},
    )
    assert request.split()[-2:] == ["-", "-"]


def test_local_kvm_tool_protocol_rejects_forged_or_oversized_responses():
    with pytest.raises(RuntimeError, match="RUNNER_TOOL_RESPONSE_INVALID"):
        parse_tool_response("TOOL_RESULT nonce=wrong status=0 output=c2FmZQ==\n", "nonce-1")
    with pytest.raises(RuntimeError, match="RUNNER_TOOL_RESPONSE_INVALID"):
        parse_tool_response("TOOL_RESULT nonce=nonce-1 status=0 output=***\n", "nonce-1")


def test_local_kvm_tool_protocol_surfaces_only_allowlisted_execution_failures():
    known = base64.b64encode(b"HTTP_REQUEST_APPLICATION_UNAVAILABLE\n").decode()
    with pytest.raises(RunnerToolExecutionError, match="HTTP_REQUEST_APPLICATION_UNAVAILABLE"):
        parse_tool_response(f"TOOL_RESULT nonce=nonce-1 status=1 output={known}\n", "nonce-1")
    invalid = base64.b64encode(b"HTTP_REQUEST_ARGUMENTS_INVALID\n").decode()
    with pytest.raises(RunnerToolExecutionError, match="HTTP_REQUEST_ARGUMENTS_INVALID"):
        parse_tool_response(f"TOOL_RESULT nonce=nonce-1 status=1 output={invalid}\n", "nonce-1")
    unknown = base64.b64encode(b"private path or command output").decode()
    with pytest.raises(RunnerToolExecutionError, match="RUNNER_TOOL_EXECUTION_FAILED"):
        parse_tool_response(f"TOOL_RESULT nonce=nonce-1 status=1 output={unknown}\n", "nonce-1")
