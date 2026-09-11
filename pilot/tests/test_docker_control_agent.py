"""Unit tests for the Docker Runner control agent (run locally, no daemon).

The agent is pure-stdlib Python, so it executes on the host with SANDBOXER_*
env pointed at tmp dirs. Every emitted line is validated with the real
host-side parsers (parse_control / parse_network_proof / parse_tool_response),
proving the wire contract without a container.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sandboxer_v0.local_kvm_control import parse_control, parse_network_proof  # noqa: E402
from sandboxer_v0.local_kvm_tools import (  # noqa: E402
    RunnerToolExecutionError,
    encode_tool_request,
    parse_tool_response,
)

AGENT = Path(__file__).parents[1] / "docker" / "runner-control"
NONCE = "test-nonce-1"
UUID = "12345678-1234-1234-1234-1234567890ab"


@pytest.fixture()
def arena(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "arena"
    (root / "protected").mkdir(parents=True)
    (root / "submissions").mkdir(parents=True)
    (root / "service.env").write_text(
        "schema_version=sandboxer.service-spec.v1\n"
        "health_path=/cgi-bin/service.cgi?route=app-health\n"
        "public_path=/cgi-bin/service.cgi?route=app-public\n"
        "protected_path=/cgi-bin/service.cgi?route=app-protected\n"
        "protected_policy=deny\n"
        "access_header=\n"
        "access_token=\n"
        "recovery_path=/cgi-bin/service.cgi?route=app-recovery\n"
        "recovery_posture=isolated\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SANDBOXER_NONCE", NONCE)
    monkeypatch.setenv("SANDBOXER_RUNNER_UUID", UUID)
    monkeypatch.setenv("SANDBOXER_ARENA_SUBNET", "10.77.99.0/24")
    monkeypatch.setenv("SANDBOXER_TOY_PORT", "18080")
    monkeypatch.setenv("SANDBOXER_ARENA_ROOT", str(root))
    return root


def _run_agent(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(AGENT), *argv],
        capture_output=True, text=True, timeout=30,
    )


def test_probe_lines_parse(arena: Path) -> None:
    proc = _run_agent("PROBE", NONCE)
    assert proc.returncode == 0
    parsed = parse_control(proc.stdout, NONCE, require_probe=True)
    assert parsed.uid != 0
    assert parsed.toy_bootstrap in {"ready", "exec_failed", "unknown"}
    assert parsed.clock_epoch > 0


def test_probe_wrong_nonce_silent(arena: Path) -> None:
    proc = _run_agent("PROBE", "wrong")
    assert proc.returncode != 0
    assert proc.stdout == ""


def test_tool_roundtrip_through_real_parsers(arena: Path) -> None:
    def call(tool: str, arguments: dict) -> str:
        request = encode_tool_request(NONCE, tool, arguments)
        parts = request.strip().split(" ")
        proc = _run_agent("TOOL", *parts[1:])
        assert proc.returncode == 0
        return parse_tool_response(proc.stdout, NONCE)

    assert "ServiceSpec schema" in call("inspect_service", {})
    assert call("orchestrator_deploy_service", {"config": "k=v\n"}).startswith("deployment promoted ")
    assert (arena / "service.env").read_text() == "k=v\n"
    assert call("orchestrator_place_flag", {"flag": "SANDBOXER-x"}) == "flag placed"
    assert call("submit_flag", {"flag": "SANDBOXER-x"}) == "submission recorded for Orchestrator verification"
    assert call("orchestrator_read_submission", {}) == "SANDBOXER-x"
    digest = call("orchestrator_workspace_digest", {})
    assert len(digest) == 64
    # Unknown tools fail closed with the safe generic failure.
    request = encode_tool_request(NONCE, "inspect_service", {})
    proc = _run_agent("TOOL", NONCE, "no_such_tool")
    assert proc.returncode == 0
    with pytest.raises(RunnerToolExecutionError):
        parse_tool_response(proc.stdout, NONCE)


def test_netprobe_blue_denies_unreachable_peer(arena: Path) -> None:
    proc = _run_agent("NETPROBE", NONCE, "blue", "10.77.99.12")
    assert proc.returncode == 0
    proof = parse_network_proof(proc.stdout, NONCE, "blue")
    assert proof.peer_denied and not proof.toy_http
    assert proof.alternate_denied and proof.egress_denied
    assert proof.orchestrator_denied


def test_netprobe_red_reaches_live_peer(arena: Path) -> None:
    received: list[str] = []

    class Toy(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            received.append(self.path)
            body = b"healthy" if self.path.endswith("route=app-health") else b"nope"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 18080), Toy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        proc = _run_agent("NETPROBE", NONCE, "red", "127.0.0.1")
    finally:
        server.shutdown()
    assert proc.returncode == 0
    proof = parse_network_proof(proc.stdout, NONCE, "red")
    assert proof.peer_tcp and proof.toy_http and not proof.peer_denied
    assert proof.local_toy
    assert any("route=app-health" in path for path in received)


def test_http_tool_rejects_outside_subnet(arena: Path) -> None:
    request = encode_tool_request(
        NONCE, "orchestrator_http_request",
        {"peer": "8.8.8.8", "method": "GET", "path": "/cgi-bin/service.cgi?route=x", "headers": "", "body": ""},
    )
    proc = _run_agent("TOOL", *request.strip().split(" ")[1:])
    assert proc.returncode == 0
    with pytest.raises(RunnerToolExecutionError):
        parse_tool_response(proc.stdout, NONCE)


def test_tool_output_is_capped(arena: Path) -> None:
    big = "A" * 100000
    (arena / "service.env").write_text(big, encoding="utf-8")
    request = encode_tool_request(NONCE, "inspect_service", {})
    proc = _run_agent("TOOL", *request.strip().split(" ")[1:])
    assert proc.returncode == 0
    output = parse_tool_response(proc.stdout, NONCE)
    assert len(output.encode()) <= 32768
