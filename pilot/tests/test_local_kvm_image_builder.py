from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


BUILDER = Path(__file__).parents[1] / "scripts" / "build_local_kvm_base.py"


def test_image_builder_renders_an_immutable_runner_contract_without_building_a_vm(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered"

    subprocess.run(
        [sys.executable, str(BUILDER), "--render-only", str(rendered)],
        check=True, text=True, capture_output=True,
    )

    manifest = json.loads((rendered / "render-manifest.json").read_text())
    control = (rendered / "sandboxer-control").read_text()
    provision = (rendered / "provision-image").read_text()
    assert manifest["base_url"].endswith("generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2")
    assert manifest["base_sha256"] == "6e2e6fe0572b6632527f268d3659e8fccebda4e1ee470fafe2c4d7b85b6a4df6"
    assert manifest["template_sha256"]["sandboxer-control"] == hashlib.sha256(control.encode()).hexdigest()
    assert "cloud-init.disabled" in provision
    assert "adduser -D -H -u 1001" in provision
    assert "sandboxer-mount-runtime" in provision
    assert "busybox httpd" in (rendered / "sandboxer-toy").read_text()
    assert "PROBE_OK" in control and "NETWORK_PROBE" in control
    assert "sandboxer_no_credentials" in control and "sandboxer_private_mounts" in control
    setup = (rendered / "sandboxer-setup").read_text()
    bootstrap = (rendered / "sandboxer-mount-runtime").read_text()
    assert "/workspace" in setup
    assert "ip addr add \"$SANDBOXER_IP/24\" dev eth0" in setup
    assert "ip route del default" in setup
    assert "SANDBOXER_STAGE_SETUP" in bootstrap
    assert "SANDBOXER_STAGE_TOY" in bootstrap
    assert "SANDBOXER_STAGE_CONTROL" in bootstrap
    assert "/usr/local/libexec/sandboxer-setup" in bootstrap
    assert "/usr/local/libexec/sandboxer-toy" in bootstrap
    assert "/usr/local/libexec/sandboxer-control" in bootstrap
    assert "/etc/init.d/sandboxer-runner start" not in bootstrap


def test_image_builder_plan_binds_the_exact_profile_to_its_output_digest_path(tmp_path: Path) -> None:
    output = tmp_path / "runner.qcow2"

    result = subprocess.run(
        [sys.executable, str(BUILDER), "--plan", "--output", str(output)],
        check=True, text=True, capture_output=True,
    )

    plan = json.loads(result.stdout)
    assert plan["output"] == str(output)
    assert plan["profile"] == str(output.with_suffix(".profile.json"))
    assert plan["runtime_contract"] == {
        "root_filesystem": "readonly",
        "workspace_mount": "/workspace",
        "control_protocol": "virtio-serial-v1",
        "toy_service": "synthetic-http",
    }
