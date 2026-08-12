"""Local snapshot-backed KVM adapter for a controlled Runner rehearsal.

This adapter is intentionally conservative.  It is a real local runtime, but
its ``production_ready`` flag stays false: a successful rehearsal proves only
the configured containment checks, not resistance to a hostile workload.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .arena_safety import ArenaNetworkPolicy, NetworkObservation, Phase, TeardownEvidence, TeardownState
from .local_kvm_control import ControlProbe, ControlReady, NetworkProof, parse_control, parse_network_proof
from .runner_backend import PreflightCheck, PreflightWitnessFailed, ProvisioningFailed, RunnerHandle


_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
_MAX_CONTROL_RESPONSE = 4096
_NETWORK_PROBE_TIMEOUT_SECONDS = 12
_MAX_QEMU_STDERR_BYTES = 1024
_SOCKET_WITNESS_TIMEOUT_SECONDS = 1.0
_SOCKET_WITNESS_MESSAGE_BYTES = 64
_SENSITIVE_DIAGNOSTIC_ASSIGNMENT = re.compile(
    r"(?i)\b[\w.-]*(?:key|token|secret|password|credential)[\w.-]*\s*=\s*\S+"
)
_ABSOLUTE_PATH = re.compile(r"(?<!\w)/(?:[^\s:]+)")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _sanitize_qemu_stderr(raw: bytes | str) -> str:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = _SENSITIVE_DIAGNOSTIC_ASSIGNMENT.sub("<redacted-assignment>", text)
    text = _ABSOLUTE_PATH.sub("<path>", text)
    text = " ".join(text.split())
    return text[:_MAX_QEMU_STDERR_BYTES] or "no diagnostic output"


class QemuStartupFailure(RuntimeError):
    """Immediate QEMU rejection with an intentionally non-sensitive witness."""

    def __init__(self, stderr: bytes | str) -> None:
        super().__init__("QEMU_EXITED_DURING_STARTUP")
        self.diagnostic = f"QEMU_STDERR:{_sanitize_qemu_stderr(stderr)}"


class SocketWitnessStage(str, Enum):
    SUCCESS = "success"
    CREATE_FAILED = "create_failed"
    UNLINK_FAILED = "unlink_failed"
    ARTIFACT_REMAINS = "artifact_remains"


@dataclass(frozen=True)
class SocketWitnessResult:
    stage: SocketWitnessStage
    errno: int | None = None

    @classmethod
    def success(cls) -> "SocketWitnessResult":
        return cls(SocketWitnessStage.SUCCESS)


def _drop_socket_witness_identity(qemu_user: str) -> None:
    identity = pwd.getpwnam(qemu_user)
    os.initgroups(identity.pw_name, identity.pw_gid)
    os.setgid(identity.pw_gid)
    os.setuid(identity.pw_uid)


def _socket_witness_child(directory: Path, qemu_user: str) -> SocketWitnessResult:
    probe = directory / f".sandboxer-control-witness-{secrets.token_hex(8)}"
    try:
        _drop_socket_witness_identity(qemu_user)
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
    except OSError as error:
        return SocketWitnessResult(SocketWitnessStage.CREATE_FAILED, error.errno or errno.EIO)
    try:
        os.unlink(probe)
    except OSError as error:
        return SocketWitnessResult(SocketWitnessStage.UNLINK_FAILED, error.errno or errno.EIO)
    try:
        os.lstat(probe)
    except FileNotFoundError:
        return SocketWitnessResult.success()
    except OSError as error:
        return SocketWitnessResult(SocketWitnessStage.ARTIFACT_REMAINS, error.errno or errno.EIO)
    return SocketWitnessResult(SocketWitnessStage.ARTIFACT_REMAINS, errno.EEXIST)


class LocalKvmHost(Protocol):
    """Small host-privileged port; every command is argv-only, never a shell."""

    def run(
        self, argv: tuple[str, ...], *, input_text: str | None = None, timeout_seconds: float = 10
    ) -> CommandResult: ...

    def start(self, argv: tuple[str, ...], *, stderr_path: Path) -> int: ...

    def verify_socket_access(
        self, directory: Path, control_socket: Path, *, qemu_user: str
    ) -> SocketWitnessResult: ...

    def control_exchange(self, socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str: ...

    def process_alive(self, pid: int) -> bool: ...

    def process_identity(self, pid: int) -> str | None: ...

    def cgroup_contains(self, cgroup: str, pid: int) -> bool: ...

    def cgroup_limited(self, cgroup: str, pid: int, memory_bytes: int, cpu_max: str, pids_max: int) -> bool: ...

    def arm_ttl(self, cgroup: str, identity: str, seconds: int) -> object: ...

    def cancel_ttl(self, token: object) -> bool: ...

    def terminate(self, pid: int) -> None: ...

    def kill(self, pid: int) -> None: ...

    def wait_for_exit(self, pid: int, identity: str, *, timeout_seconds: float) -> bool: ...

    def create_cgroup(self, name: str, *, memory_max_bytes: int, cpu_max: str, pids_max: int) -> str: ...

    def attach_to_cgroup(self, cgroup: str, pid: int) -> None: ...

    def destroy_cgroup(self, cgroup: str) -> bool: ...


class SubprocessLocalKvmHost:
    """The privileged local implementation; invoke only from the Orchestrator."""

    def __init__(self, *, cgroup_root: Path = Path("/sys/fs/cgroup/sandboxer")) -> None:
        self._cgroup_root = cgroup_root
        self._diagnostic_threads: dict[int, threading.Thread] = {}

    def run(
        self, argv: tuple[str, ...], *, input_text: str | None = None, timeout_seconds: float = 10
    ) -> CommandResult:
        completed = subprocess.run(
            argv, input=input_text, text=True, capture_output=True, timeout=timeout_seconds, check=False
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    def start(self, argv: tuple[str, ...], *, stderr_path: Path) -> int:
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.touch(mode=0o600, exist_ok=False)
        os.chmod(stderr_path, 0o600)
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            stderr_path.write_text(_sanitize_qemu_stderr(str(error)), encoding="utf-8")
            raise QemuStartupFailure(str(error)) from error
        assert process.stderr is not None

        def capture() -> None:
            captured = bytearray()
            while chunk := process.stderr.read(256):
                if len(captured) < _MAX_QEMU_STDERR_BYTES:
                    captured.extend(chunk[:_MAX_QEMU_STDERR_BYTES - len(captured)])
            stderr_path.write_text(_sanitize_qemu_stderr(bytes(captured)), encoding="utf-8")

        collector = threading.Thread(target=capture, name=f"sandboxer-qemu-stderr-{process.pid}", daemon=True)
        collector.start()
        self._diagnostic_threads[process.pid] = collector
        # Do not mistake an immediately rejected QEMU configuration for a live
        # Runner.  Longer guest boot readiness is checked via virtio control.
        time.sleep(0.1)
        if process.poll() is not None:
            collector.join(timeout=1)
            diagnostic = stderr_path.read_text(encoding="utf-8", errors="replace")
            self._diagnostic_threads.pop(process.pid, None)
            raise QemuStartupFailure(diagnostic)
        return process.pid

    def verify_socket_access(
        self, directory: Path, control_socket: Path, *, qemu_user: str
    ) -> SocketWitnessResult:
        del control_socket  # Its absence is checked by the Provider before this fork.
        read_fd, write_fd = os.pipe()
        try:
            child = os.fork()
        except OSError as error:
            os.close(read_fd)
            os.close(write_fd)
            return SocketWitnessResult(SocketWitnessStage.CREATE_FAILED, error.errno or errno.EIO)
        if child == 0:  # pragma: no cover - exercised through the parent seam
            try:
                os.close(read_fd)
                result = _socket_witness_child(directory, qemu_user)
                payload = f"{result.stage.value}:{result.errno if result.errno is not None else 0}".encode("ascii")
                os.write(write_fd, payload[:_SOCKET_WITNESS_MESSAGE_BYTES])
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        deadline = time.monotonic() + _SOCKET_WITNESS_TIMEOUT_SECONDS
        try:
            while True:
                completed, _status = os.waitpid(child, os.WNOHANG)
                if completed == child:
                    break
                if time.monotonic() >= deadline:
                    os.kill(child, signal.SIGKILL)
                    os.waitpid(child, 0)
                    return SocketWitnessResult(SocketWitnessStage.CREATE_FAILED, errno.ETIMEDOUT)
                time.sleep(0.01)
            payload = os.read(read_fd, _SOCKET_WITNESS_MESSAGE_BYTES).decode("ascii", errors="ignore")
        finally:
            os.close(read_fd)
        stage_text, separator, errno_text = payload.partition(":")
        if not separator:
            return SocketWitnessResult(SocketWitnessStage.CREATE_FAILED, errno.EIO)
        try:
            stage = SocketWitnessStage(stage_text)
            numeric_errno = int(errno_text)
        except (ValueError, TypeError):
            return SocketWitnessResult(SocketWitnessStage.CREATE_FAILED, errno.EIO)
        return SocketWitnessResult(stage, None if stage is SocketWitnessStage.SUCCESS and numeric_errno == 0 else numeric_errno)

    def control_exchange(self, socket_path: Path, payload: str, *, timeout_seconds: float = 5) -> str:
        network_probe = payload.startswith("NETPROBE ")
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
                response = b"".join(chunks)
                # The guest closes its virtio port between requests, but the
                # QEMU chardev socket can remain open while it immediately
                # reopens the port.  NETPROBE is a single newline-framed
                # response, so EOF is not its completion signal.
                if b"PROBE_OK" in response or (network_probe and b"\n" in response):
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
        completed = subprocess.run(
            ("systemd-run", "--unit", unit, "--collect", "--service-type=oneshot", "--on-active", str(seconds), sys.executable,
             "-m", "sandboxer_v0.local_kvm_watchdog", pid, starttime, cgroup),
            text=True, capture_output=True, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("TTL_WATCHDOG_UNAVAILABLE")
        return unit

    def cancel_ttl(self, token: object) -> bool:
        if not isinstance(token, str):
            return False
        timer, service = f"{token}.timer", f"{token}.service"
        # State checks distinguish an idempotently absent unit from an active
        # watchdog. A failed stop is harmless only if both checks prove this.
        subprocess.run(("systemctl", "stop", timer, service), text=True, capture_output=True, check=False)
        states = [
            subprocess.run(("systemctl", "is-active", "--quiet", unit), text=True, capture_output=True, check=False)
            for unit in (timer, service)
        ]
        # systemctl uses 3 for inactive and 4 for an absent unit. Other
        # failures (including manager unavailability) are not proof.
        return all(result.returncode in (3, 4) for result in states)

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

    def wait_for_exit(self, pid: int, identity: str, *, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.process_identity(pid) != identity:
                return True
            try:
                reaped, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                reaped = 0
            if reaped == pid:
                return True
            time.sleep(0.02)
        return self.process_identity(pid) != identity

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
    qemu_gid: int | None = None
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
    ttl_token: object | None
    nonce: str
    deadline: float
    serial_evidence: str | None = None
    phase: Phase = Phase.BLUE


@dataclass
class _PartialRecord:
    root: Path
    namespaces: tuple[str, ...]
    tap: str | None
    control_socket: Path
    pid: int | None
    process_identity: str | None
    cgroup: str | None
    ttl_token: object | None


class LocalKvmRunnerProvider:
    """A disposable, no-egress local KVM provider with an external control plane."""

    simulated_fixture = False
    # Local smoke tests cannot establish adversarial escape resistance.
    production_ready = False

    def __init__(self, config: LocalKvmConfig, host: LocalKvmHost | None = None) -> None:
        self.config = config
        self._host = host or SubprocessLocalKvmHost()
        self._records: dict[str, _RunnerRecord] = {}
        self._route_diagnostics: dict[str, tuple[str, str, str]] = {}
        self._terminal: dict[str, TeardownEvidence] = {}
        self._partials: dict[str, _PartialRecord] = {}

    def provision(self, match_id: str, names: tuple[str, str]) -> tuple[RunnerHandle, RunnerHandle]:
        self._validate_identity(match_id, names)
        self._verify_base_image()
        match_root = self.config.runner_root / match_id
        if match_root.exists():
            raise RuntimeError("MATCH_ARTIFACTS_ALREADY_EXIST")
        match_root.mkdir(parents=True, mode=0o711)
        # mkdir applies the Orchestrator's umask. The QEMU user needs only
        # traversal through this host-owned Match root to reach its 0700
        # Runner root, so restore exactly 0711 rather than widening it.
        os.chmod(match_root, 0o711)
        match_state = match_root.stat()
        if match_state.st_uid != os.geteuid() or stat.S_IMODE(match_state.st_mode) != 0o711:
            self._remove_root(match_root)
            raise RuntimeError("MATCH_ROOT_UNSAFE")
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
                self._run(("ip", "netns", "del", namespace), "", allow_failure=True)
                namespace_destroyed = self._namespace_absent(namespace)
                evidence.append(TeardownEvidence(
                    f"{match_id}:{namespace}",
                    TeardownState.DESTROYED if namespace_destroyed else TeardownState.QUARANTINED,
                    "namespace deletion verified" if namespace_destroyed else "namespace cleanup unverified",
                    None if namespace_destroyed else "NAMESPACE_REMAINS",
                ))
            if isinstance(error, ProvisioningFailed):
                evidence.extend(error.teardown_evidence)
            if all(item.state is TeardownState.DESTROYED for item in evidence) and not self._remove_root(match_root):
                evidence.append(TeardownEvidence(
                    match_id, TeardownState.QUARANTINED, "Match artifact cleanup unverified", "MATCH_ROOT_REMAINS"
                ))
            raise ProvisioningFailed(str(error), tuple(evidence)) from error
        return records[0].handle, records[1].handle

    def probe(self, runners: tuple[RunnerHandle, RunnerHandle]) -> tuple[PreflightCheck, ...]:
        records = self._require_records(runners)
        self._enforce_ttl(records)
        try:
            responses = [self._control_probe(record) for record in records]
        except PreflightWitnessFailed:
            self._capture_serial_stages(records)
            raise
        except Exception as error:
            self._capture_serial_stages(records)
            raise PreflightWitnessFailed("LOCAL_KVM_CONTROL_SOCKET_UNAVAILABLE") from error
        # Guest route fields are mandatory, bounded diagnostics.  They are not
        # an isolation proxy: the host namespace route check and active guest
        # reachability witnesses below are authoritative and fail closed.
        self._route_diagnostics.update({
            record.handle.runner_id: (response.route_after_setup, response.route_at_control, response.route_origin)
            for record, response in zip(records, responses)
        })
        try:
            network = self._measure_network(records, Phase.BLUE)
        except PreflightWitnessFailed:
            raise
        except Exception as error:
            raise PreflightWitnessFailed("LOCAL_KVM_BLUE_NETWORK_WITNESS_UNAVAILABLE") from error
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
        except PreflightWitnessFailed:
            if phase is Phase.RED:
                raise
            return NetworkObservation(frozenset(), True, True, True)
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
            failures: list[str] = []
            if partial.ttl_token is not None:
                if not self._cancel_ttl(partial.ttl_token):
                    failures.append("TTL_WATCHDOG_REMAINS")
                else:
                    partial.ttl_token = None
            if partial.pid is not None and partial.process_identity is not None:
                self._stop_pid(partial.pid, partial.process_identity)
                if self._host.process_identity(partial.pid) == partial.process_identity:
                    failures.append("QEMU_PROCESS_SURVIVED")
            if partial.cgroup is not None and not self._host.destroy_cgroup(partial.cgroup):
                failures.append("CGROUP_REMAINS")
            if partial.tap is not None and not self._remove_tap(partial.namespaces[0], partial.tap):
                failures.append("TAP_REMAINS")
            for namespace in partial.namespaces:
                self._run(("ip", "netns", "del", namespace), "", allow_failure=True)
                if not self._namespace_absent(namespace):
                    failures.append(namespace)
            if not failures and not self._remove_root(partial.root):
                failures.append("RUNNER_ROOT_REMAINS")
            match_root = partial.root.parent
            another_partial_remains = any(
                candidate_id != runner_id and candidate.root.parent == match_root
                for candidate_id, candidate in self._partials.items()
            )
            another_record_remains = any(candidate.root.parent == match_root for candidate in self._records.values())
            if not failures and not another_partial_remains and not another_record_remains and not self._remove_root(match_root):
                failures.append("MATCH_ROOT_REMAINS")
            if not failures:
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
        os.chown(root, self.config.qemu_uid, self.config.qemu_gid if self.config.qemu_gid is not None else -1)
        os.chmod(root, 0o700)
        workspace = root / "workspace.qcow2"
        seed = root / "seed.iso"
        control_socket = root / "control.sock"
        nonce = secrets.token_hex(32)
        tap = self._tap_name(match_id, name)
        pid: int | None = None
        process_identity: str | None = None
        cgroup: str | None = None
        ttl_token: object | None = None
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
            self._verify_control_socket_prelaunch(root, control_socket)
            self._run(("ip", "netns", "exec", blue_namespace, "ip", "tuntap", "add", "dev", tap, "mode", "tap", "user", str(self.config.qemu_uid)), "TAP_CREATE_FAILED")
            self._run(("ip", "-n", blue_namespace, "link", "set", tap, "up"), "TAP_ENABLE_FAILED")
            pid = self._host.start(
                self._qemu_command(blue_namespace, tap, workspace, seed, control_socket, root / "serial.log"),
                stderr_path=root / "qemu.stderr",
            )
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
            failures: list[str] = []
            if ttl_token is not None and not self._cancel_ttl(ttl_token):
                failures.append("TTL_WATCHDOG_REMAINS")
            else:
                ttl_token = None
            if pid is not None:
                if process_identity is not None:
                    self._stop_pid(pid, process_identity)
                if process_identity is not None and self._host.process_identity(pid) == process_identity:
                    failures.append("QEMU_PROCESS_SURVIVED")
                else:
                    pid = None
                    process_identity = None
            if cgroup is not None:
                if not self._host.destroy_cgroup(cgroup):
                    failures.append("CGROUP_REMAINS")
                else:
                    cgroup = None
            if not self._remove_tap(blue_namespace, tap):
                failures.append("TAP_REMAINS")
            else:
                tap = None
            if not failures and not self._remove_root(root):
                failures.append("RUNNER_ROOT_REMAINS")
            state = TeardownState.DESTROYED if not failures else TeardownState.QUARANTINED
            if state is TeardownState.QUARANTINED:
                self._partials[runner_id] = _PartialRecord(
                    root, (blue_namespace, red_namespace), tap, control_socket, pid,
                    process_identity, cgroup, ttl_token,
                )
            diagnostic = f";{error.diagnostic}" if isinstance(error, QemuStartupFailure) else ""
            raise ProvisioningFailed(
                str(error),
                (TeardownEvidence(
                    runner_id, state,
                    ("partial Runner provisioning rollback verified" if not failures else ";".join(failures)) + diagnostic,
                    str(error) if not failures else failures[0],
                ),),
            ) from error

    def _verify_control_socket_prelaunch(self, root: Path, control_socket: Path) -> None:
        root_state = root.stat()
        mode = stat.S_IMODE(root_state.st_mode)
        if (
            root_state.st_uid != self.config.qemu_uid
            or (self.config.qemu_gid is not None and root_state.st_gid != self.config.qemu_gid)
            or mode != 0o700
        ):
            raise RuntimeError("QEMU_RUNTIME_DIRECTORY_UNSAFE")
        try:
            os.lstat(control_socket)
        except FileNotFoundError:
            pass
        else:
            # Never unlink an unexpected filesystem entry merely to make a
            # Runner start. A fresh disposable root is the only valid state.
            raise RuntimeError("CONTROL_SOCKET_PREEXISTS")
        witness = self._host.verify_socket_access(root, control_socket, qemu_user=self.config.qemu_user)
        if witness.stage is not SocketWitnessStage.SUCCESS:
            suffix = f"_ERRNO_{witness.errno}" if witness.errno is not None else ""
            raise RuntimeError(f"QEMU_SOCKET_WITNESS_{witness.stage.name}{suffix}")

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
            try:
                routes = [
                    self._run(
                        ("ip", "-n", record.blue_namespace, "route", "show", "default"),
                        "NETWORK_WITNESS_UNAVAILABLE",
                    )
                    for record in records
                ]
            except Exception as error:
                raise PreflightWitnessFailed("LOCAL_KVM_BLUE_HOST_ROUTE_WITNESS_UNAVAILABLE") from error
            if any(item.stdout.strip() for item in routes):
                return NetworkObservation(frozenset(), True, True, True)
            proofs = [self._network_proof(record, phase) for record in records]
            self._require_blue_network_proofs(proofs)
            return NetworkObservation(
                frozenset({f"{records[0].handle.name}:private-a", f"{records[1].handle.name}:private-b"}),
                False, False, False,
            )
        namespace, bridge = records[0].red_namespace, records[0].red_bridge
        try:
            nft = self._run(("ip", "netns", "exec", namespace, "nft", "list", "table", "bridge", "sandboxer"), "NETWORK_WITNESS_UNAVAILABLE")
        except Exception as error:
            raise PreflightWitnessFailed("LOCAL_KVM_RED_HOST_NFT_WITNESS_UNAVAILABLE") from error
        try:
            addresses = self._run(("ip", "-n", namespace, "-j", "addr", "show", "dev", bridge), "NETWORK_WITNESS_UNAVAILABLE")
        except Exception as error:
            raise PreflightWitnessFailed("LOCAL_KVM_RED_HOST_BRIDGE_ADDRESS_WITNESS_UNAVAILABLE") from error
        try:
            routes = self._run(("ip", "-n", namespace, "route", "show", "default"), "NETWORK_WITNESS_UNAVAILABLE")
        except Exception as error:
            raise PreflightWitnessFailed("LOCAL_KVM_RED_HOST_ROUTE_WITNESS_UNAVAILABLE") from error
        try:
            bridge_addresses = json.loads(addresses.stdout)
        except json.JSONDecodeError as error:
            raise PreflightWitnessFailed("LOCAL_KVM_RED_HOST_BRIDGE_ADDRESS_WITNESS_INVALID") from error
        public_ingress = any(item.get("addr_info") for item in bridge_addresses)
        direct_egress = bool(routes.stdout.strip())
        expected_rules = ["policy drop", "ether type arp", f"tcp dport {self.config.toy_service_port}", records[0].tap, records[1].tap]
        rule_set_ok = all(rule in nft.stdout for rule in expected_rules)
        if not rule_set_ok:
            raise PreflightWitnessFailed("LOCAL_KVM_RED_HOST_NFT_POLICY_WITNESS_FAILED")
        proofs = [self._network_proof(record, phase) for record in records]
        self._require_red_network_proofs(proofs)
        # Share the canonical names with the backend policy rather than
        # independently reconstructing endpoint identifiers here.
        edges = ArenaNetworkPolicy(
            records[0].handle.runner_id.rsplit(":", 1)[0],
            records[0].handle.name,
            records[1].handle.name,
        ).expected(Phase.RED).edges
        # The namespace has only a bridge and unnumbered taps.  The Unix control
        # socket is not an IP path and cannot be reached by either guest.
        return NetworkObservation(edges, direct_egress, public_ingress, False)

    @staticmethod
    def _require_blue_network_proofs(proofs: list[NetworkProof]) -> None:
        """Preserve the failed guest witness instead of inventing an IP leak."""
        checks = (
            ("LOCAL_KVM_BLUE_PEER_ISOLATION_WITNESS_FAILED", lambda proof: proof.peer_denied),
            ("LOCAL_KVM_BLUE_TOY_SERVICE_WITNESS_FAILED", lambda proof: not proof.toy_http),
            ("LOCAL_KVM_BLUE_ALTERNATE_PORT_WITNESS_FAILED", lambda proof: proof.alternate_denied),
            ("LOCAL_KVM_BLUE_ICMP_WITNESS_FAILED", lambda proof: proof.icmp_denied),
            ("LOCAL_KVM_BLUE_ORCHESTRATOR_WITNESS_FAILED", lambda proof: proof.orchestrator_denied),
        )
        for reason_code, passed in checks:
            if not all(passed(proof) for proof in proofs):
                raise PreflightWitnessFailed(reason_code)
        if not all(proof.egress_denied for proof in proofs):
            if any(proof.egress_reason == "default_route" for proof in proofs):
                raise PreflightWitnessFailed("LOCAL_KVM_BLUE_EGRESS_DEFAULT_ROUTE_WITNESS_FAILED")
            raise PreflightWitnessFailed("LOCAL_KVM_BLUE_EGRESS_TCP_WITNESS_FAILED")

    @staticmethod
    def _require_red_network_proofs(proofs: list[NetworkProof]) -> None:
        checks = (
            ("LOCAL_KVM_RED_DECLARED_TOY_SERVICE_WITNESS_FAILED", lambda proof: not proof.peer_denied and proof.toy_http),
            ("LOCAL_KVM_RED_ALTERNATE_PORT_WITNESS_FAILED", lambda proof: proof.alternate_denied),
            ("LOCAL_KVM_RED_ICMP_WITNESS_FAILED", lambda proof: proof.icmp_denied),
            ("LOCAL_KVM_RED_EGRESS_WITNESS_FAILED", lambda proof: proof.egress_denied),
            ("LOCAL_KVM_RED_ORCHESTRATOR_WITNESS_FAILED", lambda proof: proof.orchestrator_denied),
        )
        for reason_code, passed in checks:
            if not all(passed(proof) for proof in proofs):
                raise PreflightWitnessFailed(reason_code)

    def _control_probe(self, record: _RunnerRecord) -> ControlProbe:
        try:
            response = self._control_exchange(record.control_socket, record.nonce)
        except RuntimeError as error:
            if str(error) == "CONTROL_BOOTSTRAP_TIMEOUT":
                raise PreflightWitnessFailed("LOCAL_KVM_CONTROL_TIMEOUT") from error
            raise PreflightWitnessFailed("LOCAL_KVM_CONTROL_SOCKET_UNAVAILABLE") from error
        except (FileNotFoundError, ConnectionRefusedError, OSError, socket.timeout) as error:
            raise PreflightWitnessFailed("LOCAL_KVM_CONTROL_SOCKET_UNAVAILABLE") from error
        try:
            return self._parse_control(response, record.nonce, require_probe=True)
        except Exception as error:
            raise PreflightWitnessFailed("LOCAL_KVM_CONTROL_INVALID_FRAME") from error

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
        try:
            response = self._host.control_exchange(
                record.control_socket,
                f"NETPROBE {record.nonce} {phase.value}\n",
                timeout_seconds=_NETWORK_PROBE_TIMEOUT_SECONDS,
            )
        except (TimeoutError, socket.timeout) as error:
            raise PreflightWitnessFailed(f"LOCAL_KVM_{phase.value.upper()}_GUEST_NETPROBE_TIMEOUT") from error
        except (FileNotFoundError, ConnectionRefusedError, OSError) as error:
            raise PreflightWitnessFailed(f"LOCAL_KVM_{phase.value.upper()}_GUEST_NETPROBE_REQUEST_UNAVAILABLE") from error
        try:
            return parse_network_proof(response, record.nonce, phase.value)
        except Exception as error:
            raise PreflightWitnessFailed(f"LOCAL_KVM_{phase.value.upper()}_GUEST_NETPROBE_INVALID_RESPONSE") from error

    def _parse_control(self, response: str, nonce: str, *, require_probe: bool) -> ControlReady | ControlProbe:
        if len(response.encode("ascii", errors="ignore")) > _MAX_CONTROL_RESPONSE:
            raise RuntimeError("CONTROL_RESPONSE_TOO_LARGE")
        return parse_control(response, nonce, require_probe=require_probe)

    def _capture_serial_stages(self, records: list[_RunnerRecord]) -> None:
        """Persist only fixed stage labels before disposable roots are removed."""
        allowed = {
            "SANDBOXER_RUNTIME_BOOTSTRAP", "SANDBOXER_RUNTIME_MOUNTS_READY",
            "SANDBOXER_STAGE_SETUP", "SANDBOXER_STAGE_TOY", "SANDBOXER_STAGE_CONTROL",
            "SANDBOXER_RUNTIME_READY", "SANDBOXER_SETUP_FAILED", "SANDBOXER_TOY_FAILED",
            "SANDBOXER_CONTROL_FAILED",
            "SANDBOXER_ROUTE_ABSENT_AFTER_SETUP", "SANDBOXER_ROUTE_PRESENT_AFTER_SETUP",
            "SANDBOXER_ROUTE_ABSENT_AFTER_TOY", "SANDBOXER_ROUTE_PRESENT_AFTER_TOY",
            "SANDBOXER_ROUTE_ABSENT_BEFORE_CONTROL", "SANDBOXER_ROUTE_PRESENT_BEFORE_CONTROL",
            "SANDBOXER_ROUTE_MARKER_UNAVAILABLE",
            "SANDBOXER_ROUTE_ORIGIN_ABSENT", "SANDBOXER_ROUTE_ORIGIN_DHCP", "SANDBOXER_ROUTE_ORIGIN_RA",
            "SANDBOXER_ROUTE_ORIGIN_STATIC", "SANDBOXER_ROUTE_ORIGIN_OTHER", "SANDBOXER_ROUTE_ORIGIN_UNKNOWN",
            "SANDBOXER_DHCP_CLIENT_0", "SANDBOXER_DHCP_CLIENT_1", "SANDBOXER_DHCP_CLIENT_UNKNOWN",
        }
        evidence_root = self.config.runner_root.parent / "evidence"
        for record in records:
            try:
                raw = (record.root / "serial.log").read_text(encoding="ascii", errors="ignore")[-8192:]
                netprobe_stages = {
                    "SANDBOXER_NETPROBE_STAGE=start", "SANDBOXER_NETPROBE_STAGE=peer",
                    "SANDBOXER_NETPROBE_STAGE=alternate", "SANDBOXER_NETPROBE_STAGE=icmp",
                    "SANDBOXER_NETPROBE_STAGE=egress", "SANDBOXER_NETPROBE_STAGE=orchestrator",
                    "SANDBOXER_NETPROBE_STAGE=emit",
                }
                labels = [line for line in raw.splitlines() if line in allowed or line in netprobe_stages]
                if not labels:
                    continue
                evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                target = evidence_root / f"{hashlib.sha256(record.handle.runner_id.encode()).hexdigest()[:16]}.serial-stages"
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w", encoding="ascii") as output:
                    output.write("\n".join(labels) + "\n")
                os.chmod(target, 0o600)
                record.serial_evidence = "SERIAL_STAGE_EVIDENCE"
            except OSError:
                record.serial_evidence = "SERIAL_STAGE_EVIDENCE_UNAVAILABLE"

    def _destroy_record(self, record: _RunnerRecord, reason_code: str | None) -> TeardownEvidence:
        failures: list[str] = []
        if record.ttl_token is not None:
            if not self._cancel_ttl(record.ttl_token):
                failures.append("TTL_WATCHDOG_REMAINS")
            else:
                record.ttl_token = None
        stopped = self._stop_pid(record.pid, record.process_identity)
        if not stopped or self._host.process_identity(record.pid) == record.process_identity:
            failures.append("QEMU_PROCESS_SURVIVED")
        else:
            check = self._run((self.config.qemu_img_binary, "check", "--output=json", os.fspath(record.workspace)), "", allow_failure=True)
            if check.returncode != 0:
                failures.append("OVERLAY_INTEGRITY_UNVERIFIED")
        if not self._host.destroy_cgroup(record.cgroup):
            failures.append("CGROUP_REMAINS")
        self._run(("ip", "netns", "del", record.blue_namespace), "", allow_failure=True)
        if not self._namespace_absent(record.blue_namespace):
            failures.append("BLUE_NAMESPACE_REMAINS")
        another_runner_remains = any(
            candidate.handle.runner_id != record.handle.runner_id and candidate.match_id == record.match_id
            for candidate in self._records.values()
        )
        if not failures and not another_runner_remains:
            self._run(("ip", "netns", "del", record.red_namespace), "", allow_failure=True)
            if not self._namespace_absent(record.red_namespace):
                failures.append("ARENA_NAMESPACE_REMAINS")
        if not failures:
            if not self._remove_root(record.root):
                return TeardownEvidence(record.handle.runner_id, TeardownState.QUARANTINED, "RUNNER_ROOT_REMAINS", reason_code or "RUNNER_ROOT_REMAINS")
            if not another_runner_remains:
                if not self._remove_root(record.root.parent):
                    return TeardownEvidence(record.handle.runner_id, TeardownState.QUARANTINED, "MATCH_ROOT_REMAINS", reason_code or "MATCH_ROOT_REMAINS")
            self._records.pop(record.handle.runner_id, None)
            suffix = f"; {record.serial_evidence}" if record.serial_evidence else ""
            return TeardownEvidence(record.handle.runner_id, TeardownState.DESTROYED, "QEMU stopped; workspace checked; Arena namespace and cgroup removed" + suffix)
        return TeardownEvidence(record.handle.runner_id, TeardownState.QUARANTINED, ";".join(failures), reason_code or failures[0])

    def _stop_pid(self, pid: int, identity: str) -> bool:
        if self._host.process_identity(pid) != identity:
            return True
        self._host.terminate(pid)
        if self._host.wait_for_exit(pid, identity, timeout_seconds=0.5):
            return True
        self._host.kill(pid)
        return self._host.wait_for_exit(pid, identity, timeout_seconds=0.5)

    def _cancel_ttl(self, token: object) -> bool:
        try:
            return self._host.cancel_ttl(token)
        except Exception:
            return False

    def _remove_tap(self, namespace: str, tap: str) -> bool:
        if self._namespace_absent(namespace):
            return True
        self._run(("ip", "-n", namespace, "link", "del", tap), "", allow_failure=True)
        listed = self._run(("ip", "-n", namespace, "link", "show", "dev", tap), "", allow_failure=True)
        return listed.returncode != 0 and "does not exist" in listed.stderr.lower()

    def _namespace_absent(self, namespace: str) -> bool:
        listed = self._run(("ip", "netns", "list"), "", allow_failure=True)
        return listed.returncode == 0 and not any(
            line.split(maxsplit=1)[0] == namespace for line in listed.stdout.splitlines() if line.strip()
        )

    @staticmethod
    def _remove_root(root: Path) -> bool:
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return not root.exists()

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
        if profile != required_profile:
            raise RuntimeError("BASE_IMAGE_PROFILE_INVALID")

    def _write_cloud_init(self, root: Path, match_id: str, name: str, host_octet: int, nonce: str) -> None:
        peer_octet = 12 if host_octet == 11 else 11
        (root / "meta-data.yaml").write_text(
            f"match={match_id}\nrunner={name}\nnonce={nonce}\nip=10.77.0.{host_octet}\npeer_ip=10.77.0.{peer_octet}\n",
            encoding="ascii",
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
