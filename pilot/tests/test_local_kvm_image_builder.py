from __future__ import annotations

import hashlib
import json
import os
import pty
import select
import subprocess
import sys
import time
import tty
from pathlib import Path

import pytest

from sandboxer_v0.local_kvm_control import parse_control, parse_network_proof
from scripts import build_local_kvm_base


BUILDER = Path(__file__).parents[1] / "scripts" / "build_local_kvm_base.py"


def test_image_sanitizer_reports_only_the_failing_stage(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "candidate.qcow2"
    image.touch()
    monkeypatch.setattr(build_local_kvm_base, "free_nbd_device", lambda: Path("/dev/nbd0"))
    monkeypatch.setattr(build_local_kvm_base.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""))

    for command, expected in (("qemu-nbd", "ATTACH"), ("mount", "MOUNT"), ("sync", "REMOVE")):
        def fail(argv, **_kwargs):
            if argv[0] == command:
                raise RuntimeError("API_KEY=not-disclosed /private/path")

        monkeypatch.setattr(build_local_kvm_base, "run", fail)
        try:
            build_local_kvm_base.sanitize_promoted_image(image)
        except RuntimeError as error:
            assert str(error) == f"BUILDER_IMAGE_SANITIZATION_FAILED:{expected}"
            assert "API_KEY" not in str(error) and "path" not in str(error)
        else:  # pragma: no cover
            raise AssertionError("sanitization failure must be typed")


def test_image_sanitizer_labels_a_residual_target_as_verify_failure(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "candidate.qcow2"
    image.touch()
    original_exists = Path.exists
    monkeypatch.setattr(build_local_kvm_base, "free_nbd_device", lambda: Path("/dev/nbd0"))
    monkeypatch.setattr(build_local_kvm_base, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(build_local_kvm_base.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""))
    monkeypatch.setattr(Path, "exists", lambda path: str(path).endswith("/var/lib/cloud") or original_exists(path))

    with pytest.raises(RuntimeError, match="^BUILDER_IMAGE_SANITIZATION_VERIFY_FAILED$") as error:
        build_local_kvm_base.sanitize_promoted_image(image)

    assert "/var/lib/cloud" not in str(error.value)


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
    assert "/bin/busybox timeout --help" in provision
    assert "rm -rf /root/.ssh /root/.aws /root/.config/gcloud" in provision
    assert "rm -rf /home/competitor/.ssh /home/competitor/.aws /home/competitor/.config/gcloud" in provision
    assert "rm -rf /var/lib/cloud /run/sandboxer-build" in provision
    assert 'test ! -e "$path"' in provision
    # The generic cloud image starts OpenRC networking with eth0 DHCP after
    # sysinit.  The Runner setup deliberately owns eth0 and removes its
    # default route, so the immutable build must suppress that later DHCP
    # unit rather than merely hoping the early setup wins a boot race.
    assert "rc-update del networking default" in provision
    assert "test ! -e /etc/runlevels/default/networking" in provision
    assert "rc-update del cloud-init-hotplugd default" in provision
    assert "test ! -e /etc/runlevels/default/cloud-init-hotplugd" in provision
    assert "iface eth0 inet dhcp" not in provision
    assert "sandboxer-mount-runtime" in provision
    assert "busybox httpd" in (rendered / "sandboxer-toy").read_text()
    assert "PROBE_OK" in control and "NETWORK_PROBE" in control
    assert "sandboxer_no_credentials" in control and "sandboxer_private_mounts" in control
    setup = (rendered / "sandboxer-setup").read_text()
    bootstrap = (rendered / "sandboxer-mount-runtime").read_text()
    assert "/workspace" in setup
    assert "ip addr add \"$SANDBOXER_IP/24\" dev eth0" in setup
    assert "ip route del default" in setup
    assert "sandboxer-route-after-setup" in setup
    assert setup.index("mount -o rw,nosuid,nodev,noexec /dev/vdb /workspace") < setup.index("mkdir -p /workspace/notes")
    assert "SANDBOXER_STAGE_SETUP" in bootstrap
    assert "SANDBOXER_STAGE_TOY" in bootstrap
    assert "SANDBOXER_STAGE_CONTROL" in bootstrap
    assert "/usr/local/libexec/sandboxer-setup" in bootstrap
    assert "/usr/local/libexec/sandboxer-toy" in bootstrap
    assert "/usr/local/libexec/sandboxer-control" in bootstrap
    assert "/etc/init.d/sandboxer-runner start" not in bootstrap


def test_image_builder_renders_sanitized_and_bounded_network_probes(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered"

    subprocess.run(
        [sys.executable, str(BUILDER), "--render-only", str(rendered)],
        check=True, text=True, capture_output=True,
    )

    control = (rendered / "sandboxer-control").read_text()
    assert "nc -z" not in control
    assert "/bin/busybox timeout -s KILL 1 /bin/busybox nc -w 1 \"$1\" \"$2\"" in control
    assert "/bin/busybox timeout -s KILL 2 ping -c 1 -W 1 \"$1\"" in control
    assert "SANDBOXER_NETPROBE_STAGE=" in control
    assert 'tcp_connect "$SANDBOXER_PEER_IP" 8080 || peer_denied=1' in control
    assert 'tcp_http "$SANDBOXER_PEER_IP" 8080 && toy_http=1' in control
    assert 'tcp_connect "$SANDBOXER_PEER_IP" 8081 || alternate_denied=1' in control
    assert "tcp_connect 198.51.100.1 81" in control
    assert "tcp_connect 10.77.0.1 1" in control
    assert "tcp_http \"$SANDBOXER_PEER_IP\" 8081" not in control
    assert "wget -q -T 2 -O /dev/null http://198.51.100.1:81/" not in control


def test_rendered_guest_control_bounds_hanging_netprobe_subcommands_and_keeps_the_protocol_live(tmp_path: Path) -> None:
    """The immutable handler must not let one network witness stall virtio I/O."""
    rendered = tmp_path / "rendered"
    subprocess.run(
        [sys.executable, str(BUILDER), "--render-only", str(rendered)],
        check=True, text=True, capture_output=True,
    )
    libexec = tmp_path / "libexec"
    libexec.mkdir()
    (libexec / "sandboxer-common").write_text(
        """sandboxer_load_metadata() {
    SANDBOXER_NONCE=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    SANDBOXER_IP=10.77.0.11
    SANDBOXER_PEER_IP=10.77.0.12
}
sandboxer_no_credentials() { return 0; }
sandboxer_private_mounts() { return 0; }
ip() { return 0; }
ping() { return 1; }
id() { printf '%s\\n' 1001; }
cat() {
    case "$1" in
        *sandboxer-route-after-setup|*sandboxer-route-at-control) printf '%s\\n' absent ;;
        *) printf '%s\\n' 11111111-1111-1111-1111-111111111111 ;;
    esac
}
date() { printf '%s\\n' 1720000000; }
""",
        encoding="utf-8",
    )

    master, slave = pty.openpty()
    tty.setraw(slave)
    control_port = os.ttyname(slave)
    stage_log = tmp_path / "netprobe-stages.log"
    route_at_control = tmp_path / "sandboxer-route-at-control"
    test_control = tmp_path / "sandboxer-control"
    test_control.write_text(
        (rendered / "sandboxer-control").read_text(encoding="utf-8")
        .replace("/usr/local/libexec/sandboxer-common", f"{libexec}/sandboxer-common")
        .replace("PORT=/dev/virtio-ports/org.sandboxer.control", f"PORT={control_port}")
        .replace("/run/sandboxer-route-at-control", str(route_at_control))
        .replace("/bin/busybox nc -w 1 \"$1\" \"$2\" </dev/null >/dev/null 2>&1", "/bin/busybox sleep 30")
        .replace("ping -c 1 -W 1 \"$1\" >/dev/null 2>&1", "/bin/busybox sleep 30")
        .replace("/bin/busybox timeout -s KILL 1 ip route show default | grep -q .", "false")
        .replace("> /dev/ttyS0", f"> {stage_log}"),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        ["/usr/bin/busybox", "ash", str(test_control)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        started = time.monotonic()
        assert process.poll() is None, process.stderr.read()
        os.write(master, b"NETPROBE aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa blue\n")
        response = _read_protocol_line(master, timeout_seconds=8)
        assert time.monotonic() - started < 8
        proof = parse_network_proof(response, "a" * 64, "blue")
        assert proof.peer_denied and proof.alternate_denied and proof.icmp_denied
        # The shell fixture supplies no default route and forces every TCP
        # attempt to hit the timeout. That is the safe Blue egress outcome.
        assert proof.egress_denied and proof.egress_reason == "blocked"
        # It also proves that the guest emits the complete typed witness and
        # immediately accepts the next command.
        assert proof.orchestrator_denied and not proof.toy_http
        assert stage_log.read_text(encoding="ascii").splitlines() == [
            "SANDBOXER_NETPROBE_STAGE=start",
            "SANDBOXER_NETPROBE_STAGE=peer",
            "SANDBOXER_NETPROBE_STAGE=alternate",
            "SANDBOXER_NETPROBE_STAGE=icmp",
            "SANDBOXER_NETPROBE_STAGE=egress",
            "SANDBOXER_NETPROBE_STAGE=orchestrator",
            "SANDBOXER_NETPROBE_STAGE=emit",
        ]

        os.write(master, b"PROBE aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        ready = _read_protocol_line(master, timeout_seconds=1, minimum_lines=2)
        assert time.monotonic() - started < 9
        assert parse_control(ready, "a" * 64, require_probe=True).uid == 1001
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        os.close(master)
        os.close(slave)


def _read_protocol_line(file_descriptor: int, *, timeout_seconds: float, minimum_lines: int = 1) -> str:
    deadline = time.monotonic() + timeout_seconds
    response = b""
    while response.count(b"\n") < minimum_lines:
        remaining = deadline - time.monotonic()
        assert remaining > 0, "guest control did not answer before its bounded deadline"
        readable, _, _ = select.select([file_descriptor], [], [], remaining)
        assert readable, "guest control did not answer before its bounded deadline"
        response += os.read(file_descriptor, 4096)
    return response.decode("ascii")


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
    assert plan["post_build_sanitization"] == {
        "remove_paths": ["/var/lib/cloud", "/run/sandboxer-build"],
        "verify_absent": True,
    }
