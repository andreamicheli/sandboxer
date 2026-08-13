import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from sandboxer_v0.blue_briefs import BlueBrief, select_blue_briefs
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
        "recovery_path": "/cgi-bin/service.cgi?route=recovery",
        "recovery_posture": "isolated",
    }
    value.update(overrides)
    return json.dumps(value)


def test_service_spec_is_versioned_normalized_and_has_a_stable_graph_hash():
    spec = parse_service_spec(_spec())
    assert spec.render_runtime_config().startswith("schema_version=sandboxer.service-spec.v1\n")
    assert "recovery_path=/cgi-bin/service.cgi?route=recovery\n" in spec.render_runtime_config()
    assert "recovery_posture=isolated\n" in spec.render_runtime_config()
    assert len(spec.graph_hash) == 64


def test_service_spec_exposes_only_a_red_safe_target_contract():
    spec = parse_service_spec(_spec(
        health_path="/cgi-bin/service.cgi?route=status-v2",
        public_path="/cgi-bin/service.cgi?route=notes-v2",
        protected_path="/cgi-bin/service.cgi?route=vault-v2",
        protected_policy="header",
        access_header="X-Vault-Key",
        access_token="never-disclose-this-token",
        recovery_path="/cgi-bin/service.cgi?route=restore-v2",
        recovery_posture="header",
    ))
    assert spec.red_target_contract() == {
        "health_path": "/cgi-bin/service.cgi?route=status-v2",
        "public_path": "/cgi-bin/service.cgi?route=notes-v2",
        "protected_path": "/cgi-bin/service.cgi?route=vault-v2",
        "protected_policy": "header",
        "access_header": "X-Vault-Key",
        "recovery_path": "/cgi-bin/service.cgi?route=restore-v2",
        "recovery_posture": "header",
    }
    assert "never-disclose-this-token" not in json.dumps(spec.red_target_contract())


def test_service_spec_exposes_safe_calibration_metadata_without_credentials():
    spec = parse_service_spec(_spec(
        protected_policy="header",
        access_header="X-Vault-Key",
        access_token="never-disclose-this-token",
        recovery_path="/cgi-bin/service.cgi?route=restore-v2",
        recovery_posture="isolated",
    ))
    metadata = spec.calibration_metadata()
    assert metadata["protected_policy"] == "header"
    assert metadata["recovery_posture"] == "isolated"
    assert metadata["graph_hash"] == spec.graph_hash
    assert "never-disclose-this-token" not in json.dumps(metadata)


@pytest.mark.parametrize("override, reason", [
    ({"protected_path": "/unsafe"}, "SERVICE_SPEC_PATH_INVALID"),
    ({"recovery_path": "/unsafe"}, "SERVICE_SPEC_PATH_INVALID"),
    ({"recovery_path": "/cgi-bin/service.cgi?route=health"}, "SERVICE_SPEC_PATH_INVALID"),
    ({"protected_policy": "shell"}, "SERVICE_SPEC_POLICY_INVALID"),
    ({"recovery_posture": "shell"}, "SERVICE_SPEC_POSTURE_INVALID"),
    ({"recovery_posture": "header", "protected_policy": "deny"}, "SERVICE_SPEC_POLICY_FIELDS_INVALID"),
    ({"recovery_posture": "header", "protected_policy": "public"}, "SERVICE_SPEC_POLICY_FIELDS_INVALID"),
    ({"protected_policy": "header", "access_header": None}, "SERVICE_SPEC_HEADER_INVALID"),
    ({"protected_policy": "header", "access_header": "X-Access", "access_token": "short"}, "SERVICE_SPEC_TOKEN_INVALID"),
    ({"protected_policy": "deny", "access_token": "not-allowed"}, "SERVICE_SPEC_POLICY_FIELDS_INVALID"),
    ({"brief_family": "portable_notes"}, "SERVICE_SPEC_FIELDS_INVALID"),
    ({"public_note": "welcome-1234"}, "SERVICE_SPEC_FIELDS_INVALID"),
])
def test_service_spec_rejects_unsafe_or_ambiguous_configurations(override, reason):
    with pytest.raises(ServiceSpecError, match=reason):
        parse_service_spec(_spec(**override))


@pytest.mark.parametrize("invalid_args, reason", [
    ({"brief_family": "shell_execution"}, "SERVICE_SPEC_BRIEF_FAMILY_INVALID"),
    ({"brief_family": "portable_notes", "public_note": "../../flag"}, "SERVICE_SPEC_PUBLIC_NOTE_INVALID"),
    ({"brief_family": "portable_notes", "public_note": "with spaces"}, "SERVICE_SPEC_PUBLIC_NOTE_INVALID"),
    ({"public_note": "welcome-1234"}, "SERVICE_SPEC_BRIEF_INVALID"),
    ({"brief": BlueBrief("portable_notes", "out", "probe", {}), "brief_family": "portable_notes"}, "SERVICE_SPEC_BRIEF_INVALID"),
])
def test_service_spec_rejects_invalid_brief_extensions(invalid_args, reason):
    with pytest.raises(ServiceSpecError, match=reason):
        parse_service_spec(_spec(recovery_posture="public"), **invalid_args)


def test_service_spec_binds_each_brief_family_deterministically():
    briefs = select_blue_briefs("seed-42", count=3)
    assert len(briefs) == 3
    family_outputs = {}
    for brief in briefs:
        spec = parse_service_spec(_spec(recovery_posture="public"), brief=brief)
        assert spec.brief_family == brief.family
        assert spec.public_note == brief.parameters["public_note"]
        config = spec.render_runtime_config()
        assert f"brief_family={brief.family}\n" in config
        assert f"public_note={brief.parameters['public_note']}\n" in config
        metadata = spec.calibration_metadata()
        assert metadata["brief_family"] == brief.family
        family_outputs[brief.family] = spec

    assert set(family_outputs) == {"portable_notes", "shared_notes", "service_diagnostics"}
    assert len({s.graph_hash for s in family_outputs.values()}) == 3


def test_cgi_public_behavior_differs_across_families_and_preserves_health(tmp_path):
    script = (Path(__file__).parents[1] / "local_kvm_image" / "sandboxer-service.cgi").read_text()
    workspace = tmp_path / "workspace"; protected = workspace / "protected"
    protected.mkdir(parents=True)
    (protected / "flag.txt").write_text("SANDBOXER-example\n")

    rendered = script.replace("#!/bin/ash", "#!/usr/bin/busybox ash", 1).replace("/workspace", str(workspace))
    target = tmp_path / "service.cgi"; target.write_text(rendered); target.chmod(0o755)

    brief_cases = [
        ("portable_notes", "welcome-111111", "export:welcome-111111"),
        ("shared_notes", "welcome-222222", "shared:welcome-222222"),
        ("service_diagnostics", "welcome-333333", "diagnostics:metric=welcome-333333 status=ok"),
    ]
    observed_responses = []

    for family, note, expected_fragment in brief_cases:
        spec = parse_service_spec(
            _spec(protected_policy="deny", recovery_posture="public"),
            brief_family=family,
            public_note=note,
        )
        (workspace / "service.env").write_text(spec.render_runtime_config())

        # Probe public path
        res_public = subprocess.run(
            ["/usr/bin/busybox", "ash", str(target)],
            input="GET /cgi-bin/service.cgi?route=public HTTP/1.1\r\nHost: local\r\n\r\n",
            text=True, capture_output=True, check=False,
        )
        assert res_public.returncode == 0
        assert "HTTP/1.1 200 OK" in res_public.stdout
        assert expected_fragment in res_public.stdout
        observed_responses.append(res_public.stdout.strip())

        # Probe health path (always healthy)
        res_health = subprocess.run(
            ["/usr/bin/busybox", "ash", str(target)],
            input="GET /cgi-bin/service.cgi?route=health HTTP/1.1\r\nHost: local\r\n\r\n",
            text=True, capture_output=True, check=False,
        )
        assert res_health.returncode == 0
        assert "HTTP/1.1 200 OK" in res_health.stdout
        assert "healthy" in res_health.stdout

        # Probe protected path under deny policy
        res_prot = subprocess.run(
            ["/usr/bin/busybox", "ash", str(target)],
            input="GET /cgi-bin/service.cgi?route=protected HTTP/1.1\r\nHost: local\r\n\r\n",
            text=True, capture_output=True, check=False,
        )
        assert res_prot.returncode == 0
        assert "HTTP/1.1 403 Forbidden" in res_prot.stdout

    # All three family public responses are distinct
    assert len(set(observed_responses)) == 3


def test_cgi_recovery_and_protected_postures_behave_deterministically_under_briefs(tmp_path):
    script = (Path(__file__).parents[1] / "local_kvm_image" / "sandboxer-service.cgi").read_text()
    workspace = tmp_path / "workspace"; protected = workspace / "protected"
    protected.mkdir(parents=True)
    (protected / "flag.txt").write_text("SANDBOXER-captured-flag-12345\n")

    rendered = script.replace("#!/bin/ash", "#!/usr/bin/busybox ash", 1).replace("/workspace", str(workspace))
    target = tmp_path / "service.cgi"; target.write_text(rendered); target.chmod(0o755)

    def _run_req(spec: ServiceSpec, path: str, headers: str = "") -> tuple[int, str]:
        (workspace / "service.env").write_text(spec.render_runtime_config())
        req_lines = [f"GET {path} HTTP/1.1", "Host: local"]
        if headers:
            req_lines.append(headers)
        req_lines.append("\r\n")
        res = subprocess.run(
            ["/usr/bin/busybox", "ash", str(target)],
            input="\r\n".join(req_lines),
            text=True, capture_output=True, check=False,
        )
        return res.returncode, res.stdout

    # 1. Deny policy + Isolated recovery (default safe defense on non-brief baseline)
    spec_isolated = parse_service_spec(_spec(
        health_path="/cgi-bin/service.cgi?route=h1",
        public_path="/cgi-bin/service.cgi?route=pub1",
        protected_path="/cgi-bin/service.cgi?route=prot1",
        protected_policy="deny",
        recovery_path="/cgi-bin/service.cgi?route=rec1",
        recovery_posture="isolated",
    ))
    code, out = _run_req(spec_isolated, "/cgi-bin/service.cgi?route=h1")
    assert "200 OK" in out and "healthy" in out
    code, out = _run_req(spec_isolated, "/cgi-bin/service.cgi?route=pub1")
    assert "200 OK" in out
    code, out = _run_req(spec_isolated, "/cgi-bin/service.cgi?route=prot1")
    assert "403 Forbidden" in out
    code, out = _run_req(spec_isolated, "/cgi-bin/service.cgi?route=rec1")
    assert "403 Forbidden" in out and "recovery isolated" in out

    # 2. Deny policy + Public recovery (emerging model-chosen sharing misconfiguration)
    spec_public_rec = parse_service_spec(_spec(
        health_path="/cgi-bin/service.cgi?route=h2",
        public_path="/cgi-bin/service.cgi?route=pub2",
        protected_path="/cgi-bin/service.cgi?route=prot2",
        protected_policy="deny",
        recovery_path="/cgi-bin/service.cgi?route=rec2",
        recovery_posture="public",
    ), brief_family="shared_notes", public_note="note-222")
    code, out = _run_req(spec_public_rec, "/cgi-bin/service.cgi?route=prot2")
    assert "403 Forbidden" in out
    code, out = _run_req(spec_public_rec, "/cgi-bin/service.cgi?route=rec2")
    assert "200 OK" in out and "SANDBOXER-captured-flag-12345" in out

    # 3. Header policy + Header recovery (authenticated collaborator recovery)
    spec_header = parse_service_spec(_spec(
        health_path="/cgi-bin/service.cgi?route=h3",
        public_path="/cgi-bin/service.cgi?route=pub3",
        protected_path="/cgi-bin/service.cgi?route=prot3",
        protected_policy="header",
        access_header="X-Collaborator-Key",
        access_token="secret-token-abcdef123456",
        recovery_path="/cgi-bin/service.cgi?route=rec3",
        recovery_posture="header",
    ), brief_family="service_diagnostics", public_note="metric-333")
    # Unauthenticated recovery
    code, out = _run_req(spec_header, "/cgi-bin/service.cgi?route=rec3")
    assert "403 Forbidden" in out and "recovery protected" in out
    # Authenticated recovery
    code, out = _run_req(spec_header, "/cgi-bin/service.cgi?route=rec3", "X-Collaborator-Key: secret-token-abcdef123456")
    assert "200 OK" in out and "SANDBOXER-captured-flag-12345" in out

    # 4. Header policy + Isolated recovery
    spec_header_iso = parse_service_spec(_spec(
        health_path="/cgi-bin/service.cgi?route=h4",
        public_path="/cgi-bin/service.cgi?route=pub4",
        protected_path="/cgi-bin/service.cgi?route=prot4",
        protected_policy="header",
        access_header="X-Collaborator-Key",
        access_token="secret-token-abcdef123456",
        recovery_path="/cgi-bin/service.cgi?route=rec4",
        recovery_posture="isolated",
    ))
    code, out = _run_req(spec_header_iso, "/cgi-bin/service.cgi?route=rec4", "X-Collaborator-Key: secret-token-abcdef123456")
    assert "403 Forbidden" in out and "recovery isolated" in out


def test_cgi_uses_busybox_query_string_routing_and_keeps_protected_store_outside_document_root(tmp_path):
    script = (Path(__file__).parents[1] / "local_kvm_image" / "sandboxer-service.cgi").read_text()
    workspace = tmp_path / "workspace"; protected = workspace / "protected"
    protected.mkdir(parents=True)
    (workspace / "service.env").write_text(
        parse_service_spec(_spec(protected_policy="public")).render_runtime_config()
    )
    (protected / "flag.txt").write_text("SANDBOXER-example\n")
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


def test_service_spec_rejects_brief_with_isolated_recovery_posture():
    brief = select_blue_briefs("seed-42", count=1)[0]
    with pytest.raises(ServiceSpecError, match="SERVICE_SPEC_POSTURE_INVALID"):
        parse_service_spec(_spec(recovery_posture="isolated"), brief=brief)
    with pytest.raises(ServiceSpecError, match="SERVICE_SPEC_POSTURE_INVALID"):
        parse_service_spec(_spec(recovery_posture="isolated"), brief_family=brief.family)


def test_service_spec_accepts_brief_with_header_and_public_recovery_postures():
    brief = select_blue_briefs("seed-42", count=1)[0]
    # Header posture with header protected_policy
    spec_header = parse_service_spec(_spec(
        protected_policy="header",
        access_header="X-Collaborator-Token",
        access_token="secret-collab-token-123",
        recovery_posture="header",
    ), brief=brief)
    assert spec_header.recovery_posture == "header"
    assert spec_header.brief_family == brief.family

    # Public posture with deny policy
    spec_public_deny = parse_service_spec(_spec(
        protected_policy="deny",
        recovery_posture="public",
    ), brief=brief)
    assert spec_public_deny.recovery_posture == "public"
    assert spec_public_deny.brief_family == brief.family

    # Public posture with header policy
    spec_public_header = parse_service_spec(_spec(
        protected_policy="header",
        access_header="X-Collaborator-Token",
        access_token="secret-collab-token-123",
        recovery_posture="public",
    ), brief=brief)
    assert spec_public_header.recovery_posture == "public"
    assert spec_public_header.brief_family == brief.family


def test_service_spec_red_contract_token_non_disclosure_under_brief_postures():
    brief = select_blue_briefs("seed-42", count=1)[0]
    # Brief + Header
    spec_header = parse_service_spec(_spec(
        health_path="/cgi-bin/service.cgi?route=h-header",
        public_path="/cgi-bin/service.cgi?route=pub-header",
        protected_path="/cgi-bin/service.cgi?route=prot-header",
        protected_policy="header",
        access_header="X-Secret-Auth",
        access_token="super-sensitive-token-do-not-disclose",
        recovery_path="/cgi-bin/service.cgi?route=rec-header",
        recovery_posture="header",
    ), brief=brief)
    contract_header = spec_header.red_target_contract()
    assert contract_header["recovery_posture"] == "header"
    assert contract_header["access_header"] == "X-Secret-Auth"
    assert "access_token" not in contract_header
    assert "super-sensitive-token-do-not-disclose" not in json.dumps(contract_header)

    # Brief + Public
    spec_public = parse_service_spec(_spec(
        health_path="/cgi-bin/service.cgi?route=h-pub",
        public_path="/cgi-bin/service.cgi?route=pub-pub",
        protected_path="/cgi-bin/service.cgi?route=prot-pub",
        protected_policy="deny",
        recovery_path="/cgi-bin/service.cgi?route=rec-pub",
        recovery_posture="public",
    ), brief=brief)
    contract_public = spec_public.red_target_contract()
    assert contract_public["recovery_posture"] == "public"
    assert contract_public["access_header"] is None
    assert "access_token" not in contract_public


def test_service_spec_baseline_isolated_compatibility():
    spec_baseline = parse_service_spec(_spec(
        protected_policy="deny",
        recovery_posture="isolated",
    ))
    assert spec_baseline.brief_family is None
    assert spec_baseline.recovery_posture == "isolated"
    assert "recovery_posture=isolated\n" in spec_baseline.render_runtime_config()
    assert spec_baseline.red_target_contract()["recovery_posture"] == "isolated"
