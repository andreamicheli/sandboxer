"""Disposable, no-egress local Docker Runner provider (no-KVM path).

Mirrors ``local_kvm.LocalKvmRunnerProvider`` behind the same
``runner_backend.RunnerProvider`` port so the Orchestrator, Auditor gates,
and teardown semantics stay unchanged.  Differences are declared, not hidden:

- Isolation is Linux namespaces + cgroups via the Docker daemon, NOT hardware
  virtualization.  Both Runners share the host kernel; ``RunnerHandle.kernel_id``
  therefore carries the distinct container ID (distinct mount/pid/net
  namespaces), never a kernel boot ID.  The methodology paper must record this
  as a threat-model downgrade versus KVM.
- The control channel is an Orchestrator-side ``docker exec``/inspect witness
  path, not a virtio-serial socket.  Tool execution (``execute_tool`` and
  friends) is NOT implemented here yet; this provider covers lifecycle,
  preflight witnesses, network phases, and teardown.
- TTL is enforced inline on every operation plus an in-process watchdog timer
  per Runner that runs ``docker stop`` at the deadline.  Unlike KVM's
  crash-independent ``systemd-run`` watchdog, a dead Orchestrator process kills
  the timer; ``reconcile()`` re-sweeps orphans on the next run.

Hardening per Runner (asserted by tests): ``--network none`` in BLUE,
a dedicated ``--internal`` arena network attached only in RED,
``--read-only`` rootfs with a single 64M ``--tmpfs``, ``--user`` non-root,
``--cap-drop=ALL``, ``--security-opt no-new-privileges``, ``--pids-limit``,
``--memory``/``--cpus`` bounds, no published ports, no bind mounts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .arena_safety import NetworkObservation, Phase, TeardownEvidence, TeardownState
from .local_kvm_control import parse_control, parse_network_proof
from .local_kvm_tools import RunnerToolExecutionError, encode_tool_request, parse_tool_response
from .runner_backend import PreflightCheck, PreflightWitnessFailed, RunnerHandle
from .service_spec import ServiceSpec, parse_service_spec

_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_RUNNER_USER = "10001"  # `arena` user in pilot/docker/runner.Dockerfile
_TMPFS_SIZE = "64M"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class DockerHost(Protocol):
    """Small daemon-privileged port; every command is argv-only, never a shell."""

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float = 30) -> CommandResult: ...


class SubprocessDockerHost:
    """Real host: ``docker`` CLI over subprocess, argv-only."""

    def __init__(self, docker_binary: str = "docker") -> None:
        self._binary = docker_binary

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float = 30) -> CommandResult:
        completed = subprocess.run(
            (self._binary, *argv),
            capture_output=True, text=True, timeout=timeout_seconds,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class LocalDockerConfig:
    runner_root: Path
    image: str
    image_digest: str  # hex sha256 of the pinned image (RepoDigest digest part)
    memory_mib: int = 512
    cpus: float = 1.0
    pids_max: int = 128
    ttl_seconds: int = 300
    toy_service_port: int = 8080
    docker_binary: str = "docker"

    def __post_init__(self) -> None:
        if not self.runner_root.is_absolute():
            raise ValueError("local Docker runner_root must be absolute")
        if not self.image or not re.fullmatch(r"[0-9a-f]{64}", self.image_digest):
            raise ValueError("local Docker image requires a pinned SHA-256 digest")
        if self.memory_mib < 256 or self.cpus != 1.0 or not 1 <= self.toy_service_port <= 65535:
            raise ValueError("unsafe local Docker resource configuration")
        if self.pids_max < 32 or self.ttl_seconds < 30:
            raise ValueError("local Docker limits must be bounded")


@dataclass
class _RunnerRecord:
    handle: RunnerHandle
    match_id: str
    container: str
    network: str
    subnet: str
    ip: str
    nonce: str
    root: Path
    deadline: float
    timer: threading.Timer | None = None
    phase: Phase = Phase.BLUE


class LocalDockerRunnerProvider:
    """Disposable, no-egress local Docker provider with an external control plane."""

    simulated_fixture = False
    # Container isolation is weaker than KVM; smoke tests cannot establish
    # adversarial escape resistance.
    production_ready = False

    def __init__(self, config: LocalDockerConfig, host: DockerHost | None = None) -> None:
        self.config = config
        self._host = host or SubprocessDockerHost(config.docker_binary)
        self._records: dict[str, _RunnerRecord] = {}
        self._terminal: dict[str, TeardownEvidence] = {}

    # -- RunnerProvider port -------------------------------------------------

    def provision(self, match_id: str, names: tuple[str, str]) -> tuple[RunnerHandle, RunnerHandle]:
        self._validate_identity(match_id, names)
        self._verify_image()
        match_root = self.config.runner_root / match_id
        if match_root.exists():
            raise RuntimeError("MATCH_ARTIFACTS_ALREADY_EXIST")
        match_root.mkdir(parents=True, mode=0o711)
        os.chmod(match_root, 0o711)
        match_state = match_root.stat()
        if match_state.st_uid != os.geteuid() or stat.S_IMODE(match_state.st_mode) != 0o711:
            self._remove_root(match_root)
            raise RuntimeError("MATCH_ROOT_UNSAFE")
        digest = hashlib.sha256(match_id.encode("ascii")).hexdigest()[:12]
        network = f"sbx-arena-{digest}"
        # Deterministic per-match /24 under 10.77.0.0/16 (mirrors the KVM Arena
        # numbering; peers are always .11/.12). Parallel matches sharing an
        # octet collide on create and fail closed; matches run serially.
        octet = 1 + (int(digest, 16) % 200)
        subnet = f"10.77.{octet}.0/24"
        created: list[_RunnerRecord] = []
        try:
            self._run(
                ("network", "create", "--internal", "--driver", "bridge",
                 "--subnet", subnet,
                 "--label", f"sandboxer.match={match_id}", network),
                "ARENA_NETWORK_CREATE_FAILED",
            )
            for index, name in enumerate(names):
                container = f"sbx-{digest}-{name}"
                ip = f"10.77.{octet}.{11 + index}"
                record = self._provision_runner(match_id, name, container, network, subnet, ip, match_root)
                created.append(record)
                self._records[record.handle.runner_id] = record
        except Exception:
            for record in created:
                self._destroy_record(record, "PROVISION_ROLLBACK")
                self._terminal.pop(record.handle.runner_id, None)
            self._run(("network", "rm", network), "", allow_failure=True)
            self._remove_root(match_root)
            raise
        return (created[0].handle, created[1].handle)

    def probe(self, runners: tuple[RunnerHandle, RunnerHandle]) -> tuple[PreflightCheck, ...]:
        records = self._require_records(runners)
        self._enforce_ttl(records)
        try:
            responses = [self._control_probe(record) for record in records]
        except PreflightWitnessFailed:
            raise
        except Exception as error:
            raise PreflightWitnessFailed("LOCAL_DOCKER_CONTROL_UNAVAILABLE") from error
        toy_states = {item.toy_bootstrap for item in responses}
        if toy_states != {"ready"}:
            state = next(iter(toy_states)) if len(toy_states) == 1 else "unknown"
            raise PreflightWitnessFailed(f"LOCAL_DOCKER_TOY_BOOTSTRAP_{state.upper()}")
        try:
            network = self._measure_network(records, Phase.BLUE)
        except PreflightWitnessFailed:
            raise
        except Exception as error:
            raise PreflightWitnessFailed("LOCAL_DOCKER_BLUE_NETWORK_WITNESS_UNAVAILABLE") from error
        try:
            states = [self._inspect(record.container) for record in records]
        except Exception as error:
            raise PreflightWitnessFailed("LOCAL_DOCKER_INSPECT_UNAVAILABLE") from error
        unique_containers = len({record.container for record in records}) == 2
        now = time.monotonic()
        checks: list[PreflightCheck] = []
        for record, item, state in zip(records, responses, states):
            checks.extend(self._probe_runner(record, item, state, now))
        # Collapse per-runner witnesses into the provider-level probe tuple.
        by_name: dict[str, PreflightCheck] = {}
        for check in checks:
            previous = by_name.get(check.name)
            passed = check.passed if previous is None else (previous.passed and check.passed)
            by_name[check.name] = PreflightCheck(check.name, passed, check.reason_code)
        ordered = (
            PreflightCheck("distinct_kernels", unique_containers, "KERNEL_ISOLATION_LOST"),
            *[by_name[name] for name in (
                "orchestrator_unreachable", "no_host_mounts", "no_credentials",
                "telemetry_egress", "clock", "ttl", "health", "cleanup_capability",
            )],
        )
        # The backend cross-checks the backend-level network policy; surface the
        # measured observation here so a direct-observation leak fails closed.
        if network.orchestrator_reachable or network.direct_egress:
            raise PreflightWitnessFailed("LOCAL_DOCKER_BLUE_NETWORK_WITNESS_FAILED")
        return ordered

    def network_observation(
        self, phase: Phase, runners: tuple[RunnerHandle, RunnerHandle]
    ) -> NetworkObservation:
        records = self._require_records(runners)
        try:
            self._enforce_ttl(records)
            if phase is Phase.RED:
                for record in records:
                    # A container started on `--network none` cannot be
                    # connected to a second network directly; detach the
                    # `none` stub first (the Runner stays without egress
                    # throughout — disconnect and connect are back-to-back),
                    # attaching its deterministic Arena address.
                    self._run(
                        ("network", "disconnect", "none", record.container),
                        "NETWORK_POLICY_APPLY_FAILED",
                    )
                    self._run(
                        ("network", "connect", "--ip", record.ip, record.network, record.container),
                        "NETWORK_POLICY_APPLY_FAILED",
                    )
                    record.phase = Phase.RED
            return self._measure_network(records, phase)
        except PreflightWitnessFailed:
            raise
        except RuntimeError as error:
            if str(error) == "NETWORK_POLICY_APPLY_FAILED":
                raise PreflightWitnessFailed("LOCAL_DOCKER_RED_NETWORK_POLICY_APPLY_FAILED") from error
            raise
        except Exception as error:
            raise PreflightWitnessFailed(
                "LOCAL_DOCKER_RED_NETWORK_TRANSITION_UNAVAILABLE"
                if phase is Phase.RED
                else "LOCAL_DOCKER_BLUE_NETWORK_TRANSITION_UNAVAILABLE"
            ) from error

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
            evidence = self._destroy_record(record, reason_code)
        self._terminal[runner.runner_id] = evidence
        return evidence

    def reconcile(self, runner_id: str) -> TeardownEvidence:
        existing = self._terminal.get(runner_id)
        if existing is not None and existing.state is TeardownState.DESTROYED:
            return existing
        record = self._records.get(runner_id)
        if record is not None:
            evidence = self._destroy_record(record, existing.reason_code if existing else None)
            self._terminal[runner_id] = evidence
            return evidence
        # Unknown Runner: sweep by naming convention (sbx-<digest>-<name>).
        name = runner_id.rsplit(":", 1)[-1]
        if _SAFE_ID.fullmatch(name):
            listed = self._run(("ps", "-a", "--format", "{{.Names}}"), "", allow_failure=True)
            for line in listed.stdout.splitlines():
                if line.strip().endswith(f"-{name}"):
                    self._run(("rm", "-f", line.strip()), "", allow_failure=True)
        return TeardownEvidence(runner_id, TeardownState.DESTROYED, "reconciliation sweep confirmed")

    # -- internals -----------------------------------------------------------

    def execute_tool(self, runner: RunnerHandle, tool: str, arguments: dict[str, object]) -> str:
        """Execute one validated tool through the Runner control agent."""
        record = self._records.get(runner.runner_id)
        if record is None or record.handle != runner:
            raise RuntimeError("RUNNER_TOOL_RUNNER_UNKNOWN")
        self._enforce_ttl([record])
        request = encode_tool_request(record.nonce, tool, arguments)
        parts = request.strip().split(" ")
        try:
            response = self._exec(record, "TOOL", *parts[1:], timeout_seconds=30)
            return parse_tool_response(response + "\n", record.nonce)
        except RunnerToolExecutionError:
            raise
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError("RUNNER_TOOL_CONTROL_UNAVAILABLE") from error

    def place_synthetic_flag(self, runner: RunnerHandle, flag: str) -> None:
        self.execute_tool(runner, "orchestrator_place_flag", {"flag": flag})

    def deploy_service(
        self,
        runner: RunnerHandle,
        raw_spec: str,
        *,
        brief=None,
        brief_family: str | None = None,
        public_note: str | None = None,
    ) -> ServiceSpec:
        """Validate a declarative defense outside the Runner, then promote it."""
        spec = parse_service_spec(raw_spec, brief=brief, brief_family=brief_family, public_note=public_note)
        result = self.execute_tool(runner, "orchestrator_deploy_service", {"config": spec.render_runtime_config()})
        if not result.startswith("deployment promoted "):
            raise RuntimeError("SERVICE_DEPLOYMENT_UNPROMOTED")
        return spec

    def service_request(
        self, runner: RunnerHandle, *, peer: str, method: str, path: str, headers: str, body: str
    ) -> str:
        return self.execute_tool(runner, "orchestrator_http_request", {
            "peer": peer, "method": method, "path": path, "headers": headers, "body": body,
        })

    def runner_address(self, runner: RunnerHandle) -> str:
        record = self._records.get(runner.runner_id)
        if record is None or record.handle != runner:
            raise RuntimeError("RUNNER_TOOL_RUNNER_UNKNOWN")
        return record.ip

    def verified_submission(self, runner: RunnerHandle) -> str:
        try:
            return self.execute_tool(runner, "orchestrator_read_submission", {})
        except RuntimeError as error:
            if str(error) == "RUNNER_TOOL_EXECUTION_FAILED":
                return ""
            raise

    def workspace_digest(self, runner: RunnerHandle) -> str:
        return self.execute_tool(runner, "orchestrator_workspace_digest", {}).strip()

    def peer_flag_witness(self, runner: RunnerHandle, peer: str) -> bool:
        return self.execute_tool(runner, "orchestrator_peer_flag_witness", {"peer": peer}).strip() == "reachable"

    def _exec(self, record: _RunnerRecord, *args: str, timeout_seconds: float = 30) -> str:
        """Run the in-container control agent; fail closed on any anomaly."""
        try:
            result = self._host.run(
                ("exec", record.container, "/usr/local/bin/runner-control", *args),
                timeout_seconds=timeout_seconds,
            )
        except Exception as error:
            raise RuntimeError("RUNNER_TOOL_CONTROL_UNAVAILABLE") from error
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError("RUNNER_TOOL_CONTROL_UNAVAILABLE")
        lines = result.stdout.strip().splitlines()
        if len(lines) > 4:
            raise RuntimeError("RUNNER_TOOL_CONTROL_UNAVAILABLE")
        return "\n".join(lines)

    def _provision_runner(
        self, match_id: str, name: str, container: str, network: str,
        subnet: str, ip: str, match_root: Path,
    ) -> _RunnerRecord:
        root = match_root / name
        root.mkdir(mode=0o700)
        nonce = secrets.token_urlsafe(18)
        runner_uuid = str(uuid.uuid4())
        self._run(("rm", "-f", container), "", allow_failure=True)
        result = self._run(
            ("run", "-d",
             "--name", container,
             "--hostname", name,
             "--label", f"sandboxer.match={match_id}",
             "--label", f"sandboxer.runner={name}",
             "--network", "none",
             "--read-only",
             "--tmpfs", f"/arena:rw,size={_TMPFS_SIZE},mode=1777",
             "--user", _RUNNER_USER,
             "--cap-drop=ALL",
             "--security-opt", "no-new-privileges:true",
             "--pids-limit", str(self.config.pids_max),
             "--memory", f"{self.config.memory_mib}m",
             "--memory-swap", f"{self.config.memory_mib}m",
             "--cpus", f"{self.config.cpus:.1f}",
             "--stop-timeout", "5",
             "-e", f"SANDBOXER_NONCE={nonce}",
             "-e", f"SANDBOXER_RUNNER_UUID={runner_uuid}",
             "-e", f"SANDBOXER_ARENA_SUBNET={subnet}",
             "-e", f"SANDBOXER_TOY_PORT={self.config.toy_service_port}",
             "--entrypoint", "/usr/local/bin/runner-entrypoint",
             self.config.image),
            "RUNNER_CREATE_FAILED",
        )
        container_id = result.stdout.strip()
        if not container_id:
            raise RuntimeError("RUNNER_CREATE_FAILED")
        deadline = time.monotonic() + self.config.ttl_seconds
        handle = RunnerHandle(
            runner_id=f"{match_id}:{name}",
            name=name,
            kernel_id=f"docker:{container_id[:12]}",
            non_root=True,
            resource_limits=True,
            ephemeral_service_state=True,
        )
        record = _RunnerRecord(handle, match_id, container, network, subnet, ip, nonce, root, deadline)
        record.timer = self._arm_ttl(record)
        if not self._container_running(container):
            self._destroy_record(record, "RUNNER_START_FAILED")
            raise RuntimeError("RUNNER_START_FAILED")
        return record

    def _control_probe(self, record: _RunnerRecord):
        try:
            response = self._exec(record, "PROBE", record.nonce, timeout_seconds=30)
        except RuntimeError as error:
            raise PreflightWitnessFailed("LOCAL_DOCKER_CONTROL_UNAVAILABLE") from error
        try:
            return parse_control(response, record.nonce, require_probe=True)
        except Exception as error:
            raise PreflightWitnessFailed("LOCAL_DOCKER_CONTROL_INVALID_FRAME") from error

    def _network_proofs(self, records: list[_RunnerRecord], phase: Phase) -> list:
        proofs = []
        for index, record in enumerate(records):
            peer = records[1 - index].ip
            try:
                response = self._exec(
                    record, "NETPROBE", record.nonce, phase.value, peer, timeout_seconds=60,
                )
            except RuntimeError as error:
                raise PreflightWitnessFailed("LOCAL_DOCKER_NETWORK_WITNESS_UNAVAILABLE") from error
            try:
                proofs.append(parse_network_proof(response, record.nonce, phase.value))
            except Exception as error:
                raise PreflightWitnessFailed("LOCAL_DOCKER_NETWORK_PROOF_INVALID") from error
        return proofs

    @staticmethod
    def _require_blue_network_proofs(proofs: list) -> None:
        checks = (
            ("LOCAL_DOCKER_BLUE_PEER_ISOLATION_WITNESS_FAILED", lambda proof: proof.peer_denied),
            ("LOCAL_DOCKER_BLUE_TOY_SERVICE_WITNESS_FAILED", lambda proof: not proof.toy_http),
            ("LOCAL_DOCKER_BLUE_ALTERNATE_PORT_WITNESS_FAILED", lambda proof: proof.alternate_denied),
            ("LOCAL_DOCKER_BLUE_ICMP_WITNESS_FAILED", lambda proof: proof.icmp_denied),
            ("LOCAL_DOCKER_BLUE_ORCHESTRATOR_WITNESS_FAILED", lambda proof: proof.orchestrator_denied),
        )
        for reason_code, passed in checks:
            if not all(passed(proof) for proof in proofs):
                raise PreflightWitnessFailed(reason_code)
        if not all(proof.egress_denied for proof in proofs):
            raise PreflightWitnessFailed("LOCAL_DOCKER_BLUE_EGRESS_TCP_WITNESS_FAILED")

    @staticmethod
    def _require_red_network_proofs(proofs: list) -> None:
        checks = (
            ("LOCAL_DOCKER_RED_LOCAL_TOY_SERVICE_WITNESS_FAILED", lambda proof: proof.local_toy),
            ("LOCAL_DOCKER_RED_DECLARED_TOY_TCP_WITNESS_FAILED", lambda proof: proof.peer_tcp),
            ("LOCAL_DOCKER_RED_DECLARED_TOY_HTTP_WITNESS_FAILED", lambda proof: not proof.peer_denied and proof.toy_http),
            ("LOCAL_DOCKER_RED_ALTERNATE_PORT_WITNESS_FAILED", lambda proof: proof.alternate_denied),
            ("LOCAL_DOCKER_RED_ICMP_WITNESS_FAILED", lambda proof: proof.icmp_denied),
            ("LOCAL_DOCKER_RED_EGRESS_WITNESS_FAILED", lambda proof: proof.egress_denied),
            ("LOCAL_DOCKER_RED_ORCHESTRATOR_WITNESS_FAILED", lambda proof: proof.orchestrator_denied),
        )
        for reason_code, passed in checks:
            if not all(passed(proof) for proof in proofs):
                raise PreflightWitnessFailed(reason_code)

    def _probe_runner(self, record: _RunnerRecord, item, state: dict, now: float) -> list[PreflightCheck]:
        host_config = state.get("HostConfig", {})
        config = state.get("Config", {})
        mounts = state.get("Mounts", [])
        running = bool(state.get("State", {}).get("Running"))
        readonly = bool(host_config.get("ReadonlyRootfs"))
        no_binds = all(mount.get("Type") != "bind" for mount in mounts)
        user = str(config.get("User", ""))
        non_root = bool(user) and user not in ("0", "root", "0:0")
        memory_ok = int(host_config.get("Memory", 0)) == self.config.memory_mib * 1024 * 1024
        pids_ok = int(host_config.get("PidsLimit", 0)) == self.config.pids_max
        return [
            PreflightCheck("orchestrator_unreachable", running and item.uid != 0, "ORCHESTRATOR_REACHABLE"),
            PreflightCheck("no_host_mounts", running and readonly and no_binds and item.private_mounts, "HOST_MOUNT_DETECTED"),
            PreflightCheck("no_credentials", running and non_root and item.no_credentials, "CREDENTIAL_EXPOSURE"),
            PreflightCheck("telemetry_egress", running, "TELEMETRY_EGRESS_UNAVAILABLE"),
            PreflightCheck("clock", running and item.clock_epoch > 0, "CLOCK_UNVERIFIED"),
            PreflightCheck("ttl", record.deadline > now, "TTL_UNVERIFIED"),
            PreflightCheck("health", running and non_root and memory_ok and pids_ok and item.uid != 0, "RUNNER_HEALTH_UNVERIFIED"),
            PreflightCheck("cleanup_capability", running, "CLEANUP_UNAVAILABLE"),
        ]

    def _measure_network(self, records: list[_RunnerRecord], phase: Phase) -> NetworkObservation:
        names = [record.handle.name for record in records]
        policy_edges = {
            f"{names[0]}:private-a", f"{names[1]}:private-b",
        }
        for record in records:
            try:
                state = self._inspect(record.container)
            except Exception as error:
                raise PreflightWitnessFailed("LOCAL_DOCKER_NETWORK_WITNESS_UNAVAILABLE") from error
            networks = state.get("NetworkSettings", {}).get("Networks", {})
            if phase is Phase.BLUE:
                if set(networks) - {"none"}:
                    raise PreflightWitnessFailed("LOCAL_DOCKER_BLUE_EGRESS_WITNESS_FAILED")
            else:
                if set(networks) != {record.network}:
                    raise PreflightWitnessFailed("LOCAL_DOCKER_RED_NETWORK_WITNESS_FAILED")
                try:
                    endpoint = networks[record.network]
                except (KeyError, TypeError) as error:
                    raise PreflightWitnessFailed("LOCAL_DOCKER_RED_NETWORK_WITNESS_FAILED") from error
                if endpoint.get("IPAddress", "") != record.ip:
                    raise PreflightWitnessFailed("LOCAL_DOCKER_RED_ADDRESS_WITNESS_FAILED")
        if phase is Phase.RED:
            # The Arena network must be internal: no host route, no egress.
            result = self._run(("network", "inspect", records[0].network), "LOCAL_DOCKER_NETWORK_WITNESS_UNAVAILABLE")
            try:
                inspected = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise PreflightWitnessFailed("LOCAL_DOCKER_NETWORK_WITNESS_UNAVAILABLE") from error
            if not inspected or not all(net.get("Internal") for net in inspected):
                raise PreflightWitnessFailed("LOCAL_DOCKER_RED_NETWORK_NOT_INTERNAL")
        proofs = self._network_proofs(records, phase)
        if phase is Phase.BLUE:
            self._require_blue_network_proofs(proofs)
        else:
            self._require_red_network_proofs(proofs)
        edges = set(policy_edges)
        if phase is Phase.RED:
            edges |= {f"{names[0]}->toy-service", f"{names[1]}->toy-service"}
        return NetworkObservation(frozenset(edges), False, False, False)

    def _destroy_record(self, record: _RunnerRecord, reason_code: str | None) -> TeardownEvidence:
        failures: list[str] = []
        if record.timer is not None:
            record.timer.cancel()
            record.timer = None
        self._run(("stop", "-t", "5", record.container), "", allow_failure=True)
        self._run(("rm", "-f", record.container), "", allow_failure=True)
        if self._container_exists(record.container):
            failures.append("CONTAINER_SURVIVED")
        if not self._remove_root(record.root):
            failures.append("RUNNER_ROOT_REMAINS")
        sibling_left = any(
            other.handle.runner_id != record.handle.runner_id and other.network == record.network
            for other in self._records.values()
            if other is not record
        )
        if not sibling_left:
            self._run(("network", "rm", record.network), "", allow_failure=True)
        self._records.pop(record.handle.runner_id, None)
        if failures:
            return TeardownEvidence(record.handle.runner_id, TeardownState.QUARANTINED, ",".join(failures), reason_code or "TEARDOWN_FAILED")
        return TeardownEvidence(record.handle.runner_id, TeardownState.DESTROYED, "docker Runner destroyed; network removed", reason_code)

    def _enforce_ttl(self, records: list[_RunnerRecord]) -> None:
        expired = [record for record in records if time.monotonic() >= record.deadline]
        if not expired:
            return
        for record in expired:
            evidence = self._destroy_record(record, "TTL_EXPIRED")
            self._terminal[record.handle.runner_id] = evidence
        raise RuntimeError("TTL_EXPIRED")

    def _arm_ttl(self, record: _RunnerRecord) -> threading.Timer:
        def _expire() -> None:
            try:
                self._run(("stop", "-t", "5", record.container), "", allow_failure=True)
            except Exception:
                pass

        timer = threading.Timer(self.config.ttl_seconds, _expire)
        timer.daemon = True
        timer.start()
        return timer

    def _verify_image(self) -> None:
        result = self._run(
            ("image", "inspect", self.config.image, "--format", "{{.RepoDigests}} {{.Id}}"),
            "BASE_IMAGE_MISSING",
        )
        if self.config.image_digest not in result.stdout:
            raise RuntimeError("BASE_IMAGE_DIGEST_MISMATCH")

    def _inspect(self, container: str) -> dict:
        result = self._run(("inspect", container), "LOCAL_DOCKER_INSPECT_UNAVAILABLE")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise PreflightWitnessFailed("LOCAL_DOCKER_INSPECT_UNAVAILABLE") from error
        if not payload:
            raise PreflightWitnessFailed("LOCAL_DOCKER_INSPECT_UNAVAILABLE")
        return payload[0]

    def _container_running(self, container: str) -> bool:
        result = self._run(("inspect", container, "--format", "{{.State.Running}}"), "", allow_failure=True)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _container_exists(self, container: str) -> bool:
        result = self._run(("inspect", container), "", allow_failure=True)
        return result.returncode == 0

    def _require_records(self, runners: tuple[RunnerHandle, RunnerHandle]) -> list[_RunnerRecord]:
        records = [self._records.get(runner.runner_id) for runner in runners]
        if any(record is None or record.handle != runner for record, runner in zip(records, runners)):
            raise RuntimeError("RUNNER_UNKNOWN")
        return [record for record in records if record is not None]

    def _run(self, argv: tuple[str, ...], error_code: str, *, allow_failure: bool = False) -> CommandResult:
        try:
            result = self._host.run(argv, timeout_seconds=30)
        except Exception as error:
            if allow_failure:
                return CommandResult(1, "", str(error))
            raise RuntimeError(error_code or "DOCKER_UNAVAILABLE") from error
        if result.returncode != 0 and not allow_failure:
            raise RuntimeError(error_code or "DOCKER_UNAVAILABLE")
        return result

    @staticmethod
    def _validate_identity(match_id: str, names: tuple[str, str]) -> None:
        if not _SAFE_ID.fullmatch(match_id) or len(names) != 2 or len(set(names)) != 2 or not all(
            _SAFE_ID.fullmatch(name) for name in names
        ):
            raise ValueError("local Docker Match and Runner identities must be safe, distinct identifiers")

    @staticmethod
    def _remove_root(root: Path) -> bool:
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return not root.exists()
