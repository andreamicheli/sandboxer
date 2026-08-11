"""Provider-neutral Arena network and terminal-resource safety contracts.

The production backend is deliberately not part of this module.  An
Orchestrator adapter supplies observations and teardown evidence; these small
value objects make the safety decision deterministic and auditable before a
provider is selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Phase(StrEnum):
    BLUE = "blue"
    RED = "red"


@dataclass(frozen=True)
class NetworkExpectation:
    edges: frozenset[str]
    direct_egress: bool = False
    public_ingress: bool = False
    orchestrator_reachable: bool = False


@dataclass(frozen=True)
class NetworkObservation:
    """Provider adapter's measured network state, not a provider config."""

    edges: frozenset[str]
    direct_egress: bool
    public_ingress: bool
    orchestrator_reachable: bool


@dataclass(frozen=True)
class NetworkValidation:
    safe: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ArenaNetworkPolicy:
    """The only network edges an Arena may expose in each Match phase."""

    match_id: str
    runner_a: str
    runner_b: str
    opponent_service: str = "toy-service"

    def __post_init__(self) -> None:
        if not self.match_id or not self.runner_a or not self.runner_b:
            raise ValueError("Arena network identity is required")
        if self.runner_a == self.runner_b:
            raise ValueError("Arena Runners must have distinct identities")
        if not self.opponent_service:
            raise ValueError("opponent service is required")

    def expected(self, phase: Phase) -> NetworkExpectation:
        private = frozenset({f"{self.runner_a}:private-a", f"{self.runner_b}:private-b"})
        if phase is Phase.BLUE:
            return NetworkExpectation(private)
        if phase is Phase.RED:
            return NetworkExpectation(
                private
                | frozenset(
                    {
                        f"{self.runner_a}->{self.opponent_service}",
                        f"{self.runner_b}->{self.opponent_service}",
                    }
                )
            )
        raise ValueError(f"unsupported Arena phase: {phase!r}")

    def validate(self, phase: Phase, observation: NetworkObservation) -> NetworkValidation:
        expectation = self.expected(phase)
        reasons: list[str] = []
        if observation.edges - expectation.edges:
            reasons.append("UNDECLARED_NETWORK_EDGE")
        if expectation.edges - observation.edges:
            reasons.append("MISSING_DECLARED_NETWORK_EDGE")
        if observation.direct_egress:
            reasons.append("DIRECT_EGRESS")
        if observation.public_ingress:
            reasons.append("PUBLIC_INGRESS")
        if observation.orchestrator_reachable:
            reasons.append("ORCHESTRATOR_REACHABLE")
        return NetworkValidation(not reasons, tuple(reasons))


class TeardownState(StrEnum):
    DESTROYED = "destroyed"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class TeardownEvidence:
    resource_id: str
    state: TeardownState
    evidence: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not self.resource_id:
            raise ValueError("teardown resource identity is required")
        if not isinstance(self.state, TeardownState):
            raise ValueError("invalid teardown state")
        if not self.evidence:
            raise ValueError("teardown evidence is required")


class ReconciliationLedger:
    """Idempotent terminal evidence ledger for destroy-or-quarantine paths."""

    def __init__(self) -> None:
        self._records: dict[str, TeardownEvidence] = {}

    def record(self, evidence: TeardownEvidence) -> None:
        previous = self._records.get(evidence.resource_id)
        if previous is not None:
            if previous != evidence:
                if (
                    previous.state is TeardownState.QUARANTINED
                    and evidence.state is TeardownState.DESTROYED
                ):
                    self._records[evidence.resource_id] = evidence
                    return
                raise ValueError("conflicting teardown evidence")
            return
        self._records[evidence.resource_id] = evidence

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(
            resource_id
            for resource_id, evidence in self._records.items()
            if evidence.state is TeardownState.QUARANTINED
        )

    @property
    def complete(self) -> bool:
        return bool(self._records) and not self.pending

    @property
    def records(self) -> tuple[TeardownEvidence, ...]:
        return tuple(self._records.values())
