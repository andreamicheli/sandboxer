"""Local snapshot-backed KVM adapter for a controlled Runner rehearsal.

This adapter is intentionally conservative.  It is a real local runtime, but
its ``production_ready`` flag stays false: a successful rehearsal proves only
the configured containment checks, not resistance to a hostile workload.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .arena_safety import NetworkObservation, Phase, TeardownEvidence, TeardownState
from .local_kvm_control import ControlProbe, ControlReady, NetworkProof, parse_control, parse_network_proof
from .runner_backend import PreflightCheck, ProvisioningFailed, RunnerHandle


_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_MAX_CONTROL_RESPONSE = 4096


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class LocalKvmHost(Protocol):
    """Small host-privileged port; every command is argv-only, never a shell."""

    def run(
        self, argv: tuple[str, ...], *, input_text: str | None = None, timeout_seconds: float = 10
    ) -> CommandResult: ...

    def start(self, argv: tuple[str, ...]) -> int: ...

    def control_exchange(self, socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str: ...

    def process_alive(self, pid: int) -> bool: ...

    def process_identity(self, pid: int) -> str | None: ...

    def cgroup_contains(self, cgroup: str, pid: int) -> bool: ...

    def cgroup_limited(self, cgroup: str, pid: int, memory_bytes: int, cpu_max: str, pids_max: int) -> bool: ...

    def arm_ttl(self, cgroup: str, identity: str, seconds: int) -> object: ...

    def cancel_ttl(self, token: object) -> None: ...

    def terminate(self, pid: int) -> None: ...

    def kill(self, pid: int) -> None: ...

    def create_cgroup(self, name: str, *, memory_max_bytes: int, cpu_max: str, pids_max: int) -> str: ...

    def attach_to_cgroup(self, cgroup: str, pid: int) -> None: ...

    def destroy_cgroup(self, cgroup: str) -> bool: ...


class SubprocessLocalKvmHost:
    """The privileged local implementation; invoke only from the Orchestrator."""

    def __init__(self, *, cgroup_root: Path = Path("/sys/fs/cgroup/sandboxer")) -> None:
        self._cgroup_root = cgroup_root

    def run(
        self, argv: tuple[str, ...], *, input_text: str | None = None, timeout_seconds: float = 10
    ) -> CommandResult:
        completed = subprocess.run(
            argv, input=input_text, text=True, capture_output=True, timeout=timeout_seconds, check=False
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def start(self, argv: tuple[str, ...]) -> int:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Do not mistake an immediately rejected QEMU configuration for a live
        # Runner.  Longer guest boot readiness is checked via virtio control.
        time.sleep(0.1)
        if process.poll() is not None:
            raise RuntimeError("QEMU_EXITED_DURING_STARTUP")
        return process.pid

    def control_exchange(self, socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(os.fspath(socket_path))
            client.sendall(payload.encode("ascii"))
            chunks: list[bytes] = []
            received = 0
            while received <= _MAX_CONTROL_RESPONSE:
                chunk = client.recv(min(1024, _MAX_CONTROL_RESPONSE + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if b"PROBE_OK" in b"".join(chunks):
                    break
            if received > _MAX_CONTROL_RESPONSE:
                raise RuntimeError("CONTROL_RESPONSE_TOO_LARGE")
        return b"".join(chunks).decode("ascii", errors="strict")

    def process_alive(self, pid: int) -> bool:
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
        except (FileNotFoundError, IndexError):
            return False
        return state != "Z"

    def process_identity(self, pid: int) -> str | None:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            return f"{pid}:{fields[21]}" if fields[2] != "Z" else None
        except (FileNotFoundError, IndexError):
            return None

    def cgroup_contains(self, cgroup: str, pid: int) -> bool:
        return str(pid) in Path(cgroup, "cgroup.procs").read_text(encoding="ascii").split()

    def cgroup_limited(self, cgroup: str, pid: int, memory_bytes: int, cpu_max: str, pids_max: int) -> bool:
        return self.cgroup_contains(cgroup, pid) and (
            Path(cgroup, "memory.max").read_text().strip() == str(memory_bytes)
            and Path(cgroup, "cpu.max").read_text().strip() == cpu_max
            and Path(cgroup, "pids.max").read_text().strip() == str(pids_max)
        )

    def arm_ttl(self, cgroup: str, identity: str, seconds: int) -> object:
        pid, starttime = identity.split(":", 1)
        unit = f"sandboxer-ttl-{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
        # A transient systemd unit is independent of the Orchestrator process:
        # it survives a controller crash and kills only the verified cgroup if
        # the PID still has the original start time.
        script = (
            f"sleep {seconds}; test -r /proc/{pid}/stat || exit 0; "
            f"test \"$(awk '{{print $22}}' /proc/{pid}/stat)\" = \"{starttime}\" || exit 0; "
            f"printf 1 > {cgroup}/cgroup.kill"
        )
        completed = subprocess.run(
            ("systemd-run", "--unit", unit, "--collect", "--service-type=oneshot", "/bin/sh", "-ec", script),
            text=True, capture_output=True, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("TTL_WATCHDOG_UNAVAILABLE")
        return unit

    def cancel_ttl(self, token: object) -> None:
        if isinstance(token, str):
            subprocess.run(("systemctl", "stop", token), text=True, capture_output=True, check=False)

    def terminate(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def kill(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def create_cgroup(self, name: str, *, memory_max_bytes: int, cpu_max: str, pids_max: int) -> str:
        self._cgroup_root.mkdir(parents=True, exist_ok=True)
        # Controllers must be enabled in the Sandboxer-owned parent before
        # they can be applied to child Runner cgroups on cgroup v2.
        (self._cgroup_root / "cgroup.subtree_control").write_text("+memory +cpu +pids", encoding="ascii")
        path = self._cgroup_root / name
        path.mkdir(parents=True, exist_ok=False)
        (path / "memory.max").write_text(str(memory_max_bytes), encoding="ascii")
        (path / "cpu.max").write_text(cpu_max, encoding="ascii")
        (path / "pids.max").write_text(str(pids_max), encoding="ascii")
        return os.fspath(path)

    def attach_to_cgroup(self, cgroup: str, pid: int) -> None:
        Path(cgroup, "cgroup.procs").write_text(str(pid), encoding="ascii")

    def destroy_cgroup(self, cgroup: str) -> bool:
        path = Path(cgroup)
        try:
            path.rmdir()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True


@dataclass(frozen=True)
class LocalKvmConfig:
    runner_root: Path
    base_image: Path
    base_image_sha256: str
    base_profile: Path
    qemu_user: str
    qemu_uid: int
    toy_service_port: int
    memory_mib: int = 512
    vcpus: int = 1
    pids_max: int = 128
    ttl_seconds: int = 300
    qemu_binary: str = "qemu-system-x86_64"
    qemu_img_binary: str = "qemu-img"
    cloud_localds_binary: str = "cloud-localds"

    def __post_init__(self) -> None:
        if not self.runner_root.is_absolute() or not self.base_image.is_absolute() or not self.base_profile.is_absolute():
            raise ValueError("local KVM paths must be absolute")
        if not re.fullmatch(r"[0-9a-f]{64}", self.base_image_sha256):
            raise ValueError("base image requires a SHA-256 digest")
        if self.memory_mib < 256 or self.vcpus != 1 or not 1 <= self.toy_service_port <= 65535:
            raise ValueError("unsafe local KVM resource configuration")
        if self.pids_max < 32 or self.ttl_seconds < 30:
            raise ValueError("local KVM limits must be bounded")


@dataclass
class _RunnerRecord:
    handle: RunnerHandle
    match_id: str
    blue_namespace: str
    red_namespace: str
    red_bridge: str
    tap: str
    root: Path
    workspace: Path
    control_socket: Path
    pid: int
    process_identity: str
    cgroup: str
    ttl_token: object
    nonce: str
    deadline: float
    phase: Phase = Phase.BLUE


class LocalKvmRunnerProvider:
    """A disposable, no-egress local KVM provider with an external control plane."""

    simulated_fixture = False
    # Local smoke tests cannot establish adversarial escape resistance.
    production_ready = False

    def __init__(self, config: LocalKvmConfig, host: LocalKvmHost | None = None) -> None:
        self.config = config
        self._host = host or SubprocessLocalKvmHost()
        self._records: dict[str, _RunnerRecord] = {}
        self._terminal: dict[str, TeardownEvidence] = {}
        self._partials: dict[str, tuple[Path, tuple[str, ...]]] = {}

    def provision(self, match_id: str, names: tuple[str, str]) -> tuple[RunnerHandle, RunnerHandle]:
        self._validate_identity(match_id, names)
        self._verify_base_image()
        match_root = self.config.runner_root / match_id
        if match_root.exists():
            raise RuntimeError("MATCH_ARTIFACTS_ALREADY_EXIST")
        match_root.mkdir(parents=True, mode=0o711)
        red_namespace, red_bridge = self._network_names(match_id)
        records: list[_RunnerRecord] = []
        blue_namespaces: list[str] = []
        try:
            self._run(("ip", "netns", "add", red_namespace), "NETWORK_NAMESPACE_CREATE_FAILED")
            self._run(("ip", "-n", red_namespace, "link", "add", red_bridge, "type", "bridge"), "BRIDGE_CREATE_FAILED")
            self._run(("ip", "-n", red_namespace, "link", "set", "lo", "up"), "LOOPBACK_ENABLE_FAILED")
            self._run(("ip", "-n", red_namespace, "link", "set", red_bridge, "up"), "BRIDGE_ENABLE_FAILED")
            self._install_red_policy(red_namespace, "")
            for index, name in enumerate(names, start=11):
                blue_namespace = self._blue_namespace(match_id, name)
                blue_namespaces.append(blue_namespace)
                self._run(("ip", "netns", "add", blue_namespace), "BLUE_NAMESPACE_CREATE_FAILED")
                self._run(("ip", "-n", blue_namespace, "link", "set", "lo", "up"), "BLUE_LOOPBACK_ENABLE_FAILED")
                record = self._provision_runner(match_id, name, index, match_root, blue_namespace, red_namespace, red_bridge)
                records.append(record)
                self._records[record.handle.runner_id] = record
            self._apply_network_phase(Phase.BLUE, records)
        except Exception as error:
            evidence: list[TeardownEvidence] = []
            for record in records:
                evidence.append(self._destroy_record(record, "PROVISION_ROLLBACK"))
            for namespace in (*blue_namespaces, red_namespace):
                deleted = self._run(("ip", "netns", "del", namespace), "", allow_failure=True)
                listed = self._run(("ip", "netns", "list"), "", allow_failure=True)
                remains = listed.returncode != 0 or any(line.split(maxsplit=1)[0] == namespace for line in listed.stdout.splitlines() if line.strip())
                evidence.append(TeardownEvidence(
                    f"{match_id}:{namespace}",
                    TeardownState.QUARANTINED if deleted.returncode != 0 or remains else TeardownState.DESTROYED,
                    "namespace deletion verified" if deleted.returncode == 0 and not remains else "namespace cleanup unverified",
                    None if deleted.returncode == 0 and not remains else "NAMESPACE_REMAINS",
                ))
            if all(item.state is TeardownState.DESTROYED for item in evidence):
                shutil.rmtree(match_root, ignore_errors=True)
            if isinstance(error, ProvisioningFailed):
                evidence.extend(error.teardown_evidence)
            raise ProvisioningFailed(str(error), tuple(evidence)) from error
        return records[0].handle, records[1].handle

    def probe(self, runners: tuple[RunnerHandle, RunnerHandle]) -> tuple[PreflightCheck, ...]:
        records = self._require_records(runners)
        self._enforce_ttl(records)
        responses = [self._control_probe(record) for record in records]
        network = self._measure_network(records, Phase.BLUE)
        all_alive = all(self._host.process_alive(record.pid) for record in records)
        unique_boot_ids = len({record.handle.kernel_id for record in records}) == 2
        now = time.monotonic()
        return (
            PreflightCheck("distinct_kernels", unique_boot_ids, "KERNEL_ISOLATION_LOST"),
            PreflightCheck("orchestrator_unreachable", not network.orchestrator_reachable, "ORCHESTRATOR_REACHABLE"),
            PreflightCheck("no_host_mounts", all(item.private_mounts for item in responses), "HOST_MOUNT_DETECTED"),
            PreflightCheck("no_credentials", all(item.no_credentials for item in responses), "CREDENTIAL_EXPOSURE"),
            PreflightCheck("telemetry_egress", all(item.nonce == record.nonce for item, record in zip(responses, records)), "TELEMETRY_EGRESS_UNAVAILABLE"),
            PreflightCheck("clock", all(item.clock_epoch > 0 for item in responses), "CLOCK_UNVERIFIED"),
            PreflightCheck("ttl", all(record.deadline > now for record in records), "TTL_UNVERIFIED"),
            PreflightCheck("health", all_alive and all(item.uid == 1001 for item in responses) and all(self._host.cgroup_limited(record.cgroup, record.pid, self.config.memory_mib * 1024 * 1024, f"{self.config.vcpus * 100000} 100000", self.config.pids_max) for record in records), "RUNNER_HEALTH_UNVERIFIED"),
            PreflightCheck("cleanup_capability", all(self._host.cgroup_contains(record.cgroup, record.pid) for record in records), "CLEANUP_UNAVAILABLE"),
        )

    def network_observation(
        self, phase: Phase, runners: tuple[RunnerHandle, RunnerHandle]
    ) -> NetworkObservation:
        records = self._require_records(runners)
        try:
            self._enforce_ttl(records)
            self._apply_network_phase(phase, records)
            return self._measure_network(records, phase)
        except Exception as error:
            # An unavailable witness is unsafe by definition; never infer safety.
            return NetworkObservation(frozenset(), True, True, True)

    def destroy(self, runner: RunnerHandle) -> TeardownEvidence:
        existing = self._terminal.get(runner.runner_id)
        if existing is not None:
            return existing
        record = self._records.get(runner.runner_id)
        if record is None:
            evidence = TeardownEvidence(runner.runner_id, TeardownState.DESTROYED, "no local Runner artifact remained")
        else:
            evidence = self._destroy_record(record, None)
        self._terminal[runner.runner_id] = evidence
        return evidence

    def quarantine(self, runner: RunnerHandle, reason_code: str) -> TeardownEvidence:
        record = self._records.get(runner.runner_id)
        if record is None:
            evidence = TeardownEvidence(runner.runner_id, TeardownState.QUARANTINED, "local Runner state unavailable", reason_code)
        else:
            result = self._destroy_record(record, reason_code)
            evidence = result
        self._terminal[runner.runner_id] = evidence
        return evidence

    def reconcile(self, runner_id: str) -> TeardownEvidence:
        existing = self._terminal.get(runner_id)
        if existing is not None and existing.state is TeardownState.DESTROYED:
            return existing
        record = self._records.get(runner_id)
        partial = self._partials.get(runner_id)
        if partial is not None:
            root, namespaces = partial
            failures = []
            for namespace in namespaces:
                deleted = self._run(("ip", "netns", "del", namespace), "", allow_failure=True)
                if deleted.returncode != 0:
                    failures.append(namespace)
            if not failures:
                shutil.rmtree(root, ignore_errors=True)
                self._partials.pop(runner_id, None)
                return TeardownEvidence(runner_id, TeardownState.DESTROYED, "partial Runner artifacts reconciled")
            return TeardownEvidence(runner_id, TeardownState.QUARANTINED, "partial Runner remains", "RECONCILIATION_UNAVAILABLE")
        if record is None:
            return TeardownEvidence(runner_id, TeardownState.QUARANTINED, "cannot prove local artifact absence", "RECONCILIATION_UNAVAILABLE")
        evidence = self._destroy_record(record, "RECONCILIATION")
        self._terminal[runner_id] = evidence
        return evidence

    def _provision_runner(
        self, match_id: str, name: str, host_octet: int, match_root: Path, blue_namespace: str, red_namespace: str, red_bridge: str
    ) -> _RunnerRecord:
        runner_id = f"{match_id}:{name}"
        root = match_root / name
        root.mkdir(mode=0o700)
        os.chown(root, self.config.qemu_uid, -1)
        workspace = root / "workspace.qcow2"
        seed = root / "seed.iso"
        control_socket = root / "control.sock"
        nonce = secrets.token_hex(32)
        tap = self._tap_name(match_id, name)
        pid: int | None = None
        cgroup: str | None = None
        try:
            self._write_cloud_init(root, match_id, name, host_octet, nonce)
            self._run(
                (self.config.qemu_img_binary, "create", "-q", "-f", "qcow2", os.fspath(workspace), "64M"),
                "WORKSPACE_CREATE_FAILED",
            )
            os.chown(workspace, self.config.qemu_uid, -1)
            self._run(
                (
                    self.config.cloud_localds_binary,
                    f"--network-config={root / 'network-config.yaml'}",
                    os.fspath(seed),
                    os.fspath(root / "user-data.yaml"),
                    os.fspath(root / "meta-data.yaml"),
                ),
                "SEED_CREATE_FAILED",
            )
            os.chown(seed, self.config.qemu_uid, -1)
            self._run(("ip", "netns", "exec", blue_namespace, "ip", "tuntap", "add", "dev", tap, "mode", "tap", "user", str(self.config.qemu_uid)), "TAP_CREATE_FAILED")
            self._run(("ip", "-n", blue_namespace, "link", "set", tap, "up"), "TAP_ENABLE_FAILED")
            pid = self._host.start(self._qemu_command(blue_namespace, tap, workspace, seed, control_socket, root / "serial.log"))
            process_identity = self._host.process_identity(pid)
            if process_identity is None:
                raise RuntimeError("QEMU_PID_UNVERIFIED")
            cgroup = self._host.create_cgroup(
                f"sandboxer-{hashlib.sha256(runner_id.encode()).hexdigest()[:12]}-{nonce[:8]}",
                memory_max_bytes=self.config.memory_mib * 1024 * 1024,
                cpu_max=f"{self.config.vcpus * 100000} 100000",
                pids_max=self.config.pids_max,
            )
            self._host.attach_to_cgroup(cgroup, pid)
            if not self._host.cgroup_contains(cgroup, pid):
                raise RuntimeError("CGROUP_MEMBERSHIP_UNVERIFIED")
            ttl_token = self._host.arm_ttl(cgroup, process_identity, self.config.ttl_seconds)
            response = self._control_exchange(control_socket, nonce)
            ready = self._parse_control(response, nonce, require_probe=False)
            handle = RunnerHandle(runner_id, name, ready.boot_id, ready.uid == 1001, True, True)
            return _RunnerRecord(handle, match_id, blue_namespace, red_namespace, red_bridge, tap, root, workspace, control_socket, pid, process_identity, cgroup, ttl_token, nonce, time.monotonic() + self.config.ttl_seconds)
        except Exception as error:
            if pid is not None:
                self._stop_pid(pid)
            if cgroup is not None:
                self._host.destroy_cgroup(cgroup)
            self._run_quiet(("ip", "-n", blue_namespace, "link", "del", tap))
            state = TeardownState.DESTROYED if pid is None or not self._host.process_alive(pid) else TeardownState.QUARANTINED
            if state is TeardownState.DESTROYED:
                shutil.rmtree(root, ignore_errors=True)
            else:
                self._partials[runner_id] = (root, (blue_namespace, red_namespace))
            raise ProvisioningFailed(
                str(error),
                (TeardownEvidence(runner_id, state, "partial Runner provisioning rollback attempted", str(error)),),
            ) from error

    def _qemu_command(
        self, namespace: str, tap: str, workspace: Path, seed: Path, control_socket: Path, serial_log: Path
    ) -> tuple[str, ...]:
        return (
            "ip", "netns", "exec", namespace,
            "setpriv", f"--reuid={self.config.qemu_user}", f"--regid={self.config.qemu_user}", "--init-groups",
            self.config.qemu_binary,
            "-enable-kvm", "-cpu", "host", "-m", str(self.config.memory_mib), "-smp", str(self.config.vcpus),
            "-nodefaults", "-display", "none", "-monitor", "none", "-serial", f"file:{serial_log}", "-no-reboot",
            "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
            "-drive", f"file={self.config.base_image},if=virtio,format=qcow2,readonly=on,cache=none",
            "-drive", f"file={workspace},if=virtio,format=qcow2,cache=none,discard=unmap",
            "-drive", f"file={seed},media=cdrom,readonly=on", "-nic", "none",
            "-netdev", f"tap,id=arena0,ifname={tap},script=no,downscript=no", "-device", "virtio-net-pci,netdev=arena0",
            "-device", "virtio-serial-pci", "-chardev", f"socket,id=control,path={control_socket},server=on,wait=off",
            "-device", "virtserialport,chardev=control,name=org.sandboxer.control",
        )

    def _apply_network_phase(self, phase: Phase, records: list[_RunnerRecord]) -> None:
        if phase is Phase.BLUE:
            # Red is dismantled before either tap returns to an isolated Blue
            # namespace.  There is no bridge shared by the two Runners here.
            for record in records:
                if record.phase is Phase.RED:
                    self._run(("ip", "-n", record.red_namespace, "link", "set", record.tap, "nomaster"), "BLUE_DETACH_FAILED")
                    self._run(("ip", "-n", record.red_namespace, "link", "set", record.tap, "netns", record.blue_namespace), "BLUE_MOVE_FAILED")
                    self._run(("ip", "-n", record.blue_namespace, "link", "set", record.tap, "up"), "BLUE_TAP_ENABLE_FAILED")
        else:
            left, right = records
            port = self.config.toy_service_port
            rules = (
                f'add rule bridge sandboxer forward iifname "{left.tap}" oifname "{right.tap}" ether type arp accept\n'
                f'add rule bridge sandboxer forward iifname "{right.tap}" oifname "{left.tap}" ether type arp accept\n'
                f'add rule bridge sandboxer forward iifname "{left.tap}" oifname "{right.tap}" ether type ip ip protocol tcp tcp dport {port} accept\n'
                f'add rule bridge sandboxer forward iifname "{right.tap}" oifname "{left.tap}" ether type ip ip protocol tcp tcp dport {port} accept\n'
                "add rule bridge sandboxer forward ct state established,related accept\n"
            )
            # Install a complete deny-by-default Red ruleset before the first
            # tap can join the shared bridge. nft applies this as one transaction.
            self._install_red_policy(left.red_namespace, rules)
            for record in records:
                if record.phase is not Phase.RED:
                    self._run(("ip", "-n", record.blue_namespace, "link", "set", record.tap, "netns", record.red_namespace), "RED_MOVE_FAILED")
                    self._run(("ip", "-n", record.red_namespace, "link", "set", record.tap, "master", record.red_bridge), "RED_ATTACH_FAILED")
                    self._run(("ip", "-n", record.red_namespace, "link", "set", record.tap, "up"), "RED_TAP_ENABLE_FAILED")
        for record in records:
            record.phase = phase

    def _install_red_policy(self, namespace: str, forward_rules: str) -> None:
        # There is no delete-then-open gap: nft's complete table replacement is
        # applied before any Runner tap joins the Red bridge.
        exists = self._run(
            ("ip", "netns", "exec", namespace, "nft", "list", "table", "bridge", "sandboxer"),
            "", allow_failure=True,
        )
        prefix = "flush table bridge sandboxer\n" if exists.returncode == 0 else "add table bridge sandboxer\n"
        rules = prefix + "add chain bridge sandboxer forward { type filter hook forward priority 0; policy drop; }\n" + forward_rules
        self._run(("ip", "netns", "exec", namespace, "nft", "-f", "-"), "NETWORK_POLICY_APPLY_FAILED", input_text=rules)

    def _measure_network(self, records: list[_RunnerRecord], phase: Phase) -> NetworkObservation:
        if phase is Phase.BLUE:
            # Separate Linux namespaces are the authoritative Blue boundary.
            routes = [self._run(("ip", "-n", record.blue_namespace, "ip", "route", "show", "default"), "NETWORK_WITNESS_UNAVAILABLE") for record in records]
            if any(item.stdout.strip() for item in routes):
                return NetworkObservation(frozenset(), True, True, True)
            proofs = [self._network_proof(record, phase) for record in records]
            if not all(proof.peer_denied and proof.alternate_denied and proof.icmp_denied and proof.egress_denied and proof.orchestrator_denied and not proof.toy_http for proof in proofs):
                return NetworkObservation(frozenset(), True, True, True)
            return NetworkObservation(
                frozenset({f"{records[0].handle.name}:private-a", f"{records[1].handle.name}:private-b"}),
                False, False, False,
            )
        namespace, bridge = records[0].red_namespace, records[0].red_bridge
        nft = self._run(("ip", "netns", "exec", namespace, "nft", "list", "table", "bridge", "sandboxer"), "NETWORK_WITNESS_UNAVAILABLE")
        addresses = self._run(("ip", "-n", namespace, "-j", "addr", "show", "dev", bridge), "NETWORK_WITNESS_UNAVAILABLE")
        routes = self._run(("ip", "-n", namespace, "ip", "route", "show", "default"), "NETWORK_WITNESS_UNAVAILABLE")
        try:
            bridge_addresses = json.loads(addresses.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("NETWORK_WITNESS_UNAVAILABLE") from error
        public_ingress = any(item.get("addr_info") for item in bridge_addresses)
        direct_egress = bool(routes.stdout.strip())
        expected_rules = ["policy drop", "ether type arp", f"tcp dport {self.config.toy_service_port}", records[0].tap, records[1].tap]
        rule_set_ok = all(rule in nft.stdout for rule in expected_rules)
        if not rule_set_ok:
            return NetworkObservation(frozenset(), True, True, True)
        proofs = [self._network_proof(record, phase) for record in records]
        if not all(not proof.peer_denied and proof.toy_http and proof.alternate_denied and proof.icmp_denied and proof.egress_denied and proof.orchestrator_denied for proof in proofs):
            return NetworkObservation(frozenset(), True, True, True)
        private = frozenset({f"{records[0].handle.name}:private-a", f"{records[1].handle.name}:private-b"})
        edges = private | frozenset({f"{records[0].handle.name}->toy-service", f"{records[1].handle.name}->toy-service"})
        # The namespace has only a bridge and unnumbered taps.  The Unix control
        # socket is not an IP path and cannot be reached by either guest.
        return NetworkObservation(edges, direct_egress, public_ingress, False)

    def _control_probe(self, record: _RunnerRecord) -> ControlProbe:
        return self._parse_control(self._control_exchange(record.control_socket, record.nonce), record.nonce, require_probe=True)

    def _control_exchange(self, socket_path: Path, nonce: str) -> str:
        deadline = time.monotonic() + 30
        last_error: OSError | TimeoutError | None = None
        while time.monotonic() < deadline:
            try:
                return self._host.control_exchange(socket_path, f"PROBE {nonce}\n", timeout_seconds=5)
            except (FileNotFoundError, ConnectionRefusedError, TimeoutError, socket.timeout) as error:
                last_error = error
                time.sleep(0.25)
        raise RuntimeError("CONTROL_BOOTSTRAP_TIMEOUT") from last_error

    def _network_proof(self, record: _RunnerRecord, phase: Phase) -> NetworkProof:
        response = self._host.control_exchange(
            record.control_socket, f"NETPROBE {record.nonce} {phase.value}\n", timeout_seconds=5
        )
        return parse_network_proof(response, record.nonce, phase.value)

    def _parse_control(self, response: str, nonce: str, *, require_probe: bool) -> ControlReady | ControlProbe:
        if len(response.encode("ascii", errors="ignore")) > _MAX_CONTROL_RESPONSE:
            raise RuntimeError("CONTROL_RESPONSE_TOO_LARGE")
        return parse_control(response, nonce, require_probe=require_probe)

    def _destroy_record(self, record: _RunnerRecord, reason_code: str | None) -> TeardownEvidence:
        failures: list[str] = []
        self._host.cancel_ttl(record.ttl_token)
        self._stop_pid(record.pid)
        if self._host.process_identity(record.pid) == record.process_identity:
            failures.append("QEMU_PROCESS_SURVIVED")
        check = self._run((self.config.qemu_img_binary, "check", "--output=json", os.fspath(record.workspace)), "", allow_failure=True)
        if check.returncode != 0:
            failures.append("OVERLAY_INTEGRITY_UNVERIFIED")
        if not self._host.destroy_cgroup(record.cgroup):
            failures.append("CGROUP_REMAINS")
        blue_deleted = self._run(("ip", "netns", "del", record.blue_namespace), "", allow_failure=True)
        blue_listed = self._run(("ip", "netns", "list"), "", allow_failure=True)
        blue_remains = blue_listed.returncode != 0 or any(
            line.split(maxsplit=1)[0] == record.blue_namespace for line in blue_listed.stdout.splitlines() if line.strip()
        )
        if blue_deleted.returncode != 0 or blue_remains:
            failures.append("BLUE_NAMESPACE_REMAINS")
        another_runner_remains = any(
            candidate.handle.runner_id != record.handle.runner_id and candidate.match_id == record.match_id
            for candidate in self._records.values()
        )
        if not failures and not another_runner_remains:
            deleted = self._run(("ip", "netns", "del", record.red_namespace), "", allow_failure=True)
            listed = self._run(("ip", "netns", "list"), "", allow_failure=True)
            if deleted.returncode != 0 or listed.returncode != 0 or any(
                line.split(maxsplit=1)[0] == record.red_namespace for line in listed.stdout.splitlines() if line.strip()
            ):
                failures.append("ARENA_NAMESPACE_REMAINS")
        if not failures:
            self._records.pop(record.handle.runner_id, None)
            shutil.rmtree(record.root, ignore_errors=True)
            if not another_runner_remains:
                shutil.rmtree(record.root.parent, ignore_errors=True)
            return TeardownEvidence(record.handle.runner_id, TeardownState.DESTROYED, "QEMU stopped; workspace checked; Arena namespace and cgroup removed")
        return TeardownEvidence(record.handle.runner_id, TeardownState.QUARANTINED, ";".join(failures), reason_code or failures[0])

    def _stop_pid(self, pid: int) -> None:
        if self._host.process_alive(pid):
            self._host.terminate(pid)
            if self._host.process_alive(pid):
                self._host.kill(pid)

    def _enforce_ttl(self, records: list[_RunnerRecord]) -> None:
        expired = [record for record in records if time.monotonic() >= record.deadline]
        if not expired:
            return
        for record in expired:
            evidence = self._destroy_record(record, "TTL_EXPIRED")
            self._terminal[record.handle.runner_id] = evidence
        raise RuntimeError("TTL_EXPIRED")

    def _verify_base_image(self) -> None:
        if not self.config.base_image.is_file():
            raise RuntimeError("BASE_IMAGE_MISSING")
        with self.config.base_image.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != self.config.base_image_sha256:
            raise RuntimeError("BASE_IMAGE_DIGEST_MISMATCH")
        mode = self.config.base_image.stat().st_mode
        if mode & stat.S_IWOTH:
            raise RuntimeError("BASE_IMAGE_WORLD_WRITABLE")
        try:
            profile = json.loads(self.config.base_profile.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("BASE_IMAGE_PROFILE_INVALID") from error
        required_profile = {
            "schema_version": 1,
            "image_sha256": self.config.base_image_sha256,
            "root_filesystem": "readonly",
            "workspace_mount": "/workspace",
            "control_protocol": "virtio-serial-v1",
            "toy_service": "synthetic-http",
        }
        if any(profile.get(key) != value for key, value in required_profile.items()):
            raise RuntimeError("BASE_IMAGE_PROFILE_INVALID")

    def _write_cloud_init(self, root: Path, match_id: str, name: str, host_octet: int, nonce: str) -> None:
        (root / "meta-data.yaml").write_text(
            f"match={match_id}\nrunner={name}\nnonce={nonce}\nip=10.77.0.{host_octet}\n", encoding="ascii"
        )
        (root / "network-config.yaml").write_text(
            "version: 2\nethernets:\n  eth0:\n    addresses:\n      - 10.77.0." + str(host_octet) + "/24\n", encoding="ascii"
        )
        # Only a read-only metadata disk is generated at runtime. The immutable
        # base owns the competitor, control handler, and synthetic toy service.
        (root / "user-data.yaml").write_text("#cloud-config\n", encoding="ascii")

    def _require_records(self, runners: tuple[RunnerHandle, RunnerHandle]) -> list[_RunnerRecord]:
        records = [self._records.get(runner.runner_id) for runner in runners]
        if any(record is None for record in records):
            raise RuntimeError("RUNNER_STATE_UNAVAILABLE")
        typed = [record for record in records if record is not None]
        if len({record.match_id for record in typed}) != 1 or len({record.red_namespace for record in typed}) != 1:
            raise RuntimeError("RUNNER_ARENA_MISMATCH")
        return typed

    def _validate_identity(self, match_id: str, names: tuple[str, str]) -> None:
        if not _SAFE_ID.fullmatch(match_id) or len(names) != 2 or len(set(names)) != 2 or not all(_SAFE_ID.fullmatch(name) for name in names):
            raise ValueError("local KVM Match and Runner identities must be safe, distinct identifiers")

    @staticmethod
    def _network_names(match_id: str) -> tuple[str, str]:
        digest = hashlib.sha256(match_id.encode("ascii")).hexdigest()[:8]
        return f"sbx-{digest}", f"br-{digest}"

    @staticmethod
    def _blue_namespace(match_id: str, name: str) -> str:
        digest = hashlib.sha256(f"{match_id}:{name}:blue".encode("ascii")).hexdigest()[:8]
        return f"sbb-{digest}"

    @staticmethod
    def _tap_name(match_id: str, name: str) -> str:
        digest = hashlib.sha256(f"{match_id}:{name}".encode("ascii")).hexdigest()[:8]
        return f"tap-{digest}"

    def _run(self, argv: tuple[str, ...], reason_code: str, *, input_text: str | None = None, allow_failure: bool = False) -> CommandResult:
        result = self._host.run(argv, input_text=input_text)
        if result.returncode != 0 and not allow_failure:
            raise RuntimeError(reason_code or "LOCAL_KVM_COMMAND_FAILED")
        return result

    def _run_quiet(self, argv: tuple[str, ...]) -> None:
        try:
            self._host.run(argv)
        except Exception:
            pass
