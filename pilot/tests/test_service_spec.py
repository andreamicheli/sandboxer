import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from sandboxer_v0.service_spec import SERVICE_SPEC_VERSION, ServiceSpecError, parse_service_spec


def _spec(**overrides):
    value = {
        "schema_version": SERVICE_SPEC_VERSION,
        "health_path": "/cgi-bin/service.cgi?route=health",
        "public_path": "/cgi-bin/service.cgi?route=public",
        "protected_path": "/cgi-bin/service.cgi?route=protected",
        "protected_policy": "deny",
        "access_header": None,
        "access_token": None,
    }
    value.update(overrides)
    return json.dumps(value)


def test_service_spec_is_versioned_normalized_and_has_a_stable_graph_hash():
    spec = parse_service_spec(_spec())
    assert spec.render_runtime_config().startswith("schema_version=sandboxer.service-spec.v1\n")
    assert len(spec.graph_hash) == 64


def test_service_spec_exposes_only_a_red_safe_target_contract():
    spec = parse_service_spec(_spec(
        health_path="/cgi-bin/service.cgi?route=status-v2",
        public_path="/cgi-bin/service.cgi?route=notes-v2",
        protected_path="/cgi-bin/service.cgi?route=vault-v2",
        protected_policy="header",
        access_header="X-Vault-Key",
        access_token="never-disclose-this-token",
    ))
    assert spec.red_target_contract() == {
        "health_path": "/cgi-bin/service.cgi?route=status-v2",
        "public_path": "/cgi-bin/service.cgi?route=notes-v2",
        "protected_path": "/cgi-bin/service.cgi?route=vault-v2",
        "protected_policy": "header",
        "access_header": "X-Vault-Key",
    }
    assert "never-disclose-this-token" not in json.dumps(spec.red_target_contract())


@pytest.mark.parametrize("override, reason", [
    ({"protected_path": "/unsafe"}, "SERVICE_SPEC_PATH_INVALID"),
    ({"protected_policy": "shell"}, "SERVICE_SPEC_POLICY_INVALID"),
    ({"protected_policy": "header", "access_header": None}, "SERVICE_SPEC_HEADER_INVALID"),
    ({"protected_policy": "header", "access_header": "X-Access", "access_token": "short"}, "SERVICE_SPEC_TOKEN_INVALID"),
    ({"protected_policy": "deny", "access_token": "not-allowed"}, "SERVICE_SPEC_POLICY_FIELDS_INVALID"),
])
def test_service_spec_rejects_unsafe_or_ambiguous_configurations(override, reason):
    with pytest.raises(ServiceSpecError, match=reason):
        parse_service_spec(_spec(**override))


def test_cgi_uses_busybox_query_string_routing_and_keeps_protected_store_outside_document_root(tmp_path):
    script = (Path(__file__).parents[1] / "local_kvm_image" / "sandboxer-service.cgi").read_text()
    workspace = tmp_path / "workspace"; protected = workspace / "protected"
    protected.mkdir(parents=True)
    (workspace / "service.env").write_text(
        parse_service_spec(_spec(protected_policy="public")).render_runtime_config()
    )
    (protected / "flag.txt").write_text("SANDBOXER-example\n")
    # The Runner's Alpine image provides /bin/ash; the host test uses the
    # installed BusyBox applet explicitly while preserving the wire contract.
    rendered = script.replace("#!/bin/ash", "#!/usr/bin/busybox ash", 1).replace("/workspace", str(workspace))
    target = tmp_path / "service.cgi"; target.write_text(rendered); target.chmod(0o755)
    result = subprocess.run(
        ["/usr/bin/busybox", "ash", str(target)],
        input="GET /cgi-bin/service.cgi?route=protected HTTP/1.1\r\nHost: local\r\n\r\n",
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert "HTTP/1.1 200 OK" in result.stdout and "SANDBOXER-example" in result.stdout


def test_service_health_route_is_available_over_the_runner_nc_wire_contract(tmp_path):
    """The Red preflight and model-facing requests use this exact public seam."""
    script = (Path(__file__).parents[1] / "local_kvm_image" / "sandboxer-service.cgi").read_text()
    workspace = tmp_path / "workspace"; workspace.mkdir()
    (workspace / "service.env").write_text(parse_service_spec(_spec()).render_runtime_config())
    rendered = script.replace("#!/bin/ash", "#!/usr/bin/busybox ash", 1).replace("/workspace", str(workspace))
    target = tmp_path / "service.cgi"; target.write_text(rendered); target.chmod(0o755)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    listener = subprocess.Popen(["/usr/bin/busybox", "nc", "-ll", "-p", str(port), "-e", str(target)])
    try:
        time.sleep(0.1)
        response = subprocess.run(
            ["/usr/bin/busybox", "wget", "-q", "-T", "2", "-O", "-", f"http://127.0.0.1:{port}/cgi-bin/service.cgi?route=health"],
            text=True, capture_output=True, check=False, timeout=3,
        )
        assert response.returncode == 0, response.stderr
        assert response.stdout == "healthy\n"
    finally:
        listener.terminate()
        listener.wait(timeout=3)
