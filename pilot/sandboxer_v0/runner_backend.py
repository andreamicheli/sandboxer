"""Provider-neutral production boundary for disposable VM-backed Runners.

No provider is selected here.  A concrete provider must implement the narrow
``RunnerProvider`` port and produce observations for every containment claim.
The included simulated provider is deterministic and disposable: it
exercises the same boundary without creating a cloud resource.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .arena_safety import (
    ArenaNetworkPolicy,
    NetworkObservation,
    Phase,
    ReconciliationLedger,
    TeardownEvidence,
    TeardownState,
)


ProbeName = Literal[
    "distinct_kernels", "orchestrator_unreachable", "no_host_mounts",
    "no_credentials", "telemetry_egress", "clock", "ttl", "health",
    "cleanup_capability",
]


@dataclass(frozen=True)
class RunnerHandle:
    runner_id: str
    name: str
    kernel_id: str
    non_root: bool
    resource_limits: bool
    ephemeral_service_state: bool


@dataclass(frozen=True)
class PreflightCheck:
    name: ProbeName
    passed: bool
    reason_code: str | None = None


@dataclass(frozen=True)
class RehearsalReport:
    terminal_code: str
    production_ready: bool
    simulated_fixture: bool
    runner_ids: tuple[str, str]
    preflight_checks: tuple[PreflightCheck, ...]
    teardown_evidence: tuple[TeardownEvidence, ...]


class RunnerPreflightFailed(RuntimeError):
    def __init__(self, reason_code: str, teardown_evidence: tuple[TeardownEvidence, ...]) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.teardown_evidence = teardown_evidence
        self.quarantined_runner_ids = tuple(item.resource_id for item in teardown_evidence if item.state is TeardownState.QUARANTINED)


class TeardownUncertain(RuntimeError):
    def __init__(self, teardown_evidence: tuple[TeardownEvidence, ...]) -> None:
        super().__init__("TEARDOWN_UNCERTAIN")
        self.reason_code = "TEARDOWN_UNCERTAIN"
        self.teardown_evidence = teardown_evidence
        self.quarantined_runner_ids = tuple(item.resource_id for item in teardown_evidence if item.state is TeardownState.QUARANTINED)


class RunnerProvider(Protocol):
    """The sole cloud/runtime extension point; never exposed to a Runner."""

    simulated_fixture: bool

    def provision(self, match_id: str, names: tuple[str, str]) -> tuple[RunnerHandle, RunnerHandle]: ...

    def probe(self, runners: tuple[RunnerHandle, RunnerHandle]) -> tuple[PreflightCheck, ...]: ...

    def network_observation(
        self, phase: Phase, runners: tuple[RunnerHandle, RunnerHandle]
    ) -> NetworkObservation: ...

    def destroy(self, runner: RunnerHandle) -> TeardownEvidence: ...

    def quarantine(self, runner: RunnerHandle, reason_code: str) -> TeardownEvidence: ...

    def reconcile(self, runner_id: str) -> TeardownEvidence: ...


_REASON_BY_PROBE: dict[str, str] = {
    "distinct_kernels": "KERNEL_ISOLATION_LOST",
    "orchestrator_unreachable": "ORCHESTRATOR_REACHABLE",
    "no_host_mounts": "HOST_MOUNT_DETECTED",
    "no_credentials": "CREDENTIAL_EXPOSURE",
    "telemetry_egress": "TELEMETRY_EGRESS_UNAVAILABLE",
    "clock": "CLOCK_UNVERIFIED",
    "ttl": "TTL_UNVERIFIED",
    "health": "RUNNER_HEALTH_UNVERIFIED",
    "cleanup_capability": "CLEANUP_UNAVAILABLE",
}
_REQUIRED_PROBES: tuple[ProbeName, ...] = tuple(_REASON_BY_PROBE)  # type: ignore[assignment]


class ProductionRunnerBackend:
    """Fail-closed orchestrator-side lifecycle for two fresh private Runners."""

    def __init__(self, provider: RunnerProvider) -> None:
        self._provider = provider
        self._known: dict[str, RunnerHandle] = {}
        self._ledger = ReconciliationLedger()

    def rehearse(self, *, match_id: str, runner_names: tuple[str, str]) -> RehearsalReport:
        if len(runner_names) != 2 or len(set(runner_names)) != 2:
            raise ValueError("a Match requires exactly two distinct Runner names")
        runners = self._provider.provision(match_id, runner_names)
        self._known.update({runner.runner_id: runner for runner in runners})
        checks = self._preflight(runners)
        failed = next((check for check in checks if not check.passed), None)
        if failed is not None:
            code = failed.reason_code or _REASON_BY_PROBE[failed.name]
            raise RunnerPreflightFailed(code, self._quarantine(runners, code))
        policy = ArenaNetworkPolicy(match_id, runners[0].name, runners[1].name)
        for phase in (Phase.BLUE, Phase.RED):
            validation = policy.validate(phase, self._provider.network_observation(phase, runners))
            if not validation.safe:
                code = validation.reason_codes[0]
                raise RunnerPreflightFailed(code, self._quarantine(runners, code))
        evidence = tuple(self._record(self._provider.destroy(runner)) for runner in runners)
        uncertain = tuple(item for item in evidence if item.state is not TeardownState.DESTROYED)
        if uncertain:
            raise TeardownUncertain(uncertain)
        simulated = self._provider.simulated_fixture
        return RehearsalReport(
            terminal_code="SIMULATED_CONTRACT_PASSED" if simulated else "RUNNERS_DESTROYED",
            production_ready=not simulated,
            simulated_fixture=simulated,
            runner_ids=(runners[0].runner_id, runners[1].runner_id),
            preflight_checks=checks, teardown_evidence=evidence,
        )

    def reconcile(self, runner_ids: tuple[str, ...]) -> tuple[TeardownEvidence, ...]:
        return tuple(self._record(self._provider.reconcile(runner_id)) for runner_id in runner_ids)

    def _preflight(self, runners: tuple[RunnerHandle, RunnerHandle]) -> tuple[PreflightCheck, ...]:
        intrinsic = (
            PreflightCheck("distinct_kernels", runners[0].kernel_id != runners[1].kernel_id, _REASON_BY_PROBE["distinct_kernels"]),
            PreflightCheck("health", all(r.non_root and r.resource_limits and r.ephemeral_service_state for r in runners), _REASON_BY_PROBE["health"]),
        )
        observed = {check.name: check for check in self._provider.probe(runners)}
        checks: list[PreflightCheck] = []
        for name in _REQUIRED_PROBES:
            check = observed.get(name)
            if name == "distinct_kernels":
                check = intrinsic[0] if check is None else PreflightCheck(name, check.passed and intrinsic[0].passed, _REASON_BY_PROBE[name])
            elif name == "health":
                check = intrinsic[1] if check is None else PreflightCheck(name, check.passed and intrinsic[1].passed, _REASON_BY_PROBE[name])
            if check is None:
                check = PreflightCheck(name, False, _REASON_BY_PROBE[name])
            checks.append(check if check.passed else PreflightCheck(name, False, check.reason_code or _REASON_BY_PROBE[name]))
        return tuple(checks)

    def _quarantine(
        self, runners: tuple[RunnerHandle, RunnerHandle], reason_code: str
    ) -> tuple[TeardownEvidence, ...]:
        # A failed preflight must never silently continue into a Competitor call.
        return tuple(self._record(self._provider.quarantine(runner, reason_code)) for runner in runners)

    def _record(self, evidence: TeardownEvidence) -> TeardownEvidence:
        self._ledger.record(evidence)
        return evidence


class SimulatedRunnerProvider:
    """Deterministic simulated contract fixture; it never contacts a cloud API."""

    simulated_fixture = True

    def __init__(
        self, *, failing_probes: set[str] | None = None,
        teardown_outcomes: dict[str, str] | None = None,
    ) -> None:
        self._failing_probes = failing_probes or set()
        self._teardown_outcomes = teardown_outcomes or {}
        self.competitor_calls = 0
        self.quarantine_calls: list[str] = []

    def provision(self, match_id: str, names: tuple[str, str]) -> tuple[RunnerHandle, RunnerHandle]:
        return tuple(
            RunnerHandle(f"{match_id}:{name}", name, f"kernel:{match_id}:{name}", True, True, True)
            for name in names
        )  # type: ignore[return-value]

    def probe(self, runners: tuple[RunnerHandle, RunnerHandle]) -> tuple[PreflightCheck, ...]:
        del runners
        return tuple(
            PreflightCheck(name, name not in self._failing_probes, None if name not in self._failing_probes else _REASON_BY_PROBE[name])
            for name in _REQUIRED_PROBES
        )

    def network_observation(
        self, phase: Phase, runners: tuple[RunnerHandle, RunnerHandle]
    ) -> NetworkObservation:
        policy = ArenaNetworkPolicy("fixture-network", runners[0].name, runners[1].name)
        expected = policy.expected(phase)
        return NetworkObservation(expected.edges, False, False, False)

    def destroy(self, runner: RunnerHandle) -> TeardownEvidence:
        failed = self._teardown_outcomes.get(runner.name) == "failed"
        return TeardownEvidence(
            runner.runner_id,
            TeardownState.QUARANTINED if failed else TeardownState.DESTROYED,
            "fixture destroy failed" if failed else "fixture destroy confirmed",
            "DESTROY_FAILED" if failed else None,
        )

    def quarantine(self, runner: RunnerHandle, reason_code: str) -> TeardownEvidence:
        self.quarantine_calls.append(runner.runner_id)
        return TeardownEvidence(
            runner.runner_id, TeardownState.QUARANTINED,
            "fixture containment confirmed", reason_code,
        )

    def reconcile(self, runner_id: str) -> TeardownEvidence:
        runner = self._known_runner(runner_id)
        if runner is not None and self._teardown_outcomes.get(runner.name) == "failed":
            return TeardownEvidence(runner_id, TeardownState.QUARANTINED, "fixture destroy failed", "DESTROY_FAILED")
        return TeardownEvidence(runner_id, TeardownState.DESTROYED, "fixture reconciliation confirmed")

    def _known_runner(self, runner_id: str) -> RunnerHandle | None:
        # The fixture encodes the configured name in the disposable identifier.
        name = runner_id.rsplit(":", 1)[-1]
        return RunnerHandle(runner_id, name, "fixture", True, True, True)
