from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from sandboxer_v0.arena_safety import ArenaNetworkPolicy, Phase, TeardownState
from sandboxer_v0.local_kvm import (
    CommandResult,
    LocalKvmConfig,
    LocalKvmRunnerProvider,
    QemuStartupFailure,
    SocketWitnessResult,
    SocketWitnessStage,
    SubprocessLocalKvmHost,
)
from sandboxer_v0.local_kvm_control import ControlProbe, parse_control
from sandboxer_v0.runner_backend import PreflightWitnessFailed, ProductionRunnerBackend, ProvisioningFailed, RunnerPreflightFailed


class RecordingKvmHost:
    """External-process fixture at the LocalKvmRunnerProvider boundary."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.inputs: list[str] = []
        self.alive: set[int] = set()
        self.next_pid = 4100
        self.control_requests: list[str] = []
        self.control_timeouts: list[tuple[str, float]] = []
        self.control_failures = 0
        self.destroyed_cgroups: list[str] = []
        self.ttl_tokens: list[object] = []
        self.namespaces: set[str] = set()
        self.taps: dict[str, str] = {}
        self.namespace_delete_returns_nonzero = False
        self.cgroup_destroyable = True
        self.ttl_cancellable = True
        self.ttl_timer_remains = False
        self.tap_cleanup_works = True
        self.startup_failure: QemuStartupFailure | None = None
        self.qemu_stderr_paths: list[Path] = []
        self.socket_witnesses: list[tuple[Path, Path, str]] = []
        self.socket_witness_directory_metadata: list[tuple[int, int, int]] = []
        self.socket_witness_result = SocketWitnessResult.success()
        self.create_stale_control_socket = False
        self.terminate_leaves_process_alive = False
        self.wait_calls: list[tuple[int, str, float]] = []
        self.stop_order: list[str] = []
        self.route_witness_failure = False
        self.network_probe_failure: BaseException | None = None
        self.network_probe_response: str | None = None

    def run(self, argv: tuple[str, ...], *, input_text: str | None = None, timeout_seconds: float = 10) -> CommandResult:
        del timeout_seconds
        self.commands.append(argv)
        if input_text is not None:
            self.inputs.append(input_text)
        if argv[:3] == ("ip", "netns", "add"):
            self.namespaces.add(argv[-1])
        if argv[:3] == ("ip", "netns", "del"):
            namespace = argv[-1]
            self.namespaces.discard(namespace)
            self.taps = {tap: owner for tap, owner in self.taps.items() if owner != namespace}
            if self.namespace_delete_returns_nonzero:
                return CommandResult(1, "", "already absent")
        if argv[:3] == ("ip", "netns", "list"):
            return CommandResult(0, "".join(f"{namespace}\n" for namespace in sorted(self.namespaces)), "")
        if "tuntap" in argv and "add" in argv:
            self.taps[argv[argv.index("dev") + 1]] = argv[argv.index("exec") + 1]
        if "link" in argv and "del" in argv and any(item.startswith("tap-") for item in argv):
            tap = next(item for item in argv if item.startswith("tap-"))
            if self.tap_cleanup_works:
                self.taps.pop(tap, None)
            else:
                return CommandResult(1, "", "tap deletion unavailable")
        if "link" in argv and "show" in argv and any(item.startswith("tap-") for item in argv):
            tap = next(item for item in argv if item.startswith("tap-"))
            return CommandResult(0 if tap in self.taps else 1, "" if tap not in self.taps else tap, "" if tap in self.taps else "Device does not exist")
        if argv[:2] == ("qemu-img", "create"):
            Path(next(item for item in argv if item.endswith(".qcow2") and item != "qcow2")).touch()
        if argv[:1] == ("cloud-localds",):
            Path(next(item for item in argv if item.endswith(".iso"))).touch()
            if self.create_stale_control_socket:
                Path(next(item for item in argv if item.endswith(".iso"))).parent.joinpath("control.sock").touch()
        if argv[:3] == ("qemu-img", "check", "--output=json"):
            self.stop_order.append("check")
            return CommandResult(0, '{"filename":"overlay","format":"qcow2"}', "")
        if "addr" in argv and "show" in argv and "-j" in argv:
            return CommandResult(0, "[]", "")
        if "route" in argv and "default" in argv:
            if self.route_witness_failure:
                return CommandResult(1, "", "host route witness unavailable")
            return CommandResult(0, "", "")
        if "nft" in argv and "list" in argv:
            return CommandResult(0, self.inputs[-1] if self.inputs else "", "")
        return CommandResult(0, "", "")

    def start(self, argv: tuple[str, ...], *, stderr_path: Path) -> int:
        self.commands.append(argv)
        self.qemu_stderr_paths.append(stderr_path)
        if self.startup_failure is not None:
            raise self.startup_failure
        pid = self.next_pid
        self.alive.add(pid)
        self.next_pid += 1
        return pid

    def verify_socket_access(self, directory: Path, control_socket: Path, *, qemu_user: str) -> SocketWitnessResult:
        self.socket_witnesses.append((directory, control_socket, qemu_user))
        state = directory.stat()
        self.socket_witness_directory_metadata.append((state.st_uid, state.st_gid, state.st_mode & 0o777))
        return self.socket_witness_result

    def control_exchange(self, socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str:
        del socket_path
        if self.control_failures:
            self.control_failures -= 1
            raise TimeoutError("guest control is not ready")
        self.control_requests.append(payload)
        self.control_timeouts.append((payload, timeout_seconds))
        if payload.startswith("NETPROBE "):
            if self.network_probe_failure is not None:
                raise self.network_probe_failure
            if self.network_probe_response is not None:
                return self.network_probe_response
            _, nonce, phase = payload.split()
            if phase == "blue":
                return f"NETWORK_PROBE nonce={nonce} phase=blue peer_denied=1 toy_http=0 alternate_denied=1 icmp_denied=1 egress_denied=1 egress_reason=blocked orchestrator_denied=1\n"
            return f"NETWORK_PROBE nonce={nonce} phase=red peer_denied=0 toy_http=1 alternate_denied=1 icmp_denied=1 egress_denied=1 egress_reason=blocked orchestrator_denied=1\n"
        if payload.startswith("PHASE_RED "):
            nonce = payload.split()[1]
            return f"PHASE_RED_OK nonce={nonce}\n"
        runner_number = len(self.control_requests)
        boot_id = "11111111-1111-1111-1111-111111111111" if runner_number == 1 else "22222222-2222-2222-2222-222222222222"
        nonce = payload.split()[1]
        return f"READY nonce={nonce} uid=1001 boot_id={boot_id} no_credentials=1 private_mounts=1 route_after_setup=absent route_at_control=absent toy_bootstrap=ready\nPROBE_OK nonce={nonce} uid=1001 clock_epoch=1720000000\n"

    def process_alive(self, pid: int) -> bool:
        return pid in self.alive

    def process_identity(self, pid: int) -> str | None:
        return f"{pid}:fixture" if pid in self.alive else None

    def cgroup_contains(self, cgroup: str, pid: int) -> bool:
        return cgroup.startswith("/sys/fs/cgroup/sandboxer/") and pid in self.alive

    def cgroup_limited(self, cgroup: str, pid: int, memory_bytes: int, cpu_max: str, pids_max: int) -> bool:
        return self.cgroup_contains(cgroup, pid) and memory_bytes == 512 * 1024 * 1024 and cpu_max == "100000 100000" and pids_max == 128

    def arm_ttl(self, cgroup: str, identity: str, seconds: int) -> object:
        token = (cgroup, identity, seconds)
        self.ttl_tokens.append(token)
        return token

    def cancel_ttl(self, token: object) -> bool:
        if not self.ttl_cancellable or self.ttl_timer_remains:
            return False
        if token in self.ttl_tokens:
            self.ttl_tokens.remove(token)
        return True

    def terminate(self, pid: int) -> None:
        self.stop_order.append("term")
        if not self.terminate_leaves_process_alive:
            self.alive.discard(pid)

    def kill(self, pid: int) -> None:
        self.stop_order.append("kill")
        self.alive.discard(pid)

    def wait_for_exit(self, pid: int, identity: str, *, timeout_seconds: float) -> bool:
        self.wait_calls.append((pid, identity, timeout_seconds))
        self.stop_order.append("wait")
        return pid not in self.alive

    def create_cgroup(self, name: str, *, memory_max_bytes: int, cpu_max: str, pids_max: int) -> str:
        assert memory_max_bytes == 512 * 1024 * 1024
        assert cpu_max == "100000 100000"
        assert pids_max == 128
        return f"/sys/fs/cgroup/sandboxer/{name}"

    def attach_to_cgroup(self, cgroup: str, pid: int) -> None:
        assert cgroup.startswith("/sys/fs/cgroup/sandboxer/")
        assert pid in self.alive

    def destroy_cgroup(self, cgroup: str) -> bool:
        self.destroyed_cgroups.append(cgroup)
        return self.cgroup_destroyable


def configured_provider(tmp_path: Path) -> tuple[LocalKvmRunnerProvider, RecordingKvmHost]:
    base = tmp_path / "immutable-base.qcow2"
    base.write_bytes(b"known-good-qcow2-base")
    profile = tmp_path / "immutable-base.profile.json"
    profile.write_text(
        '{"schema_version":1,"image_sha256":"' + hashlib.sha256(base.read_bytes()).hexdigest() + '","root_filesystem":"readonly","workspace_mount":"/workspace",'
        '"control_protocol":"virtio-serial-v1","toy_service":"synthetic-http"}'
    )
    config = LocalKvmConfig(
        runner_root=tmp_path / "runners",
        base_image=base,
        base_image_sha256=hashlib.sha256(base.read_bytes()).hexdigest(),
        base_profile=profile,
        qemu_user="sandboxer-runner",
        qemu_uid=os.getuid(),
        qemu_gid=os.getgid(),
        toy_service_port=8080,
    )
    host = RecordingKvmHost()
    return LocalKvmRunnerProvider(config, host), host


def test_local_kvm_rehearsal_uses_distinct_overlays_control_and_a_disposable_network(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)

    report = ProductionRunnerBackend(provider).rehearse(
        match_id="kvm-rehearsal-001", runner_names=("atlas", "borealis")
    )

    assert report.terminal_code == "RUNNERS_DESTROYED"
    assert report.simulated_fixture is False
    assert report.production_ready is False
    assert all(check.passed for check in report.preflight_checks)
    assert all(item.state is TeardownState.DESTROYED for item in report.teardown_evidence)
    assert len({request.split()[1] for request in host.control_requests}) == 2
    assert {timeout for request, timeout in host.control_timeouts if request.startswith("PROBE ")} == {5}
    assert {timeout for request, timeout in host.control_timeouts if request.startswith("NETPROBE ")} == {24}
    blue_route_witnesses = [
        command for command in host.commands
        if len(command) == 6 and command[:2] == ("ip", "-n") and command[2].startswith("sbb-") and command[3:] == ("route", "show", "default")
    ]
    assert len({command[2] for command in blue_route_witnesses}) == 2
    qemu_commands = [command for command in host.commands if "qemu-system-x86_64" in command]
    assert len(qemu_commands) == 2
    assert all("setpriv" in command and any("sandboxer-runner" in item for item in command) for command in qemu_commands)
    assert all("-runas" not in command for command in qemu_commands)
    assert all("-daemonize" not in command and "-pidfile" not in command for command in qemu_commands)
    assert all("timeout" not in command for command in qemu_commands)
    assert not host.ttl_tokens
    assert all("-sandbox" in command and "-nodefaults" in command for command in qemu_commands)
    assert all("-virtfs" not in command and "hostfwd" not in " ".join(command) for command in qemu_commands)
    assert all(
        any(item.startswith(f"file={provider.config.base_image},if=virtio,format=qcow2,readonly=on") for item in command)
        for command in qemu_commands
    )
    seed_commands = [command for command in host.commands if command[:1] == ("cloud-localds",)]
    assert all(any(item.startswith("--network-config=") for item in command) for command in seed_commands)
    assert any("policy drop" in rule_set for rule_set in host.inputs)
    assert any("tcp dport 8080" in rule_set for rule_set in host.inputs)
    assert len(host.destroyed_cgroups) == 2
    assert not (tmp_path / "runners" / "kvm-rehearsal-001").exists()
    assert {path.name for path in host.qemu_stderr_paths} == {"qemu.stderr"}
    assert len(host.socket_witnesses) == 2
    assert all(item[2] == "sandboxer-runner" for item in host.socket_witnesses)
    assert all(
        metadata == (provider.config.qemu_uid, provider.config.qemu_gid, 0o700)
        for metadata in host.socket_witness_directory_metadata
    )


def test_local_kvm_match_root_keeps_traversal_permission_under_a_restrictive_umask(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    previous_umask = os.umask(0o077)
    try:
        runners = provider.provision("kvm-umask-traversal", ("atlas", "borealis"))
    finally:
        os.umask(previous_umask)

    match_root = host.socket_witnesses[0][0].parent
    state = match_root.stat()
    assert state.st_mode & 0o777 == 0o711
    assert state.st_uid == os.geteuid()
    assert provider.destroy(runners[0]).state is TeardownState.DESTROYED
    assert provider.destroy(runners[1]).state is TeardownState.DESTROYED


def test_local_kvm_refuses_a_preexisting_control_socket_without_unlinking_it(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    host.create_stale_control_socket = True

    try:
        provider.provision("kvm-stale-control", ("atlas", "borealis"))
    except ProvisioningFailed as error:
        assert error.reason_code == "CONTROL_SOCKET_PREEXISTS"
    else:  # pragma: no cover - explicit fail-closed contract
        raise AssertionError("a preexisting control socket must block the Runner")

    assert not host.socket_witnesses
    assert not any("qemu-system-x86_64" in command for command in host.commands)


def test_local_kvm_requires_non_root_socket_create_unlink_witness_before_qemu(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    host.socket_witness_result = SocketWitnessResult(SocketWitnessStage.UNLINK_FAILED, errno.EACCES)

    try:
        provider.provision("kvm-socket-witness", ("atlas", "borealis"))
    except ProvisioningFailed as error:
        assert error.reason_code == "QEMU_SOCKET_WITNESS_UNLINK_FAILED_ERRNO_13"
    else:  # pragma: no cover - explicit fail-closed contract
        raise AssertionError("an unproven QEMU socket directory must block the Runner")

    assert len(host.socket_witnesses) == 1
    assert not any("qemu-system-x86_64" in command for command in host.commands)


def test_local_kvm_labels_an_unavailable_blue_network_witness_without_generic_preflight_collapse(tmp_path: Path, monkeypatch) -> None:
    provider, _host = configured_provider(tmp_path)
    monkeypatch.setattr(
        provider,
        "_measure_network",
        lambda _records, _phase: (_ for _ in ()).throw(RuntimeError("private detail")),
    )

    with pytest.raises(RunnerPreflightFailed) as error:
        ProductionRunnerBackend(provider).rehearse(
            match_id="kvm-blue-witness", runner_names=("atlas", "borealis")
        )

    assert error.value.reason_code == "LOCAL_KVM_BLUE_NETWORK_WITNESS_UNAVAILABLE"


def test_local_kvm_labels_a_blue_host_route_witness_failure(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    host.route_witness_failure = True

    with pytest.raises(RunnerPreflightFailed) as error:
        ProductionRunnerBackend(provider).rehearse(
            match_id="kvm-blue-host-route", runner_names=("atlas", "borealis")
        )

    assert error.value.reason_code == "LOCAL_KVM_BLUE_HOST_ROUTE_WITNESS_UNAVAILABLE"


def test_local_kvm_keeps_guest_route_diagnostics_without_rejecting_active_blue_isolation(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    original_exchange = host.control_exchange

    def route_diagnostic(socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str:
        if payload.startswith("NETPROBE "):
            return original_exchange(socket_path, payload, timeout_seconds=timeout_seconds)
        response = original_exchange(socket_path, payload, timeout_seconds=timeout_seconds)
        return response.replace("route_at_control=absent", "route_at_control=present route_origin=other dhcp_client=1")

    host.control_exchange = route_diagnostic  # type: ignore[method-assign]
    report = ProductionRunnerBackend(provider).rehearse(
        match_id="kvm-guest-route-diagnostic", runner_names=("atlas", "borealis")
    )

    assert report.terminal_code == "RUNNERS_DESTROYED"
    assert set(provider._route_diagnostics.values()) == {("absent", "present", "other")}


def test_local_kvm_labels_a_blue_guest_netprobe_timeout(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    host.network_probe_failure = TimeoutError("private detail")

    with pytest.raises(RunnerPreflightFailed) as error:
        ProductionRunnerBackend(provider).rehearse(
            match_id="kvm-blue-netprobe-timeout", runner_names=("atlas", "borealis")
        )

    assert error.value.reason_code == "LOCAL_KVM_BLUE_GUEST_NETPROBE_TIMEOUT"


def test_local_kvm_labels_a_blue_guest_netprobe_invalid_response(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    host.network_probe_response = "not a network proof\n"

    with pytest.raises(RunnerPreflightFailed) as error:
        ProductionRunnerBackend(provider).rehearse(
            match_id="kvm-blue-netprobe-invalid", runner_names=("atlas", "borealis")
        )

    assert error.value.reason_code == "LOCAL_KVM_BLUE_GUEST_NETPROBE_INVALID_RESPONSE"


def test_local_kvm_fails_closed_when_red_neighbor_reset_is_not_acknowledged(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    original_exchange = host.control_exchange

    def failed_reset(socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str:
        if payload.startswith("PHASE_RED "):
            return "PHASE_RED_FAILED nonce=redacted\n"
        return original_exchange(socket_path, payload, timeout_seconds=timeout_seconds)

    host.control_exchange = failed_reset  # type: ignore[method-assign]

    with pytest.raises(RunnerPreflightFailed) as error:
        ProductionRunnerBackend(provider).rehearse(
            match_id="kvm-red-neighbor-reset", runner_names=("atlas", "borealis")
        )

    assert error.value.reason_code == "LOCAL_KVM_RED_NEIGHBOR_RESET_FAILED"
    assert all(item.state is TeardownState.DESTROYED for item in error.value.teardown_evidence)


def test_local_kvm_waits_for_guest_control_reopen_after_red_ack(tmp_path: Path, monkeypatch) -> None:
    provider, _host = configured_provider(tmp_path)
    waits: list[float] = []
    monkeypatch.setattr("sandboxer_v0.local_kvm.time.sleep", waits.append)

    report = ProductionRunnerBackend(provider).rehearse(
        match_id="kvm-red-control-barrier", runner_names=("atlas", "borealis")
    )

    assert report.terminal_code == "RUNNERS_DESTROYED"
    assert waits == [0.25] * 4


def test_local_kvm_does_not_mislabel_a_failed_blue_peer_witness_as_orchestrator_reachability(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    original_exchange = host.control_exchange

    def failed_peer_witness(socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str:
        if payload.startswith("NETPROBE "):
            _, nonce, phase = payload.split()
            return (
                f"NETWORK_PROBE nonce={nonce} phase={phase} peer_denied=0 toy_http=0 "
                "alternate_denied=1 icmp_denied=1 egress_denied=1 egress_reason=blocked orchestrator_denied=1\n"
            )
        return original_exchange(socket_path, payload, timeout_seconds=timeout_seconds)

    host.control_exchange = failed_peer_witness  # type: ignore[method-assign]

    with pytest.raises(RunnerPreflightFailed) as error:
        ProductionRunnerBackend(provider).rehearse(
            match_id="kvm-blue-peer-witness", runner_names=("atlas", "borealis")
        )

    assert error.value.reason_code == "LOCAL_KVM_BLUE_PEER_ISOLATION_WITNESS_FAILED"


def test_local_kvm_treats_a_reachable_undeclared_egress_target_as_a_blue_failure(tmp_path: Path) -> None:
    """A denied target is safe; a successful external TCP connection is not."""
    provider, host = configured_provider(tmp_path)
    original_exchange = host.control_exchange

    def reachable_egress_witness(socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str:
        if payload.startswith("NETPROBE "):
            _, nonce, phase = payload.split()
            return (
                f"NETWORK_PROBE nonce={nonce} phase={phase} peer_denied=1 toy_http=0 "
                "alternate_denied=1 icmp_denied=1 egress_denied=0 egress_reason=tcp_reachable orchestrator_denied=1\n"
            )
        return original_exchange(socket_path, payload, timeout_seconds=timeout_seconds)

    host.control_exchange = reachable_egress_witness  # type: ignore[method-assign]

    with pytest.raises(RunnerPreflightFailed) as error:
        ProductionRunnerBackend(provider).rehearse(
            match_id="kvm-blue-egress-witness", runner_names=("atlas", "borealis")
        )

    assert error.value.reason_code == "LOCAL_KVM_BLUE_EGRESS_TCP_WITNESS_FAILED"


def test_local_kvm_labels_a_blue_default_route_as_egress_risk(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    original_exchange = host.control_exchange

    def default_route_witness(socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str:
        if payload.startswith("NETPROBE "):
            _, nonce, phase = payload.split()
            return (
                f"NETWORK_PROBE nonce={nonce} phase={phase} peer_denied=1 toy_http=0 "
                "alternate_denied=1 icmp_denied=1 egress_denied=0 egress_reason=default_route orchestrator_denied=1\n"
            )
        return original_exchange(socket_path, payload, timeout_seconds=timeout_seconds)

    host.control_exchange = default_route_witness  # type: ignore[method-assign]

    with pytest.raises(RunnerPreflightFailed) as error:
        ProductionRunnerBackend(provider).rehearse(
            match_id="kvm-blue-default-route-witness", runner_names=("atlas", "borealis")
        )

    assert error.value.reason_code == "LOCAL_KVM_BLUE_EGRESS_DEFAULT_ROUTE_WITNESS_FAILED"


def test_local_kvm_waits_for_identity_exit_before_checking_the_overlay(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision("kvm-teardown-wait", ("atlas", "borealis"))
    host.terminate_leaves_process_alive = True

    evidence = provider.destroy(runners[0])

    assert evidence.state is TeardownState.DESTROYED
    assert host.stop_order[:5] == ["term", "wait", "kill", "wait", "check"]
    assert host.wait_calls[0][2] > 0


def test_local_kvm_preserves_a_sanitized_qemu_startup_diagnostic_in_typed_failure(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    host.startup_failure = QemuStartupFailure("Could not access /dev/kvm: API_KEY=do-not-disclose")

    try:
        provider.provision("kvm-startup-diagnostic", ("atlas", "borealis"))
    except ProvisioningFailed as error:
        assert error.reason_code == "QEMU_EXITED_DURING_STARTUP"
        evidence = next(item for item in error.teardown_evidence if item.resource_id == "kvm-startup-diagnostic:atlas")
    else:  # pragma: no cover - explicit fail-closed contract
        raise AssertionError("a rejected QEMU must be a typed provisioning failure")

    assert "Could not access <path>" in evidence.evidence
    assert "do-not-disclose" not in evidence.evidence
    assert not (tmp_path / "runners" / "kvm-startup-diagnostic").exists()


def test_subprocess_host_bounds_and_sanitizes_qemu_stderr_on_immediate_exit(monkeypatch, tmp_path: Path) -> None:
    class ImmediatelyExitedProcess:
        pid = 4401
        stderr = io.BytesIO(b"qemu-system: Could not access /dev/kvm: OPENAI_API_KEY=do-not-disclose\\n")

        def poll(self) -> int:
            return 1

    def fake_popen(_argv, **kwargs):
        assert kwargs["stderr"] is subprocess.PIPE
        return ImmediatelyExitedProcess()

    monkeypatch.setattr("sandboxer_v0.local_kvm.subprocess.Popen", fake_popen)
    host = SubprocessLocalKvmHost()
    diagnostic = tmp_path / "runner" / "qemu.stderr"
    diagnostic.parent.mkdir()

    try:
        host.start(("qemu-system-x86_64",), stderr_path=diagnostic)
    except QemuStartupFailure as error:
        assert error.diagnostic.startswith("QEMU_STDERR:")
        assert "Could not access <path>" in error.diagnostic
        assert "do-not-disclose" not in error.diagnostic
    else:  # pragma: no cover - explicit fail-closed contract
        raise AssertionError("an immediate QEMU exit must preserve bounded diagnostics")

    assert diagnostic.stat().st_size <= 1024


def test_subprocess_host_returns_a_complete_netprobe_frame_without_waiting_for_eof(tmp_path: Path) -> None:
    """Virtio guest port reopening must not make the host lose a complete proof."""
    socket_path = tmp_path / "control.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    request_received = threading.Event()
    release_connection = threading.Event()
    request: list[bytes] = []
    response = (
        b"NETWORK_PROBE nonce=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        b"phase=blue peer_denied=1 toy_http=0 alternate_denied=1 icmp_denied=1 "
        b"egress_denied=1 egress_reason=blocked orchestrator_denied=1\n"
    )

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            request.append(connection.recv(4096))
            connection.sendall(response)
            request_received.set()
            release_connection.wait(timeout=2)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        actual = SubprocessLocalKvmHost().control_exchange(
            socket_path,
            "NETPROBE aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa blue\n",
            timeout_seconds=0.1,
        )
    finally:
        release_connection.set()
        thread.join(timeout=2)
        listener.close()

    assert request_received.is_set()
    assert request == [b"NETPROBE aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa blue\n"]


def test_subprocess_host_preserves_temporal_ready_fields_through_a_persistent_socket(tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    response = (
        b"READY nonce=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        b"uid=1001 boot_id=11111111-1111-1111-1111-111111111111 no_credentials=1 private_mounts=1 "
        b"route_after_setup=absent route_at_control=absent\n"
        b"PROBE_OK nonce=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa uid=1001 clock_epoch=1720000000\n"
    )
    release = threading.Event()

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(4096)
            connection.sendall(response)
            release.wait(timeout=2)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        actual = SubprocessLocalKvmHost().control_exchange(
            socket_path, "PROBE " + "a" * 64 + "\n", timeout_seconds=0.1
        )
        probe = parse_control(actual, "a" * 64, require_probe=True)
        assert probe.route_after_setup == probe.route_at_control == "absent"
    finally:
        release.set()
        thread.join(timeout=2)
        listener.close()
    assert actual == response.decode("ascii")


def test_subprocess_host_socket_witness_forks_and_removes_its_private_probe(monkeypatch, tmp_path: Path) -> None:
    host = SubprocessLocalKvmHost()
    monkeypatch.setattr("sandboxer_v0.local_kvm._drop_socket_witness_identity", lambda _user: None)
    directory = tmp_path / "runner"
    directory.mkdir()

    witness = host.verify_socket_access(directory, directory / "control.sock", qemu_user="sandboxer-runner")
    assert witness.stage is SocketWitnessStage.SUCCESS
    assert witness.errno is None
    assert list(directory.iterdir()) == []


def test_subprocess_host_socket_witness_reports_a_create_failure_by_category(monkeypatch, tmp_path: Path) -> None:
    host = SubprocessLocalKvmHost()
    monkeypatch.setattr("sandboxer_v0.local_kvm._drop_socket_witness_identity", lambda _user: None)
    def denied(*_args, **_kwargs):
        raise OSError(errno.EACCES, "sensitive host detail")
    monkeypatch.setattr("sandboxer_v0.local_kvm.os.open", denied)
    directory = tmp_path / "runner"
    directory.mkdir()

    witness = host.verify_socket_access(directory, directory / "control.sock", qemu_user="sandboxer-runner")
    assert witness.stage is SocketWitnessStage.CREATE_FAILED
    assert witness.errno == errno.EACCES


def test_local_kvm_quarantines_a_runner_while_its_ttl_timer_remains_active(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision("kvm-rehearsal-ttl-timer", ("atlas", "borealis"))
    host.ttl_timer_remains = True

    quarantined = provider.destroy(runners[0])

    assert quarantined.state is TeardownState.QUARANTINED
    assert quarantined.reason_code == "TTL_WATCHDOG_REMAINS"
    assert host.ttl_tokens

    host.ttl_timer_remains = False
    assert provider.reconcile(runners[0].runner_id).state is TeardownState.DESTROYED
    assert provider.destroy(runners[1]).state is TeardownState.DESTROYED


def test_local_kvm_accepts_an_already_absent_ttl_watchdog_as_cancelled(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision("kvm-rehearsal-ttl-absent", ("atlas", "borealis"))
    host.ttl_tokens.clear()

    assert provider.destroy(runners[0]).state is TeardownState.DESTROYED
    assert provider.destroy(runners[1]).state is TeardownState.DESTROYED


def test_systemd_watchdog_cancellation_requires_both_timer_and_service_to_be_inactive(monkeypatch) -> None:
    host = SubprocessLocalKvmHost()
    token = "sandboxer-ttl-fixture"
    calls: list[tuple[str, ...]] = []
    states = {f"{token}.timer": 3, f"{token}.service": 3}

    def fake_run(argv: tuple[str, ...], **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, states.get(argv[-1], 1), "", "")

    monkeypatch.setattr("sandboxer_v0.local_kvm.subprocess.run", fake_run)

    assert host.cancel_ttl(token) is True
    assert calls == [
        ("systemctl", "stop", f"{token}.timer", f"{token}.service"),
        ("systemctl", "is-active", "--quiet", f"{token}.timer"),
        ("systemctl", "is-active", "--quiet", f"{token}.service"),
    ]

    states[f"{token}.timer"] = 0
    assert host.cancel_ttl(token) is False


def test_local_kvm_rejects_a_mutated_base_before_creating_runner_artifacts(tmp_path: Path) -> None:
    provider, _host = configured_provider(tmp_path)
    provider.config.base_image.write_bytes(b"mutated-after-config")

    try:
        provider.provision("kvm-rehearsal-002", ("atlas", "borealis"))
    except RuntimeError as error:
        assert str(error) == "BASE_IMAGE_DIGEST_MISMATCH"
    else:  # pragma: no cover - makes the safety outcome explicit
        raise AssertionError("a changed base image must fail closed")

    assert not (tmp_path / "runners").exists()


def test_local_kvm_refuses_an_unprofiled_base_before_booting_a_guest(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    provider.config.base_profile.unlink()

    try:
        provider.provision("kvm-rehearsal-unprofiled", ("atlas", "borealis"))
    except RuntimeError as error:
        assert str(error) == "BASE_IMAGE_PROFILE_INVALID"
    else:  # pragma: no cover
        raise AssertionError("unprofiled images are not a local Runner base")

    assert not host.commands


def test_local_kvm_refuses_a_profile_with_undeclared_runtime_contract_fields(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    profile = json.loads(provider.config.base_profile.read_text())
    profile["undeclared_runtime_behavior"] = True
    provider.config.base_profile.write_text(json.dumps(profile))

    try:
        provider.provision("kvm-rehearsal-extra-profile", ("atlas", "borealis"))
    except RuntimeError as error:
        assert str(error) == "BASE_IMAGE_PROFILE_INVALID"
    else:  # pragma: no cover
        raise AssertionError("the local Runner profile must be exact")

    assert not host.commands


def test_local_kvm_network_observation_fails_closed_when_host_measurement_is_incomplete(tmp_path: Path, monkeypatch) -> None:
    provider, _host = configured_provider(tmp_path)

    runners = provider.provision("kvm-rehearsal-003", ("atlas", "borealis"))
    monkeypatch.setattr(
        provider,
        "_apply_network_phase",
        lambda _phase, _records: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )

    with pytest.raises(PreflightWitnessFailed, match="LOCAL_KVM_BLUE_NETWORK_TRANSITION_UNAVAILABLE"):
        provider.network_observation(Phase.BLUE, runners)
    for runner in runners:
        provider.destroy(runner)


def test_local_kvm_red_typed_witness_persists_only_current_allowlisted_serial_stages(tmp_path: Path, monkeypatch) -> None:
    provider, _host = configured_provider(tmp_path)
    runners = provider.provision("kvm-red-serial-evidence", ("atlas", "borealis"))
    record = provider._records[runners[0].runner_id]
    (record.root / "serial.log").write_text(
        "SANDBOXER_TOY_LISTENER_MISSING\nOPENAI_API_KEY=no\nSANDBOXER_NETPROBE_STAGE=attacker\n",
        encoding="ascii",
    )
    monkeypatch.setattr(
        provider, "_measure_network",
        lambda _records, _phase: (_ for _ in ()).throw(PreflightWitnessFailed("LOCAL_KVM_RED_LOCAL_TOY_SERVICE_WITNESS_FAILED")),
    )
    with pytest.raises(PreflightWitnessFailed, match="LOCAL_KVM_RED_LOCAL_TOY_SERVICE_WITNESS_FAILED"):
        provider.network_observation(Phase.RED, runners)
    artifact = tmp_path / "evidence" / f"{hashlib.sha256(runners[0].runner_id.encode()).hexdigest()[:16]}.serial-stages"
    assert artifact.read_text(encoding="ascii") == "SANDBOXER_TOY_LISTENER_MISSING\n"
    assert artifact.stat().st_mode & 0o777 == 0o600


def test_local_kvm_red_transition_failure_is_not_collapsed_to_missing_edge(tmp_path: Path, monkeypatch) -> None:
    provider, _host = configured_provider(tmp_path)
    runners = provider.provision("kvm-red-transition-failure", ("atlas", "borealis"))
    monkeypatch.setattr(
        provider,
        "_apply_network_phase",
        lambda _phase, _records: (_ for _ in ()).throw(RuntimeError("NETWORK_POLICY_APPLY_FAILED")),
    )

    with pytest.raises(PreflightWitnessFailed, match="LOCAL_KVM_RED_NETWORK_POLICY_APPLY_FAILED"):
        provider.network_observation(Phase.RED, runners)

    assert all(provider.destroy(runner).state is TeardownState.DESTROYED for runner in runners)


def test_destroying_one_runner_never_removes_its_opponents_live_artifacts(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision("kvm-rehearsal-004", ("atlas", "borealis"))

    first = provider.destroy(runners[0])

    assert first.state is TeardownState.DESTROYED
    assert host.process_alive(4101)
    assert (tmp_path / "runners" / "kvm-rehearsal-004" / "borealis" / "workspace.qcow2").exists()
    assert provider.destroy(runners[1]).state is TeardownState.DESTROYED
    assert not (tmp_path / "runners" / "kvm-rehearsal-004").exists()


def test_partial_provision_rolls_back_a_qemu_process_when_control_proof_is_invalid(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    host.control_exchange = lambda *args, **kwargs: "CONTROL_DENIED\n"  # type: ignore[method-assign]

    failure: ProvisioningFailed | None = None
    try:
        provider.provision("kvm-rehearsal-005", ("atlas", "borealis"))
    except ProvisioningFailed as error:
        assert str(error) == "CONTROL_PROBE_INVALID"
        failure = error
    else:  # pragma: no cover - makes the no-partial-Runner guarantee explicit
        raise AssertionError("invalid control proof must abort provisioning")

    assert not host.alive
    assert host.destroyed_cgroups
    assert failure is not None
    assert any(item.resource_id == "kvm-rehearsal-005:atlas" for item in failure.teardown_evidence)
    assert any(item.resource_id.startswith("kvm-rehearsal-005:sbb-") for item in failure.teardown_evidence)
    assert not (tmp_path / "runners" / "kvm-rehearsal-005").exists()


def test_partial_cleanup_treats_an_already_absent_namespace_as_destroyed(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    host.control_exchange = lambda *args, **kwargs: "CONTROL_DENIED\n"  # type: ignore[method-assign]
    host.namespace_delete_returns_nonzero = True
    host.cgroup_destroyable = False

    try:
        provider.provision("kvm-rehearsal-005b", ("atlas", "borealis"))
    except ProvisioningFailed as error:
        assert any(item.state is TeardownState.QUARANTINED for item in error.teardown_evidence)
    else:  # pragma: no cover
        raise AssertionError("invalid control proof must abort provisioning")

    # The namespace command is idempotently non-zero but the authoritative
    # namespace listing proves absence. Reconciliation must not quarantine it.
    host.cgroup_destroyable = True
    evidence = provider.reconcile("kvm-rehearsal-005b:atlas")

    assert evidence.state is TeardownState.DESTROYED
    assert not (tmp_path / "runners" / "kvm-rehearsal-005b").exists()


def test_partial_provision_retains_unproven_cleanup_until_reconciliation_succeeds(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    host.control_exchange = lambda *args, **kwargs: "CONTROL_DENIED\n"  # type: ignore[method-assign]
    host.cgroup_destroyable = False
    host.ttl_cancellable = False
    host.tap_cleanup_works = False

    try:
        provider.provision("kvm-rehearsal-005d", ("atlas", "borealis"))
    except ProvisioningFailed as error:
        partial = next(item for item in error.teardown_evidence if item.resource_id == "kvm-rehearsal-005d:atlas")
        assert partial.state is TeardownState.QUARANTINED
        assert {"TTL_WATCHDOG_REMAINS", "CGROUP_REMAINS", "TAP_REMAINS"} <= set(partial.evidence.split(";"))
    else:  # pragma: no cover
        raise AssertionError("invalid control proof must abort provisioning")

    root = tmp_path / "runners" / "kvm-rehearsal-005d"
    assert root.exists()
    assert host.ttl_tokens

    host.cgroup_destroyable = True
    host.ttl_cancellable = True
    host.tap_cleanup_works = True
    evidence = provider.reconcile("kvm-rehearsal-005d:atlas")

    assert evidence.state is TeardownState.DESTROYED
    assert not host.ttl_tokens
    assert not root.exists()


def test_local_kvm_waits_for_a_bounded_guest_control_bootstrap(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    host.control_failures = 1

    runners = provider.provision("kvm-rehearsal-006", ("atlas", "borealis"))

    assert len(host.control_requests) == 2
    assert all(provider.destroy(runner).state is TeardownState.DESTROYED for runner in runners)


def test_local_kvm_serial_failure_evidence_is_allowlisted_external_and_survives_teardown(tmp_path: Path, monkeypatch) -> None:
    provider, _host = configured_provider(tmp_path)
    runners = provider.provision("kvm-serial-evidence", ("atlas", "borealis"))
    record = provider._records[runners[0].runner_id]
    (record.root / "serial.log").write_text(
        "SANDBOXER_STAGE_SETUP\nOPENAI_API_KEY=do-not-disclose\n"
        "198.51.100.1 /private/path arbitrary text\nSANDBOXER_NETPROBE_STAGE=attacker_value\nSANDBOXER_CONTROL_FAILED\n",
        encoding="ascii",
    )
    monkeypatch.setattr(
        provider, "_control_probe",
        lambda _record: (_ for _ in ()).throw(PreflightWitnessFailed("LOCAL_KVM_CONTROL_INVALID_FRAME")),
    )

    with pytest.raises(PreflightWitnessFailed, match="LOCAL_KVM_CONTROL_INVALID_FRAME"):
        provider.probe(runners)

    artifact = tmp_path / "evidence" / f"{hashlib.sha256(runners[0].runner_id.encode()).hexdigest()[:16]}.serial-stages"
    assert artifact.read_text(encoding="ascii") == "SANDBOXER_STAGE_SETUP\nSANDBOXER_CONTROL_FAILED\n"
    assert artifact.stat().st_mode & 0o777 == 0o600
    teardown = provider.destroy(runners[0])
    assert "SERIAL_STAGE_EVIDENCE" in teardown.evidence
    assert "serial-stages" not in teardown.evidence
    provider.destroy(runners[1])
    assert artifact.exists()
    assert not record.root.exists()


def test_local_kvm_unknown_route_marker_is_diagnostic_when_active_proofs_pass(tmp_path: Path, monkeypatch) -> None:
    provider, _host = configured_provider(tmp_path)
    runners = provider.provision("kvm-unknown-route", ("atlas", "borealis"))
    record = provider._records[runners[0].runner_id]
    (record.root / "serial.log").write_text("SANDBOXER_ROUTE_MARKER_UNAVAILABLE\nsecret\n", encoding="ascii")
    probe = ControlProbe(
        "n", 1001, "11111111-1111-1111-1111-111111111111", True, True, "absent", "unknown",
            route_origin="unknown", dhcp_client="unknown", clock_epoch=1,
            toy_bootstrap="ready",
    )
    monkeypatch.setattr(provider, "_control_probe", lambda _record: probe)
    checks = provider.probe(runners)
    assert {check.name for check in checks}
    assert set(provider._route_diagnostics.values()) == {("absent", "unknown", "unknown")}
    assert not (tmp_path / "evidence" / f"{hashlib.sha256(runners[0].runner_id.encode()).hexdigest()[:16]}.serial-stages").exists()


@pytest.mark.parametrize("toy_state", ["root_failed", "exec_failed", "address_unavailable", "httpd_bind_exit", "exited_other"])
def test_local_kvm_surfaces_fixed_toy_lifecycle_outcomes(tmp_path: Path, monkeypatch, toy_state: str) -> None:
    provider, _host = configured_provider(tmp_path)
    runners = provider.provision(f"kvm-toy-{toy_state.replace('_', '-')}", ("atlas", "borealis"))
    probe = ControlProbe(
        "n", 1001, "11111111-1111-1111-1111-111111111111", True, True, "absent", "absent",
        route_origin="absent", dhcp_client="0", clock_epoch=1, toy_bootstrap=toy_state,
    )
    monkeypatch.setattr(provider, "_control_probe", lambda _record: probe)

    with pytest.raises(PreflightWitnessFailed, match=f"LOCAL_KVM_TOY_BOOTSTRAP_{toy_state.upper()}"):
        provider.probe(runners)

    assert all(provider.destroy(runner).state is TeardownState.DESTROYED for runner in runners)


def test_runtime_metadata_never_asks_cloud_init_to_write_the_runner_root(tmp_path: Path) -> None:
    provider, _host = configured_provider(tmp_path)
    runners = provider.provision("kvm-rehearsal-007", ("atlas", "borealis"))

    user_data = (tmp_path / "runners" / "kvm-rehearsal-007" / "atlas" / "user-data.yaml").read_text()
    metadata = (tmp_path / "runners" / "kvm-rehearsal-007" / "atlas" / "meta-data.yaml").read_text()

    assert user_data == "#cloud-config\n"
    assert "ip=10.77.0.11\n" in metadata
    assert "peer_ip=10.77.0.12\n" in metadata
    assert all(provider.destroy(runner).state is TeardownState.DESTROYED for runner in runners)


def test_blue_uses_two_separate_runner_namespaces_and_red_installs_deny_first_policy(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)
    runners = provider.provision("kvm-rehearsal-008", ("atlas", "borealis"))

    blue_namespaces = [command[-1] for command in host.commands if command[:3] == ("ip", "netns", "add") and command[-1].startswith("sbb-")]

    assert len(blue_namespaces) == 2
    assert len(set(blue_namespaces)) == 2
    qemu_commands = [command for command in host.commands if "qemu-system-x86_64" in command]
    assert {command[command.index("exec") + 1] for command in qemu_commands} == set(blue_namespaces)
    assert not any("master" in command and command[-1].startswith("br-") for command in host.commands)

    red = provider.network_observation(Phase.RED, runners)

    assert red.direct_egress is False
    assert red.edges == ArenaNetworkPolicy("kvm-rehearsal-008", "atlas", "borealis").expected(Phase.RED).edges
    red_namespace = provider._records[runners[0].runner_id].red_namespace
    assert ("ip", "-n", red_namespace, "route", "show", "default") in host.commands
    assert any("policy drop" in rules and "tcp dport 8080" in rules for rules in host.inputs)
    assert sum("netns" in command and any(item.startswith("tap-") for item in command) for command in host.commands) >= 2
    assert all(provider.destroy(runner).state is TeardownState.DESTROYED for runner in runners)
