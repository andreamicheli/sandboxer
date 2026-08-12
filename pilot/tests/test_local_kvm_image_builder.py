from __future__ import annotations

import hashlib
import json
import os
import pty
import select
import socket
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
    monkeypatch.setattr(build_local_kvm_base, "wait_for_nbd_ready", lambda _device: None)
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
    monkeypatch.setattr(build_local_kvm_base, "wait_for_nbd_ready", lambda _device: None)
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
    toy = (rendered / "sandboxer-toy").read_text()
    assert "/usr/sbin/httpd -f" in toy
    assert "/bin/busybox httpd" not in toy
    assert "PROBE_OK" in control and "NETWORK_PROBE" in control
    assert "sandboxer_no_credentials" in control and "sandboxer_private_mounts" in control
    setup = (rendered / "sandboxer-setup").read_text()
    bootstrap = (rendered / "sandboxer-mount-runtime").read_text()
    assert "/workspace" in setup
    assert "ip addr add \"$SANDBOXER_IP/24\" dev eth0" in setup
    assert setup.index("ip link set lo up") < setup.index("ip link set eth0 up")
    assert "ip route del default" in setup
    assert "sandboxer-route-after-setup" in setup
    assert setup.index("mount -o rw,nosuid,nodev,noexec /dev/vdb /workspace") < setup.index("mkdir -p /workspace/notes")
    assert "SANDBOXER_STAGE_SETUP" in bootstrap
    assert "SANDBOXER_STAGE_TOY" in bootstrap
    assert "SANDBOXER_STAGE_CONTROL" in bootstrap
    assert "/usr/local/libexec/sandboxer-setup" in bootstrap
    assert "/usr/sbin/httpd -p 8080 -u competitor -h /workspace/notes" in bootstrap
    assert "sandboxer_toy_http_ready 127.0.0.1" in bootstrap
    assert "/usr/local/libexec/sandboxer-control" in bootstrap
    assert "SANDBOXER_TOY_READY" in bootstrap
    assert "SANDBOXER_TOY_ROOT_FAILED" in bootstrap
    assert "SANDBOXER_TOY_EXEC_FAILED" in bootstrap
    assert "SANDBOXER_TOY_ADDRESS_UNAVAILABLE" in bootstrap
    assert "SANDBOXER_TOY_HTTPD_BIND_EXIT" in bootstrap
    assert "SANDBOXER_TOY_EXITED_OTHER" in bootstrap
    assert "kill -0" in bootstrap and "sandboxer_toy_http_ready" in bootstrap
    assert "timeout -s KILL 2" in (rendered / "sandboxer-common").read_text()
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
    common = (rendered / "sandboxer-common").read_text()
    assert "/bin/busybox timeout -s KILL 1 /sbin/ip route show default" in common
    assert "/bin/busybox timeout -s KILL 1 ip route show default" not in common
    assert "sandboxer_route_diagnostics" in control
    assert "has_default_route" not in control
    assert "if sandboxer_route_state" not in control
    assert "/bin/busybox timeout -s KILL 2 ping -c 1 -W 1 \"$1\"" in control
    assert "SANDBOXER_NETPROBE_STAGE=" in control
    assert 'tcp_connect "$SANDBOXER_PEER_IP" 8080 || peer_denied=1' in control
    assert 'tcp_http "$SANDBOXER_PEER_IP" 8080 && toy_http=1' in control
    assert 'tcp_connect "$SANDBOXER_PEER_IP" 8081 || alternate_denied=1' in control
    assert "tcp_connect 198.51.100.1 81" in control
    assert "tcp_connect 10.77.0.1 1" in control
    assert "tcp_http \"$SANDBOXER_PEER_IP\" 8081" not in control
    assert "wget -q -T 2 -O /dev/null http://198.51.100.1:81/" not in control


def test_rendered_toy_health_retries_a_bounded_http_request_after_background_start(tmp_path: Path) -> None:
    """A TCP-only probe is not a toy health check and can race httpd startup."""
    rendered = tmp_path / "rendered"
    subprocess.run([sys.executable, str(BUILDER), "--render-only", str(rendered)], check=True)
    common = (rendered / "sandboxer-common").read_text(encoding="utf-8")
    assert "sandboxer_toy_http_ready" in common
    assert "busybox wget -q -T 1 -O /dev/null" in common
    assert "sandboxer_toy_http_ready" in (rendered / "sandboxer-mount-runtime").read_text(encoding="utf-8")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        decoy_port = reservation.getsockname()[1]
    decoy = subprocess.Popen(
        ["/usr/bin/busybox", "sh", "-c", f"sleep 5 | exec /usr/bin/busybox nc -l -p {decoy_port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.1)
        bare_tcp = subprocess.run(
            ["/usr/bin/busybox", "nc", "-w", "1", "127.0.0.1", str(decoy_port)],
            input="", check=False, capture_output=True, text=True, timeout=2,
        )
        non_http = subprocess.run(
            ["/usr/bin/busybox", "wget", "-q", "-T", "1", "-O", "/dev/null", f"http://127.0.0.1:{decoy_port}/"],
            check=False, capture_output=True, text=True, timeout=2,
        )
        assert bare_tcp.returncode == 0
        assert non_http.returncode != 0
    finally:
        decoy.terminate()
        try:
            decoy.wait(timeout=2)
        except subprocess.TimeoutExpired:
            decoy.kill(); decoy.wait(timeout=2)

    document_root = tmp_path / "notes"; document_root.mkdir()
    (document_root / "index.html").write_text("synthetic", encoding="ascii")
    listener = subprocess.Popen(
        ["/usr/bin/busybox", "sh", "-c", f"sleep 3; exec /usr/bin/busybox httpd -f -p 127.0.0.2:8080 -h {document_root}"],
    )
    try:
        # The service contract is an HTTP response, and a single immediate
        # request can race a daemon launched in the background.
        immediate = subprocess.run(
            ["/usr/bin/busybox", "wget", "-q", "-T", "1", "-O", "/dev/null", "http://127.0.0.2:8080/"],
            check=False,
        )
        assert immediate.returncode != 0
        helper = tmp_path / "toy-health"
        helper.write_text(common.replace("/bin/busybox", "/usr/bin/busybox") + "\nsandboxer_toy_http_ready 127.0.0.2\n", encoding="utf-8")
        result = subprocess.run(["/usr/bin/busybox", "ash", str(helper)], check=False, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
    finally:
        listener.terminate()
        try:
            listener.wait(timeout=2)
        except subprocess.TimeoutExpired:
            listener.kill(); listener.wait(timeout=2)


def test_rendered_toy_reports_address_bind_and_process_outcomes_without_raw_output(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered"
    subprocess.run([sys.executable, str(BUILDER), "--render-only", str(rendered)], check=True)
    toy = (rendered / "sandboxer-toy").read_text(encoding="utf-8")
    bootstrap = (rendered / "sandboxer-mount-runtime").read_text(encoding="utf-8")

    assert "sandboxer_local_address_ready" in toy
    assert "/run/sandboxer-toy-process-outcome" in toy
    assert "toy_process_outcome address_unavailable" in toy
    assert "toy_process_outcome httpd_bind_exit" in toy
    assert "-u competitor" in toy
    assert "-p 8080" in toy
    assert '"$SANDBOXER_IP:8080"' not in toy
    assert "/usr/sbin/httpd -p 8080 -u competitor -h /workspace/notes" in bootstrap
    assert "exec su " not in toy
    assert "toy_process_outcome exited_other" in toy
    assert "2>" not in toy and "stderr" not in toy
    assert "cat /run/sandboxer-toy-process-outcome" in bootstrap
    assert "awk '{print $3}' /proc/\"$toy_pid\"/stat" in bootstrap
    assert "Z) toy_outcome exited_other" in bootstrap
    assert "SANDBOXER_TOY_ADDRESS_UNAVAILABLE" in bootstrap
    assert "SANDBOXER_TOY_HTTPD_BIND_EXIT" in bootstrap


@pytest.mark.parametrize(
    ("address_ready", "su_status", "expected"),
    [(False, 0, "address_unavailable"), (True, 1, "httpd_bind_exit")],
)
def test_rendered_toy_reproduces_fixed_bind_subcauses_offline(
    tmp_path: Path, address_ready: bool, su_status: int, expected: str,
) -> None:
    rendered = tmp_path / "rendered"
    subprocess.run([sys.executable, str(BUILDER), "--render-only", str(rendered)], check=True)
    common = tmp_path / "common"
    common.write_text(
        "sandboxer_load_metadata() { SANDBOXER_IP=192.0.2.10; return 0; }\n"
        f"sandboxer_local_address_ready() {{ return {0 if address_ready else 1}; }}\n",
        encoding="ascii",
    )
    document = tmp_path / "index.html"; document.write_text("synthetic", encoding="ascii")
    outcome = tmp_path / "outcome"
    fake_httpd = tmp_path / "httpd"
    fake_httpd.write_text(f"#!/bin/sh\nexit {su_status}\n", encoding="ascii"); fake_httpd.chmod(0o755)
    toy = (rendered / "sandboxer-toy").read_text(encoding="utf-8")
    toy = toy.replace("/usr/local/libexec/sandboxer-common", str(common))
    toy = toy.replace("/workspace/notes/index.html", str(document))
    toy = toy.replace("/usr/sbin/httpd", str(fake_httpd))
    toy = toy.replace("/run/sandboxer-toy-process-outcome", str(outcome))
    script = tmp_path / "toy"; script.write_text(toy, encoding="utf-8")

    subprocess.run(
        ["/usr/bin/busybox", "ash", str(script)], env=os.environ,
        check=False, capture_output=True, text=True,
    )

    assert outcome.read_text(encoding="ascii").strip() == expected


def test_rendered_route_state_is_a_value_contract_not_a_shell_predicate(tmp_path: Path) -> None:
    """An empty successful route listing is absent; only command I/O is unknown."""
    rendered = tmp_path / "rendered"
    subprocess.run([sys.executable, str(BUILDER), "--render-only", str(rendered)], check=True)
    fake_busybox = tmp_path / "busybox"
    fake_ip = tmp_path / "ip"
    fake_busybox.write_text("#!/bin/sh\nshift 4\nexec \"$@\"\n", encoding="ascii")
    fake_ip.write_text("#!/bin/sh\nprintf '%s' \"${ROUTE_OUTPUT:-}\"\n", encoding="ascii")
    fake_busybox.chmod(0o755); fake_ip.chmod(0o755)
    common = (rendered / "sandboxer-common").read_text(encoding="utf-8")
    common = common.replace("/bin/busybox", str(fake_busybox)).replace("/sbin/ip", str(fake_ip))
    common_path = tmp_path / "sandboxer-common"; common_path.write_text(common, encoding="utf-8")

    def state(*, output: str = "", fails: bool = False) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "ROUTE_OUTPUT": output}
        if fails:
            fake_busybox.write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
        else:
            fake_busybox.write_text("#!/bin/sh\nshift 4\nexec \"$@\"\n", encoding="ascii")
        fake_busybox.chmod(0o755)
        return subprocess.run(["/usr/bin/busybox", "ash", "-c", f". {common_path}; sandboxer_route_state"], text=True, capture_output=True, env=env)

    assert (result := state()).returncode == 0 and result.stdout == "absent\n"
    assert (result := state(output="default via 10.77.0.1\n")).returncode == 0 and result.stdout == "present\n"
    assert (result := state(fails=True)).returncode != 0 and result.stdout == ""


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
    sandboxer_route_diagnostics() { printf '%s\\n' 'absent absent'; }
sandboxer_dhcp_client_present() { printf '%s\\n' 0; }
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
    route_origin = tmp_path / "sandboxer-route-origin"
    dhcp_client = tmp_path / "sandboxer-dhcp-client"
    emitted = tmp_path / "sandboxer-control-diagnostics-emitted"
    test_control = tmp_path / "sandboxer-control"
    test_control.write_text(
        (rendered / "sandboxer-control").read_text(encoding="utf-8")
        .replace("/usr/local/libexec/sandboxer-common", f"{libexec}/sandboxer-common")
            .replace("PORT=/dev/virtio-ports/org.sandboxer.control", f"PORT={control_port}")
            .replace("/run/sandboxer-route-at-control", str(route_at_control))
            .replace("/run/sandboxer-route-origin", str(route_origin))
            .replace("/run/sandboxer-dhcp-client", str(dhcp_client))
            .replace("/run/sandboxer-control-diagnostics-emitted", str(emitted))
        .replace("/bin/busybox nc -w 1 \"$1\" \"$2\" </dev/null >/dev/null 2>&1", "/bin/busybox sleep 30")
        .replace("ping -c 1 -W 1 \"$1\" >/dev/null 2>&1", "/bin/busybox sleep 30")
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
        response = _read_protocol_line(master, timeout_seconds=11)
        assert time.monotonic() - started < 11
        proof = parse_network_proof(response, "a" * 64, "blue")
        assert proof.peer_denied and proof.alternate_denied and proof.icmp_denied
        # The shell fixture supplies no default route and forces every TCP
        # attempt to hit the timeout. That is the safe Blue egress outcome.
        assert proof.egress_denied and proof.egress_reason == "blocked"
        # It also proves that the guest emits the complete typed witness and
        # immediately accepts the next command.
        assert proof.orchestrator_denied and not proof.toy_http
        assert stage_log.read_text(encoding="ascii").splitlines() == [
            "SANDBOXER_ROUTE_ORIGIN_ABSENT",
            "SANDBOXER_DHCP_CLIENT_0",
            "SANDBOXER_NETPROBE_STAGE=start",
            "SANDBOXER_NETPROBE_STAGE=local_toy",
            "SANDBOXER_NETPROBE_STAGE=peer",
            "SANDBOXER_NETPROBE_STAGE=alternate",
            "SANDBOXER_NETPROBE_STAGE=icmp",
            "SANDBOXER_NETPROBE_STAGE=egress",
            "SANDBOXER_NETPROBE_STAGE=orchestrator",
            "SANDBOXER_NETPROBE_STAGE=emit",
        ]

        os.write(master, b"PROBE aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        ready = _read_protocol_line(master, timeout_seconds=1, minimum_lines=2)
        assert time.monotonic() - started < 12
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


def test_rendered_guest_control_emits_unknown_for_a_missing_route_marker(tmp_path: Path) -> None:
    rendered = tmp_path / "rendered"
    subprocess.run([sys.executable, str(BUILDER), "--render-only", str(rendered)], check=True)
    libexec = tmp_path / "libexec"; libexec.mkdir()
    (libexec / "sandboxer-common").write_text(
        "sandboxer_load_metadata() { SANDBOXER_NONCE=" + "a" * 64 + "; }\n"
        "sandboxer_no_credentials() { return 0; }\nsandboxer_private_mounts() { return 0; }\nsandboxer_route_diagnostics() { return 1; }\nsandboxer_dhcp_client_present() { echo unknown; }\n"
        "id() { echo 1001; }\ncat() { case \"$1\" in *after-setup) echo absent;; *at-control) return 1;; *) echo 11111111-1111-1111-1111-111111111111;; esac; }\ndate() { echo 1; }\n",
    )
    master, slave = pty.openpty(); tty.setraw(slave)
    stage = tmp_path / "stage"; port = os.ttyname(slave)
    control = (rendered / "sandboxer-control").read_text().replace("/usr/local/libexec/sandboxer-common", str(libexec / "sandboxer-common")).replace("PORT=/dev/virtio-ports/org.sandboxer.control", f"PORT={port}").replace("/run/sandboxer-route-at-control", str(tmp_path / "route-at-control")).replace("/run/sandboxer-route-origin", str(tmp_path / "route-origin")).replace("/run/sandboxer-dhcp-client", str(tmp_path / "dhcp-client")).replace("/run/sandboxer-control-diagnostics-emitted", str(tmp_path / "emitted")).replace(">> /dev/ttyS0", f">> {stage}")
    path = tmp_path / "control"; path.write_text(control)
    process = subprocess.Popen(["/usr/bin/busybox", "ash", str(path)])
    try:
        os.write(master, b"PROBE " + b"a" * 64 + b"\n")
        response = _read_protocol_line(master, timeout_seconds=2, minimum_lines=2)
        assert parse_control(response, "a" * 64, require_probe=True).route_at_control == "unknown"
        assert "SANDBOXER_ROUTE_MARKER_UNAVAILABLE" in stage.read_text()
    finally:
        process.terminate(); process.wait(timeout=2); os.close(master); os.close(slave)


def test_rendered_guest_control_keeps_one_route_snapshot_and_emits_it_once(tmp_path: Path) -> None:
    """A PROBE cannot combine an absent state with a later present origin."""
    rendered = tmp_path / "rendered"
    subprocess.run([sys.executable, str(BUILDER), "--render-only", str(rendered)], check=True)
    libexec = tmp_path / "libexec"; libexec.mkdir()
    calls = tmp_path / "route-diagnostic-calls"
    (libexec / "sandboxer-common").write_text(
        "sandboxer_load_metadata() { SANDBOXER_NONCE=" + "a" * 64 + "; }\n"
        f"sandboxer_route_diagnostics() {{ printf x >> {calls}; printf '%s\\n' 'absent absent'; }}\n"
        "sandboxer_dhcp_client_present() { echo 0; }\n"
        "sandboxer_no_credentials() { return 0; }\nsandboxer_private_mounts() { return 0; }\n"
        "id() { echo 1001; }\n"
        "cat() { case \"$1\" in *after-setup) echo absent;; /proc/sys/kernel/random/boot_id) echo 11111111-1111-1111-1111-111111111111;; *) command cat \"$@\";; esac; }\n"
        "date() { echo 1; }\n",
    )
    master, slave = pty.openpty(); tty.setraw(slave)
    stage = tmp_path / "stage"; port = os.ttyname(slave)
    route_state = tmp_path / "route-at-control"
    route_origin = tmp_path / "route-origin"
    dhcp_client = tmp_path / "dhcp-client"
    emitted = tmp_path / "route-diagnostics-emitted"
    control = (rendered / "sandboxer-control").read_text()
    control = control.replace("/usr/local/libexec/sandboxer-common", str(libexec / "sandboxer-common"))
    control = control.replace("PORT=/dev/virtio-ports/org.sandboxer.control", f"PORT={port}")
    control = control.replace("/run/sandboxer-route-at-control", str(route_state))
    control = control.replace("/run/sandboxer-route-origin", str(route_origin))
    control = control.replace("/run/sandboxer-dhcp-client", str(dhcp_client))
    control = control.replace("/run/sandboxer-control-diagnostics-emitted", str(emitted))
    control = control.replace(">> /dev/ttyS0", f">> {stage}")
    path = tmp_path / "control"; path.write_text(control)
    process = subprocess.Popen(["/usr/bin/busybox", "ash", str(path)])
    try:
        command = b"PROBE " + b"a" * 64 + b"\n"
        os.write(master, command)
        first = _read_protocol_line(master, timeout_seconds=2, minimum_lines=2)
        os.write(master, command)
        second = _read_protocol_line(master, timeout_seconds=2, minimum_lines=2)
        for response in (first, second):
            probe = parse_control(response, "a" * 64, require_probe=True)
            assert probe.route_at_control == "absent"
            assert probe.route_origin == "absent"
        assert calls.read_text(encoding="ascii") == "x"
        assert stage.read_text(encoding="ascii").splitlines() == [
            "SANDBOXER_ROUTE_ORIGIN_ABSENT", "SANDBOXER_DHCP_CLIENT_0",
        ]
    finally:
        process.terminate(); process.wait(timeout=2); os.close(master); os.close(slave)


@pytest.mark.parametrize("toy_bootstrap", ["ready", "root_failed", "exec_failed", "address_unavailable", "httpd_bind_exit", "exited_other", "unknown"])
def test_rendered_guest_control_emits_each_current_toy_bootstrap_state(tmp_path: Path, toy_bootstrap: str) -> None:
    """The guest's exact READY field order remains compatible with the parser."""
    rendered = tmp_path / "rendered"
    subprocess.run([sys.executable, str(BUILDER), "--render-only", str(rendered)], check=True)
    libexec = tmp_path / "libexec"; libexec.mkdir()
    (libexec / "sandboxer-common").write_text(
        "sandboxer_load_metadata() { SANDBOXER_NONCE=" + "a" * 64 + "; }\n"
        "sandboxer_route_diagnostics() { echo 'absent absent'; }\nsandboxer_dhcp_client_present() { echo 0; }\n"
        "sandboxer_no_credentials() { return 0; }\nsandboxer_private_mounts() { return 0; }\n"
        "id() { echo 1001; }\n"
        "cat() { case \"$1\" in *after-setup) echo absent;; /proc/sys/kernel/random/boot_id) echo 11111111-1111-1111-1111-111111111111;; *) command cat \"$@\";; esac; }\n"
        "date() { echo 1; }\n",
    )
    toy_state = tmp_path / "toy-state"; toy_state.write_text(toy_bootstrap + "\n", encoding="ascii")
    master, slave = pty.openpty(); tty.setraw(slave)
    port = os.ttyname(slave)
    control = (rendered / "sandboxer-control").read_text()
    control = control.replace("/usr/local/libexec/sandboxer-common", str(libexec / "sandboxer-common"))
    control = control.replace("PORT=/dev/virtio-ports/org.sandboxer.control", f"PORT={port}")
    control = control.replace("/run/sandboxer-route-at-control", str(tmp_path / "route-at-control"))
    control = control.replace("/run/sandboxer-route-origin", str(tmp_path / "route-origin"))
    control = control.replace("/run/sandboxer-dhcp-client", str(tmp_path / "dhcp-client"))
    control = control.replace("/run/sandboxer-control-diagnostics-emitted", str(tmp_path / "emitted"))
    control = control.replace("/run/sandboxer-toy-bootstrap-state", str(toy_state))
    path = tmp_path / "control"; path.write_text(control)
    process = subprocess.Popen(["/usr/bin/busybox", "ash", str(path)])
    try:
        os.write(master, b"PROBE " + b"a" * 64 + b"\n")
        response = _read_protocol_line(master, timeout_seconds=2, minimum_lines=2)
        assert parse_control(response, "a" * 64, require_probe=True).toy_bootstrap == toy_bootstrap
    finally:
        process.terminate(); process.wait(timeout=2); os.close(master); os.close(slave)


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
