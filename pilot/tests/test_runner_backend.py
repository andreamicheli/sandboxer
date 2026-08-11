from __future__ import annotations

import pytest

from sandboxer_v0.runner_backend import (
    SimulatedRunnerProvider,
    ProductionRunnerBackend,
    RunnerPreflightFailed,
    TeardownUncertain,
)
from sandboxer_v0.arena_safety import NetworkObservation, Phase


def test_simulated_contract_fixture_never_claims_a_remote_production_rehearsal() -> None:
    backend = ProductionRunnerBackend(SimulatedRunnerProvider())

    report = backend.rehearse(match_id="rehearsal-001", runner_names=("atlas", "borealis"))

    assert report.terminal_code == "SIMULATED_CONTRACT_PASSED"
    assert report.production_ready is False
    assert report.simulated_fixture is True
    assert report.runner_ids == ("rehearsal-001:atlas", "rehearsal-001:borealis")
    assert {check.name for check in report.preflight_checks} >= {
        "distinct_kernels",
        "orchestrator_unreachable",
        "no_host_mounts",
        "no_credentials",
        "telemetry_egress",
        "clock",
        "ttl",
        "health",
        "cleanup_capability",
    }
    assert all(check.passed for check in report.preflight_checks)
    assert all(evidence.state.value == "destroyed" for evidence in report.teardown_evidence)


def test_real_provider_can_remain_a_rehearsal_until_its_evidence_boundary_is_complete() -> None:
    class IncompleteLocalFixture(SimulatedRunnerProvider):
        simulated_fixture = False
        production_ready = False

    backend = ProductionRunnerBackend(IncompleteLocalFixture())

    report = backend.rehearse(match_id="local-kvm-001", runner_names=("atlas", "borealis"))

    assert report.terminal_code == "RUNNERS_DESTROYED"
    assert report.simulated_fixture is False
    assert report.production_ready is False


def test_active_preflight_fails_closed_before_competitors_can_run() -> None:
    provider = SimulatedRunnerProvider(failing_probes={"orchestrator_unreachable"})
    backend = ProductionRunnerBackend(provider)

    with pytest.raises(RunnerPreflightFailed) as error:
        backend.rehearse(match_id="rehearsal-unsafe", runner_names=("atlas", "borealis"))

    assert error.value.reason_code == "ORCHESTRATOR_REACHABLE"
    assert {evidence.resource_id for evidence in error.value.teardown_evidence} == {
        "rehearsal-unsafe:atlas", "rehearsal-unsafe:borealis"
    }
    assert all(evidence.state.value == "quarantined" for evidence in error.value.teardown_evidence)
    assert provider.quarantine_calls == ["rehearsal-unsafe:atlas", "rehearsal-unsafe:borealis"]
    assert provider.competitor_calls == 0


def test_red_network_drift_fails_closed_before_any_competitor_call() -> None:
    class UnsafeNetworkFixture(SimulatedRunnerProvider):
        def network_observation(self, phase, runners):  # type: ignore[no-untyped-def]
            observed = super().network_observation(phase, runners)
            if phase is Phase.RED:
                return NetworkObservation(
                    observed.edges | frozenset({"atlas->internet"}), True, False, False
                )
            return observed

    provider = UnsafeNetworkFixture()
    backend = ProductionRunnerBackend(provider)

    with pytest.raises(RunnerPreflightFailed) as error:
        backend.rehearse(match_id="rehearsal-network", runner_names=("atlas", "borealis"))

    assert error.value.reason_code == "UNDECLARED_NETWORK_EDGE"
    assert provider.quarantine_calls == ["rehearsal-network:atlas", "rehearsal-network:borealis"]
    assert all(evidence.state.value == "quarantined" for evidence in error.value.teardown_evidence)
    assert provider.competitor_calls == 0


def test_provider_probe_exception_is_quarantined_instead_of_leaking_live_runners() -> None:
    class BrokenProbeFixture(SimulatedRunnerProvider):
        def probe(self, runners):  # type: ignore[no-untyped-def]
            raise RuntimeError("host witness unavailable")

    provider = BrokenProbeFixture()

    with pytest.raises(RunnerPreflightFailed) as error:
        ProductionRunnerBackend(provider).rehearse(
            match_id="rehearsal-probe-error", runner_names=("atlas", "borealis")
        )

    assert error.value.reason_code == "PREFLIGHT_EXECUTION_FAILED"
    assert provider.quarantine_calls == ["rehearsal-probe-error:atlas", "rehearsal-probe-error:borealis"]


def test_teardown_failure_is_quarantined_and_reconciliation_retains_typed_evidence() -> None:
    provider = SimulatedRunnerProvider(teardown_outcomes={"borealis": "failed"})
    backend = ProductionRunnerBackend(provider)

    with pytest.raises(TeardownUncertain) as error:
        backend.rehearse(match_id="rehearsal-cleanup", runner_names=("atlas", "borealis"))

    assert error.value.reason_code == "TEARDOWN_UNCERTAIN"
    assert error.value.teardown_evidence[0].resource_id == "rehearsal-cleanup:borealis"
    reconciliation = backend.reconcile(error.value.quarantined_runner_ids)
    assert reconciliation[0].resource_id == "rehearsal-cleanup:borealis"
    assert reconciliation[0].state.value == "quarantined"
    assert reconciliation[0].reason_code == "DESTROY_FAILED"
