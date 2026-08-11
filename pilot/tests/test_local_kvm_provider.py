from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sandboxer_v0.arena_safety import Phase, TeardownState
from sandboxer_v0.local_kvm import CommandResult, LocalKvmConfig, LocalKvmRunnerProvider
from sandboxer_v0.runner_backend import ProductionRunnerBackend, ProvisioningFailed


class RecordingKvmHost:
    """External-process fixture at the LocalKvmRunnerProvider boundary."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.inputs: list[str] = []
        self.alive: set[int] = set()
        self.next_pid = 4100
        self.control_requests: list[str] = []
        self.control_failures = 0
        self.destroyed_cgroups: list[str] = []
        self.ttl_tokens: list[object] = []
        self.namespaces: set[str] = set()
        self.taps: dict[str, str] = {}
        self.namespace_delete_returns_nonzero = False
        self.cgroup_destroyable = True
        self.ttl_cancellable = True
        self.tap_cleanup_works = True

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
        if argv[:3] == ("qemu-img", "check", "--output=json"):
            return CommandResult(0, '{"filename":"overlay","format":"qcow2"}', "")
        if "addr" in argv and "show" in argv and "-j" in argv:
            return CommandResult(0, "[]", "")
        if "route" in argv and "default" in argv:
            return CommandResult(0, "", "")
        if "nft" in argv and "list" in argv:
            return CommandResult(0, self.inputs[-1] if self.inputs else "", "")
        return CommandResult(0, "", "")

    def start(self, argv: tuple[str, ...]) -> int:
        self.commands.append(argv)
        pid = self.next_pid
        self.alive.add(pid)
        self.next_pid += 1
        return pid

    def control_exchange(self, socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str:
        del socket_path, timeout_seconds
        if self.control_failures:
            self.control_failures -= 1
            raise TimeoutError("guest control is not ready")
        self.control_requests.append(payload)
        if payload.startswith("NETPROBE "):
            _, nonce, phase = payload.split()
            if phase == "blue":
                return f"NETWORK_PROBE nonce={nonce} phase=blue peer_denied=1 toy_http=0 alternate_denied=1 icmp_denied=1 egress_denied=1 orchestrator_denied=1\n"
            return f"NETWORK_PROBE nonce={nonce} phase=red peer_denied=0 toy_http=1 alternate_denied=1 icmp_denied=1 egress_denied=1 orchestrator_denied=1\n"
        runner_number = len(self.control_requests)
        boot_id = "11111111-1111-1111-1111-111111111111" if runner_number == 1 else "22222222-2222-2222-2222-222222222222"
        nonce = payload.split()[1]
        return f"READY nonce={nonce} uid=1001 boot_id={boot_id} no_credentials=1 private_mounts=1\nPROBE_OK nonce={nonce} uid=1001 clock_epoch=1720000000\n"

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
        if not self.ttl_cancellable:
            return False
        self.ttl_tokens.remove(token)
        return True

    def terminate(self, pid: int) -> None:
        self.alive.discard(pid)

    def kill(self, pid: int) -> None:
        self.alive.discard(pid)

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


def test_local_kvm_network_observation_fails_closed_when_host_measurement_is_incomplete(tmp_path: Path) -> None:
    provider, host = configured_provider(tmp_path)

    runners = provider.provision("kvm-rehearsal-003", ("atlas", "borealis"))
    host.run = lambda argv, **kwargs: CommandResult(1, "", "unavailable")  # type: ignore[method-assign]

    observation = provider.network_observation(Phase.BLUE, runners)

    assert observation.edges == frozenset()
    assert observation.direct_egress is True
    assert observation.public_ingress is True
    assert observation.orchestrator_reachable is True
    for runner in runners:
        provider.destroy(runner)


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


def test_runtime_metadata_never_asks_cloud_init_to_write_the_runner_root(tmp_path: Path) -> None:
    provider, _host = configured_provider(tmp_path)
    runners = provider.provision("kvm-rehearsal-007", ("atlas", "borealis"))

    user_data = (tmp_path / "runners" / "kvm-rehearsal-007" / "atlas" / "user-data.yaml").read_text()

    assert user_data == "#cloud-config\n"
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
    assert any("policy drop" in rules and "tcp dport 8080" in rules for rules in host.inputs)
    assert sum("netns" in command and any(item.startswith("tap-") for item in command) for command in host.commands) >= 2
    assert all(provider.destroy(runner).state is TeardownState.DESTROYED for runner in runners)
