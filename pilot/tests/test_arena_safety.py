import pytest

from sandboxer_v0.arena_safety import (
    ArenaNetworkPolicy,
    NetworkObservation,
    Phase,
    ReconciliationLedger,
    TeardownEvidence,
    TeardownState,
)


def test_blue_policy_has_only_private_runner_edges() -> None:
    policy = ArenaNetworkPolicy("m-1", "runner-a", "runner-b")

    assert policy.expected(Phase.BLUE).edges == frozenset({"runner-a:private-a", "runner-b:private-b"})


def test_red_policy_allows_only_declared_opponent_service_path() -> None:
    policy = ArenaNetworkPolicy("m-1", "runner-a", "runner-b", opponent_service="toy-service")

    expected = policy.expected(Phase.RED)

    assert expected.edges == frozenset({"runner-a:private-a", "runner-b:private-b", "runner-a->toy-service", "runner-b->toy-service"})
    assert expected.direct_egress is False
    assert expected.public_ingress is False


def test_observed_extra_edge_fails_closed_with_typed_reasons() -> None:
    policy = ArenaNetworkPolicy("m-1", "runner-a", "runner-b")
    observation = NetworkObservation(
        edges=frozenset({"runner-a:private-a", "runner-b:private-b", "runner-a->internet"}),
        direct_egress=True,
        public_ingress=False,
        orchestrator_reachable=False,
    )

    result = policy.validate(Phase.BLUE, observation)

    assert result.safe is False
    assert result.reason_codes == ("UNDECLARED_NETWORK_EDGE", "DIRECT_EGRESS")


def test_teardown_requires_destroy_evidence_or_quarantine() -> None:
    ledger = ReconciliationLedger()

    ledger.record(TeardownEvidence("runner-a", TeardownState.DESTROYED, "provider-confirmed"))
    ledger.record(TeardownEvidence("runner-b", TeardownState.QUARANTINED, "isolated-and-tagged"))

    assert ledger.pending == ("runner-b",)
    assert ledger.complete is False


def test_teardown_unknown_state_is_rejected_and_duplicate_is_idempotent() -> None:
    ledger = ReconciliationLedger()
    with pytest.raises(ValueError, match="teardown state"):
        ledger.record(TeardownEvidence("runner-a", "lost", ""))  # type: ignore[arg-type]

    evidence = TeardownEvidence("runner-a", TeardownState.DESTROYED, "provider-confirmed")
    ledger.record(evidence)
    ledger.record(evidence)
    assert ledger.complete is True
