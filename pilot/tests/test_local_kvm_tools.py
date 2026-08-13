from __future__ import annotations

import base64

import pytest

from sandboxer_v0.local_kvm_tools import encode_tool_request, parse_tool_response


def test_local_kvm_tool_protocol_is_single_line_bounded_and_round_trips_output():
    request = encode_tool_request(
        "nonce-1", "write_service_file", {"path": "app.py", "content": "hello\nworld"}
    )
    assert request.count("\n") == 1 and request.endswith("\n")
    assert "hello" not in request
    output = base64.b64encode(b"saved").decode()
    assert parse_tool_response(f"TOOL_RESULT nonce=nonce-1 status=0 output={output}\n", "nonce-1") == "saved"


def test_local_kvm_tool_protocol_rejects_forged_or_oversized_responses():
    with pytest.raises(RuntimeError, match="RUNNER_TOOL_RESPONSE_INVALID"):
        parse_tool_response("TOOL_RESULT nonce=wrong status=0 output=c2FmZQ==\n", "nonce-1")
    with pytest.raises(RuntimeError, match="RUNNER_TOOL_RESPONSE_INVALID"):
        parse_tool_response("TOOL_RESULT nonce=nonce-1 status=0 output=***\n", "nonce-1")
